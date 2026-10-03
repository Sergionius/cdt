"""Foreground supervisor capturing a child CDT run's combined output safely.

This module is the core of the optional ``cdt run <pipeline> --capture-output``
mode: it starts one child CDT process without a shell in its own POSIX process
group, streams the merged stdout/stderr through an incremental UTF-8 decoder
and the existing :class:`~cdt.redaction.StreamingRedactor`, and writes only
redacted data to the private capture log and to the terminal at the same time.

Guarantees and limits:

- Foreground parent only: no PTY, no background detachment, and the child
  gets ``stdin=DEVNULL``. ``-u`` disables the child Python's own output
  buffering; internal buffering of third-party CLIs is not addressed and may
  delay their output.
- Capture covers what reaches the child run's stdout/stderr. Private files of
  third-party tools and processes that redirected their own descriptors are
  not captured, and unknown secrets cannot be recognized by redaction.
- SIGINT/SIGTERM interrupt the child process group so it can clean up, then
  escalate SIGTERM/SIGKILL across the remaining group with bounded grace
  periods; the direct child is always reaped.
- Waiting for stream EOF after the child exits is bounded so a grandchild
  inheriting the pipe cannot hang the supervisor. Leftover members of the
  child process group are terminated independently of the leader's exit.
- Completing the child's own process group is never claimed to stop processes
  that created their own session or process group; they are outside this
  guarantee. Cleanup failures and surviving group members are reported in
  the result instead of being hidden.
"""

from __future__ import annotations

import codecs
import os
import select
import signal
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from .redaction import SecretRedactor, StreamingRedactor

_CHUNK_SIZE = 65536
_LOG_FILE_MODE = 0o600
_POLL_INTERVAL_SECONDS = 0.05
_DEFAULT_INTERRUPT_GRACE_SECONDS = 10.0
_DEFAULT_EOF_GRACE_SECONDS = 5.0
_MIN_GRACE_SECONDS = 0.05
_REAP_TIMEOUT_SECONDS = 10.0


class ForegroundCaptureError(RuntimeError):
    """Raised before the child starts when foreground capture cannot be set up."""


@dataclass(frozen=True)
class ForegroundCaptureResult:
    """Outcome of one foreground capture run.

    ``capture_errors`` lists every capture-side failure (stream, log, or
    terminal failures, forced teardowns). A non-empty tuple means the capture
    was incomplete; callers must not report a clean successful capture.
    """

    exit_code: int
    interrupted: bool
    capture_errors: tuple[str, ...] = ()
    residual_processes: bool = False

    @property
    def capture_ok(self) -> bool:
        return not self.capture_errors and not self.residual_processes


def build_child_command(args: Sequence[str]) -> list[str]:
    """Build the child CDT command for capture: current Python with ``-u``.

    ``-u`` disables the child Python's own stdout/stderr buffering so streamed
    output reaches the supervisor promptly. Internal buffering of third-party
    CLIs invoked by the pipeline is not addressed and may delay their output.
    """
    executable = sys.executable or "python3"
    return [executable, "-u", "-m", "cdt", *args]


def run_captured_child(
    argv: Sequence[str],
    *,
    cwd: Path,
    log_path: Path,
    env: Mapping[str, str] | None = None,
    redactor: SecretRedactor | None = None,
    terminal: IO[str] | None = None,
    interrupt_grace: float = _DEFAULT_INTERRUPT_GRACE_SECONDS,
    eof_grace: float = _DEFAULT_EOF_GRACE_SECONDS,
    chunk_size: int = _CHUNK_SIZE,
) -> ForegroundCaptureResult:
    """Run *argv* under the foreground capture supervisor and return its outcome.

    The child starts without a shell in its own POSIX session and process
    group with ``stdin=DEVNULL`` and merged stdout/stderr. Only redacted data
    is ever written to *log_path* (owner-only permissions) or to *terminal*.

    Interrupts interrupt the child group first so it can clean up, then
    escalate to SIGTERM/SIGKILL with bounded grace periods. All waits are
    bounded; a capture-side failure is reported through ``capture_errors``
    and never presented as a successful capture.

    Raises:
        ForegroundCaptureError: before the child starts, when the platform
            lacks process-group primitives or the capture log cannot be
            opened; the pipeline is not executed in that case.
    """
    _require_posix_capture()
    environment = dict(env) if env is not None else dict(os.environ)
    active_redactor = redactor if redactor is not None else SecretRedactor.from_env(environment)
    active_terminal = terminal if terminal is not None else sys.stdout
    # The capture log is opened before the child exists: a failure here aborts
    # the whole capture before any pipeline step can execute.
    log = _open_capture_log(log_path)
    state = _InterruptState()
    restored = state.install()
    errors: list[str] = []
    bounded_eof_grace = max(eof_grace, _MIN_GRACE_SECONDS)
    process: subprocess.Popen[bytes] | None = None
    pgid = 0
    residual = False
    try:
        process = _spawn_child(argv, cwd=cwd, env=environment)
        # start_new_session made the child a group leader: its pid is the pgid.
        pgid = process.pid
        try:
            escalation = _GroupEscalation(pgid, interrupt_grace, errors)
            sink = _CaptureSink(log, active_terminal)
            errors.extend(
                _pump_output(process, active_redactor, sink, state, escalation, bounded_eof_grace, max(1, chunk_size))
            )
            errors.extend(sink.errors)
            exit_code, reap_errors = _reap_process(process, pgid, bounded_eof_grace)
            errors.extend(reap_errors)
        finally:
            if process.stdout is not None:
                try:
                    process.stdout.close()
                except OSError:
                    pass
        residual = _cleanup_leftover_group(pgid, max(interrupt_grace, _MIN_GRACE_SECONDS), errors)
    except BaseException:
        # An unexpected failure must never leave the child running unobserved.
        if process is not None and process.poll() is None:
            _signal_group(pgid, signal.SIGKILL, errors)
            try:
                process.wait(timeout=_REAP_TIMEOUT_SECONDS)
            except (OSError, subprocess.TimeoutExpired):
                pass
        raise
    finally:
        for number, previous in reversed(restored):
            try:
                signal.signal(number, previous)
            except (OSError, ValueError):
                pass
        try:
            log.close()
        except OSError:
            pass
    return ForegroundCaptureResult(
        exit_code=exit_code,
        interrupted=state.count > 0,
        capture_errors=tuple(dict.fromkeys(errors)),
        residual_processes=residual,
    )


