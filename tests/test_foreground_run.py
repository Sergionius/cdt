"""Tests for the foreground capture supervisor (cdt.foreground_run)."""

import io
import os
import signal
import stat
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from cdt.foreground_run import (
    ForegroundCaptureError,
    ForegroundCaptureResult,
    _CaptureSink,
    build_child_command,
    run_captured_child,
)
from cdt.redaction import SecretRedactor

_SECRET = "capture-pipeline-secret"
_TOKEN_ENV = "CAPTURE_TOKEN"


def _child_env(**extra: str) -> dict[str, str]:
    env = dict(os.environ)
    env[_TOKEN_ENV] = _SECRET
    env.update(extra)
    return env


def _redactor() -> SecretRedactor:
    return SecretRedactor.from_env(_child_env())


def _child_argv(script: str) -> list[str]:
    return [sys.executable, "-u", "-c", textwrap.dedent(script)]


def _log_path(tmp_path: Path) -> Path:
    return tmp_path / "run" / "output.log"


def _run(argv, tmp_path, *, terminal=None, env=None, **overrides) -> ForegroundCaptureResult:
    return run_captured_child(
        argv,
        cwd=tmp_path,
        env=env if env is not None else _child_env(),
        log_path=_log_path(tmp_path),
        redactor=_redactor(),
        terminal=terminal,
        **overrides,
    )


def test_build_child_command_uses_unbuffered_current_python():
    command = build_child_command(["run", "demo", "--input", "key=value"])

    assert command[:4] == [sys.executable, "-u", "-m", "cdt"]
    assert command[4:] == ["run", "demo", "--input", "key=value"]


def test_capture_rejects_unsupported_platform_before_spawn(tmp_path, monkeypatch):
    marker = tmp_path / "child-marker"
    monkeypatch.setattr(sys, "platform", "win32")

    with pytest.raises(ForegroundCaptureError, match="POSIX"):
        _run(_child_argv(f"open({str(marker)!r}, 'w').close()"), tmp_path)

    assert not marker.exists()


def test_capture_merges_stdout_stderr_and_shows_only_redacted_text(tmp_path):
    terminal = io.StringIO()
    script = """
        import os, sys
        print("out keeps " + os.environ["CAPTURE_TOKEN"])
        sys.stderr.write("err keeps " + os.environ["CAPTURE_TOKEN"] + "\\n")
        """
    result = _run(_child_argv(script), tmp_path, terminal=terminal)

    assert result.exit_code == 0
    assert result.capture_ok
    saved = _log_path(tmp_path).read_text(encoding="utf-8")
    for captured in (saved, terminal.getvalue()):
        assert "out keeps ***" in captured
        assert "err keeps ***" in captured
        assert _SECRET not in captured


def test_capture_runs_child_without_stdin(tmp_path):
    script = """
        import sys
        data = sys.stdin.read()
        print("stdin:" + data + ":end")
        """
    result = _run(_child_argv(script), tmp_path)

    assert result.exit_code == 0
    assert result.capture_ok
    assert "stdin::end" in _log_path(tmp_path).read_text(encoding="utf-8")


def test_capture_with_tiny_chunks_keeps_utf8_and_redacts_split_secret(tmp_path):
    terminal = io.StringIO()
    script = """
        import os
        print("юникод ✓ split:" + os.environ["CAPTURE_TOKEN"] + ":done")
        """
    result = _run(_child_argv(script), tmp_path, terminal=terminal, chunk_size=2)

    assert result.exit_code == 0
    assert result.capture_ok
    for captured in (_log_path(tmp_path).read_text(encoding="utf-8"), terminal.getvalue()):
        assert "юникод ✓ split:***:done" in captured
        assert _SECRET not in captured


def test_capture_flushes_pending_line_at_eof(tmp_path):
    script = """
        import os, sys
        sys.stdout.write("tail without newline " + os.environ["CAPTURE_TOKEN"])
        """
    result = _run(_child_argv(script), tmp_path)

    assert result.exit_code == 0
    assert result.capture_ok
    saved = _log_path(tmp_path).read_text(encoding="utf-8")
    assert "tail without newline ***" in saved
    assert _SECRET not in saved