class _CaptureSink:
    """Fan out one already-redacted stream to the capture log and the terminal.

    A failing destination (including ``BrokenPipeError`` on the terminal copy)
    is disabled after its first error instead of switching to raw output or
    stopping the pump: the child stays observed, already-redacted data keeps
    flowing to the healthy destination, and the error is reported to the
    caller so a broken capture is never reported as successful.
    """

    def __init__(self, log: IO[str], terminal: IO[str]) -> None:
        self._log = log
        self._terminal = terminal
        self._log_enabled = True
        self._terminal_enabled = True
        self.errors: list[str] = []

    @property
    def ok(self) -> bool:
        return not self.errors

    def emit(self, text: str, *, final: bool = False) -> None:
        if not text and not final:
            return
        if self._log_enabled:
            try:
                self._log.write(text)
                self._log.flush()
            except OSError as exc:
                self._log_enabled = False
                self.errors.append(f"capture log write failed; saved copy stopped: {exc}")
        if self._terminal_enabled:
            try:
                self._terminal.write(text)
                self._terminal.flush()
            except (OSError, ValueError) as exc:
                self._terminal_enabled = False
                self.errors.append(f"terminal output failed; live copy stopped: {exc}")


def _pump_output(
    process: subprocess.Popen[bytes],
    redactor: SecretRedactor,
    sink: _CaptureSink,
    state: _InterruptState,
    escalation: _GroupEscalation,
    eof_grace: float,
    chunk_size: int,
) -> list[str]:
    """Stream the child's merged output through decode + redact + emit.

    Reads bounded chunks so a fast producer cannot grow the supervisor's
    memory; nothing but the current chunk and the redactor's bounded pending
    line is ever held in memory, and no raw temporary file is involved.
    Oversized single lines keep the existing fail-closed handling of
    :class:`StreamingRedactor`: the line is discarded behind a visible
    replacement marker instead of being logged raw or buffered unbounded.
    """
    errors: list[str] = []
    assert process.stdout is not None
    stream_fd = process.stdout.fileno()
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    stream = StreamingRedactor(redactor)
    child_exited_at: float | None = None
    try:
        while True:
            now = time.monotonic()
            if state.count:
                escalation.start(now)
            escalation.tick(now)
            if child_exited_at is None and process.poll() is not None:
                child_exited_at = now
            if child_exited_at is not None and now - child_exited_at >= eof_grace:
                # A grandchild inheriting the pipe must not hang the supervisor
                # forever; the leftover group is cleaned up by the caller.
                errors.append("child kept the capture stream open after exiting; "
                              "stopped draining after the bounded grace")
                break
            try:
                ready, _, _ = select.select([stream_fd], [], [], _POLL_INTERVAL_SECONDS)
            except OSError as exc:
                errors.append(f"capture stream select failed: {exc}")
                break
            if not ready:
                continue
            try:
                chunk = os.read(stream_fd, chunk_size)
            except OSError as exc:
                errors.append(f"capture stream read failed: {exc}")
                break
            if not chunk:
                break
            sink.emit(stream.feed(decoder.decode(chunk)))
    finally:
        # EOF: flush the decoder remainder and the redactor's pending line so a
        # final partial line is still redacted and persisted.
        sink.emit(stream.feed(decoder.decode(b"", final=True), final=True), final=True)
    return errors


def _open_capture_log(log_path: Path) -> IO[str]:
    """Open the capture log owner-only (0600) before the child is started."""
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handle = log_path.open("w", encoding="utf-8", errors="replace")
        # os.open mode is filtered by umask, so enforce the final mode.
        os.fchmod(handle.fileno(), _LOG_FILE_MODE)
        return handle
    except OSError as exc:
        raise ForegroundCaptureError(f"Cannot open capture log {log_path}: {exc}") from exc


class _InterruptState:
    """Counts SIGINT/SIGTERM deliveries; the pump loop acts on the count."""

    def __init__(self) -> None:
        self.count = 0

    def _record(self, signum: int, frame: Any) -> None:
        self.count += 1

    def install(self) -> list[tuple[int, Any]]:
        """Install the counting handler; returns previous handlers to restore.

        Outside the main thread ``signal.signal`` fails; capture then runs
        without interrupt forwarding instead of refusing to start.
        """
        previous: list[tuple[int, Any]] = []
        for number in (signal.SIGINT, signal.SIGTERM):
            try:
                previous.append((number, signal.signal(number, self._record)))
            except (OSError, ValueError):
                continue
        return previous


class _GroupEscalation:
    """Bounded SIGINT -> SIGTERM -> SIGKILL teardown of the child process group.

    The first interrupt reaches the whole group so the child can run its own
    cleanup. If it is still running after the grace period, the remaining
    group members are escalated to SIGTERM and then SIGKILL, each after the
    same bounded grace.
    """

    def __init__(self, pgid: int, grace: float, errors: list[str]) -> None:
        self._pgid = pgid
        self._grace = max(grace, _MIN_GRACE_SECONDS)
        self._errors = errors
        self._phase = 0
        self._deadline = 0.0

    def start(self, now: float) -> None:
        if self._phase == 0:
            self._enter(1, signal.SIGINT, now)

    def tick(self, now: float) -> None:
        if self._phase in (0, 3) or now < self._deadline:
            return
        if self._phase == 1:
            self._enter(2, signal.SIGTERM, now)
        elif self._phase == 2:
            self._enter(3, signal.SIGKILL, now)

    def _enter(self, phase: int, number: int, now: float) -> None:
        self._phase = phase
        self._deadline = now + self._grace
        _signal_group(self._pgid, number, self._errors)


def _signal_group(pgid: int, number: int, errors: list[str]) -> None:
    try:
        os.killpg(pgid, number)
    except ProcessLookupError:
        pass
    except OSError as exc:
        errors.append(f"cannot signal process group {pgid}: {exc}")


def _group_alive(pgid: int) -> bool:
    """Whether the child process group still has any member, leader or not."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _wait_group_gone(pgid: int, timeout: float) -> bool:
    deadline = time.monotonic() + max(timeout, 0.0)
    while _group_alive(pgid):
        if time.monotonic() >= deadline:
            return not _group_alive(pgid)
        time.sleep(_POLL_INTERVAL_SECONDS)
    return True


def _cleanup_leftover_group(pgid: int, grace: float, errors: list[str]) -> bool:
    """Terminate remaining group members independently of the leader's exit.

    Returns ``True`` when processes may still be running in the group after
    the bounded TERM/KILL escalation; cleanup failures are never hidden.
    """
    if not _group_alive(pgid):
        return False
    for number in (signal.SIGTERM, signal.SIGKILL):
        _signal_group(pgid, number, errors)
        if _wait_group_gone(pgid, grace):
            return False
    if _group_alive(pgid):
        errors.append(f"processes may still be running in the child process group {pgid}")
        return True
    return False


def _reap_process(process: subprocess.Popen[bytes], pgid: int, timeout: float) -> tuple[int, list[str]]:
    """Reap the direct child, escalating to a group kill when it lingers."""
    errors: list[str] = []
    deadline = time.monotonic() + max(timeout, 0.0)
    while process.poll() is None:
        if time.monotonic() >= deadline:
            _signal_group(pgid, signal.SIGKILL, errors)
            try:
                process.kill()
            except OSError:
                pass
            break
        time.sleep(_POLL_INTERVAL_SECONDS)
    try:
        return process.wait(timeout=_REAP_TIMEOUT_SECONDS), errors
    except subprocess.TimeoutExpired:
        errors.append(f"failed to reap the captured child after TERM and KILL: pid {process.pid}")
        return -1, errors


def _require_posix_capture() -> None:
    """Reject platforms without the process-group primitives capture relies on."""
    if sys.platform == "win32" or not hasattr(os, "killpg") or not hasattr(os, "setsid"):
        raise ForegroundCaptureError(
            "Foreground output capture requires POSIX process-group primitives (Linux/macOS); "
            f"this platform ({sys.platform}) is not supported"
        )


def _spawn_child(argv: Sequence[str], *, cwd: Path, env: Mapping[str, str]) -> subprocess.Popen[bytes]:
    """Start the captured child in its own POSIX process group.

    ``start_new_session`` makes the child a session and group leader, so the
    supervisor receives terminal SIGINT/SIGTERM itself and decides when the
    child group is interrupted. Callers must check :func:`_require_posix_capture`
    first; the flag is a POSIX feature.
    """
    try:
        return subprocess.Popen(
            [str(argument) for argument in argv],
            cwd=str(cwd),
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        raise ForegroundCaptureError(f"Cannot start the captured child process: {exc}") from exc