def test_capture_fails_closed_on_oversized_line(tmp_path):
    script = """
        import os, sys
        sys.stdout.write("x" * (2 * 1024 * 1024) + os.environ["CAPTURE_TOKEN"] + "\\n")
        print("visible-after-oversize")
        """
    result = _run(_child_argv(script), tmp_path)

    assert result.exit_code == 0
    assert result.capture_ok
    saved = _log_path(tmp_path).read_text(encoding="utf-8")
    assert "CDT redacted oversized output line" in saved
    assert "visible-after-oversize" in saved
    assert _SECRET not in saved
    assert "x" * 4096 not in saved


def test_capture_reports_nonzero_child_exit_code(tmp_path):
    script = """
        import os
        print("before exit " + os.environ["CAPTURE_TOKEN"])
        raise SystemExit(3)
        """
    result = _run(_child_argv(script), tmp_path)

    assert result.exit_code == 3
    assert result.capture_ok
    assert "before exit ***" in _log_path(tmp_path).read_text(encoding="utf-8")


def test_capture_log_has_owner_only_permissions(tmp_path):
    _run(_child_argv("print('permissions')"), tmp_path)

    mode = stat.S_IMODE(_log_path(tmp_path).stat().st_mode)
    assert mode == 0o600


def test_capture_log_open_failure_aborts_before_child_runs(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    marker = tmp_path / "child-marker"

    with pytest.raises(ForegroundCaptureError, match="capture log"):
        run_captured_child(
            _child_argv(f"open({str(marker)!r}, 'w').close()"),
            cwd=tmp_path,
            env=_child_env(),
            log_path=blocker / "output.log",
            redactor=_redactor(),
        )

    assert not marker.exists()


def test_capture_survives_terminal_broken_pipe_and_keeps_redacted_log(tmp_path):
    class BrokenTerminal:
        def write(self, text: str) -> int:
            raise BrokenPipeError(32, "Broken pipe")

        def flush(self) -> None:
            return None

    script = """
        import os
        print("kept for log " + os.environ["CAPTURE_TOKEN"])
        """
    result = _run(_child_argv(script), tmp_path, terminal=BrokenTerminal())

    assert result.exit_code == 0
    assert not result.capture_ok
    assert any("terminal" in error for error in result.capture_errors)
    saved = _log_path(tmp_path).read_text(encoding="utf-8")
    assert "kept for log ***" in saved
    assert _SECRET not in saved


def test_capture_sink_disables_only_failing_log_destination():
    class FlakyLog(io.StringIO):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def write(self, text: str) -> int:
            self.calls += 1
            if self.calls > 1:
                raise OSError(28, "No space left on device")
            return super().write(text)

    log = FlakyLog()
    terminal = io.StringIO()
    sink = _CaptureSink(log, terminal)

    sink.emit("first ***\n")
    sink.emit("second ***\n")
    sink.emit("third ***\n", final=True)

    assert "first ***" in log.getvalue()
    assert "second" not in log.getvalue()
    assert terminal.getvalue() == "first ***\nsecond ***\nthird ***\n"
    assert not sink.ok
    assert any("capture log" in error for error in sink.errors)


_CHILD_INTERRUPT_SCRIPT = """
    import os, signal, sys, time
    def cleanup(signum, frame):
        print("cleanup-started", flush=True)
        time.sleep(0.2)
        print("cleanup-done", flush=True)
        raise SystemExit(130)
    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)
    with open(os.environ["CHILD_READY_FILE"], "w") as handle:
        handle.write("ready")
    print("child-ready", flush=True)
    while True:
        time.sleep(0.05)
    """


def _signal_after_ready(ready_file: Path, signal_number: int, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not ready_file.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.05)
    os.kill(os.getpid(), signal_number)


@pytest.mark.parametrize("signal_number", [signal.SIGINT, signal.SIGTERM], ids=["sigint", "sigterm"])
def test_capture_cancellation_lets_child_clean_up_then_exits(tmp_path, signal_number):
    ready_file = tmp_path / "child-ready"
    terminal = io.StringIO()
    sender = threading.Thread(target=_signal_after_ready, args=(ready_file, signal_number), daemon=True)
    sender.start()
    started = time.monotonic()
    try:
        result = _run(
            _child_argv(_CHILD_INTERRUPT_SCRIPT),
            tmp_path,
            terminal=terminal,
            env=_child_env(CHILD_READY_FILE=str(ready_file)),
            interrupt_grace=2.0,
            eof_grace=2.0,
        )
    finally:
        sender.join(timeout=25)
    elapsed = time.monotonic() - started

    assert result.interrupted
    assert result.exit_code == 130
    assert result.capture_ok
    assert elapsed < 30
    saved = _log_path(tmp_path).read_text(encoding="utf-8")
    for marker in ("child-ready", "cleanup-started", "cleanup-done"):
        assert marker in saved
        assert marker in terminal.getvalue()


def test_capture_escalates_to_kill_when_child_ignores_signals(tmp_path):
    terminal = io.StringIO()
    script = """
        import os, signal, time
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        with open(os.environ["CHILD_READY_FILE"], "w") as handle:
            handle.write("ready")
        print("still-alive", flush=True)
        while True:
            time.sleep(0.05)
        """
    ready_file = tmp_path / "child-ready"
    sender = threading.Thread(
        target=_signal_after_ready,
        args=(ready_file, signal.SIGINT),
        daemon=True,
    )
    sender.start()
    started = time.monotonic()
    try:
        result = _run(
            _child_argv(script),
            tmp_path,
            terminal=terminal,
            env=_child_env(CHILD_READY_FILE=str(ready_file)),
            interrupt_grace=0.3,
            eof_grace=2.0,
        )
    finally:
        sender.join(timeout=25)
    elapsed = time.monotonic() - started

    assert result.interrupted
    assert result.exit_code == -signal.SIGKILL
    assert elapsed < 30
    saved = _log_path(tmp_path).read_text(encoding="utf-8")
    assert "still-alive" in saved
    assert "still-alive" in terminal.getvalue()


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _assert_eventually_gone(pid: int | None) -> None:
    assert pid is not None
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    pytest.fail(f"grandchild {pid} survived the bounded cleanup")


_GRANDCHILD_SCRIPT = textwrap.dedent(
    """
    import signal, sys, time
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    print("grandchild-ready", flush=True)
    time.sleep(120)
    """
)

_CHILD_SPAWNING_SCRIPT = textwrap.dedent(
    """
    import os, subprocess, sys
    grandchild = subprocess.Popen([sys.executable, "-u", "-c", os.environ["GRANDCHILD_SCRIPT"]])
    with open(os.environ["GRANDCHILD_PID_FILE"], "w") as handle:
        handle.write(str(grandchild.pid))
    print("leader-exiting", flush=True)
    """
)


def test_capture_bounds_wait_for_grandchild_holding_pipe(tmp_path):
    """Regression: a grandchild inheriting the pipe must not hang the supervisor.

    The leader exits immediately while its grandchild keeps the pipe write end
    open and ignores SIGTERM. The supervisor must stop draining after the
    bounded EOF grace, terminate the leftover group (escalating to SIGKILL),
    and report the forced teardown instead of reporting a clean capture.
    """
    pid_file = tmp_path / "grandchild-pid"
    terminal = io.StringIO()
    started = time.monotonic()
    try:
        result = _run(
            _child_argv(_CHILD_SPAWNING_SCRIPT),
            tmp_path,
            terminal=terminal,
            env=_child_env(
                GRANDCHILD_SCRIPT=_GRANDCHILD_SCRIPT,
                GRANDCHILD_PID_FILE=str(pid_file),
            ),
            interrupt_grace=0.5,
            eof_grace=1.0,
        )
    finally:
        leftover_pid = _read_pid(pid_file)
        if leftover_pid is not None:
            try:
                os.kill(leftover_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    elapsed = time.monotonic() - started

    assert result.exit_code == 0
    assert elapsed < 30
    assert not result.residual_processes
    assert any("capture stream" in error for error in result.capture_errors)
    saved = _log_path(tmp_path).read_text(encoding="utf-8")
    assert "leader-exiting" in saved
    assert "grandchild-ready" in saved
    assert "grandchild-ready" in terminal.getvalue()
    _assert_eventually_gone(_read_pid(pid_file))
