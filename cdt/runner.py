import os
import re
import shlex
import signal
import subprocess
import tempfile
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import typer

from . import config
from .redaction import SecretRedactor, StreamingRedactor


@dataclass
class SpawnedProcess:
    proc: subprocess.Popen
    log_path: Path | None


class CommandExecutionError(Exception):
    """A caller-raised failure for a command that exited with a nonzero code.

    ``CommandRunner.run()`` keeps returning an integer exit code; this exception
    exists so steps can surface a readable cause together with the exact command
    and exit code for aggregation and reporting.
    """

    def __init__(self, cause: str, *, command: list[str], exit_code: int) -> None:
        if not cause.strip():
            raise ValueError("CommandExecutionError requires a nonempty cause")
        super().__init__(cause)
        self.cause = cause
        self.command = list(command)
        self.exit_code = exit_code


class CommandRunner:
    def run(self, command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> int:
        if env is None:
            return _run(command, cwd=cwd)
        return _run(command, cwd=cwd, env=env)

    def run_with_diagnostics(
        self, command: list[str], *, cwd: Path, run_dir: Path, env: dict[str, str]
    ) -> tuple[int, str, Path | None]:
        """Capture verbose build output once, retaining a redacted log only on failure."""
        return _run_with_diagnostics(command, cwd=cwd, run_dir=run_dir, env=env)

    def spawn(self, command: list[str], *, cwd: Path) -> SpawnedProcess:
        proc, log_path = _spawn(command, cwd=cwd)
        return SpawnedProcess(proc=proc, log_path=log_path)

    def tail(self, path: Path, lines: int = 60) -> str:
        return _tail_text(path, lines=lines)


# Bounded grace period between TERM and KILL when terminating a process group.
PROCESS_GROUP_TERMINATE_GRACE_SECONDS = 5.0


def supports_process_groups() -> bool:
    """Whether this platform can start children in a separate process group.

    Only POSIX platforms can offer the process-tree termination guarantee used
    by the managed subprocess helper; callers must reject timeout capabilities
    elsewhere instead of pretending the guarantee exists.
    """
    return os.name == "posix"


def run_managed_subprocess(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
) -> int:
    """Run a child to completion, inheriting the current stdio, cwd and env.

    On POSIX the child starts in its own process group. When the wait times
    out or is interrupted, the whole group is terminated: first TERM, then,
    after a bounded grace period, KILL; the direct child is always reaped.
    Children that leave the process group on their own are outside this
    guarantee. A ``timeout`` on a platform without process groups raises
    instead of claiming a termination guarantee it cannot keep.
    """
    if timeout is not None and not supports_process_groups():
        raise RuntimeError(
            f"Managed subprocess timeout requires POSIX process groups; this platform ({os.name}) "
            "cannot guarantee process-tree termination"
        )
    popen_kwargs: dict = {}
    if supports_process_groups():
        popen_kwargs["start_new_session"] = True
    if env is not None:
        popen_kwargs["env"] = env
    proc = subprocess.Popen(command, cwd=cwd, **popen_kwargs)
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate_process_group(proc)
        raise
    except BaseException:
        # Interrupted wait (e.g. KeyboardInterrupt): same cleanup, then re-raise.
        _terminate_process_group(proc)
        raise


def _terminate_process_group(proc: subprocess.Popen) -> None:
    """TERM the process group, then KILL after a bounded grace; always reap."""
    deadline = time.monotonic() + PROCESS_GROUP_TERMINATE_GRACE_SECONDS
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=PROCESS_GROUP_TERMINATE_GRACE_SECONDS)
        # Reaping the parent does not imply its descendants have exited.
        # Give the remaining group the rest of the same bounded grace period.
        while True:
            try:
                os.killpg(proc.pid, 0)
            except ProcessLookupError:
                return
            except PermissionError:
                # A denied probe does not prove the group has exited. Keep
                # the bounded grace and KILL attempt instead of masking the
                # original timeout/interrupt with the probe error.
                pass
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.05, remaining))
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=PROCESS_GROUP_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired as exc:
        # Never hide a failed cleanup behind the original error.
        raise RuntimeError(f"Failed to reap managed subprocess after TERM and KILL: pid {proc.pid}") from exc


def _tail_text(path: Path, lines: int = 60) -> str:
    try:
        data = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(data[-lines:])
    except Exception:
        return ""


def _run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> int:
    cmd_preview = " ".join(shlex.quote(x) for x in command)
    child_env = os.environ | env if env is not None else None
    if config.UI_MODE == "verbose":
        typer.echo(f"$ {cmd_preview} (cwd={cwd})")
        popen_kwargs = {"env": child_env} if child_env is not None else {}
        proc = subprocess.Popen(command, cwd=cwd, **popen_kwargs)
        return proc.wait()

    if config.UI_MODE != "pretty":
        typer.echo(f"… {command[0]} {' '.join(command[1:3])}".strip())
    with tempfile.NamedTemporaryFile(prefix="cdt-", suffix=".log", delete=False) as tmp:
        log_path = Path(tmp.name)

    with log_path.open("w", encoding="utf-8") as f:
        proc = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=f,
            stderr=subprocess.STDOUT,
            **({"env": child_env} if child_env is not None else {}),
        )
        code = proc.wait()

    if code == 0:
        try:
            os.remove(log_path)
        except FileNotFoundError:
            pass
        return code

    typer.echo(f"Command failed with exit code {code}. Full log: {log_path}", err=True)
    tail = _tail_text(log_path)
    if tail:
        typer.echo("--- last log lines ---", err=True)
        typer.echo(tail, err=True)
        typer.echo("--- end log ---", err=True)
    return code


def _run_with_diagnostics(
    command: list[str], *, cwd: Path, run_dir: Path, env: dict[str, str]
) -> tuple[int, str, Path | None]:
    """Keep the raw Flutter stream private and temporary; persist only redacted failures.

    Flutter's normal output can hide the underlying xcodebuild error. Its verbose
    stream includes the xcodebuild transcript without invoking another build.
    """
    with tempfile.NamedTemporaryFile(prefix="cdt-ios-", suffix=".log", delete=False) as tmp:
        raw_path = Path(tmp.name)
    try:
        with raw_path.open("wb") as output:
            proc = subprocess.Popen(
                command, cwd=cwd, env=os.environ | env, stdin=subprocess.DEVNULL,
                stdout=output, stderr=subprocess.STDOUT,
            )
            code = proc.wait()
        if code == 0:
            return 0, "", None

        destination = run_dir / "ios-build.log"
        # Never persist an unredacted copy inside the project. Redact one line at
        # a time to bound memory even for very large Xcode transcripts.
        redactor = StreamingRedactor(SecretRedactor.from_env(env))
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(destination, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            with (
                os.fdopen(fd, "w", encoding="utf-8") as saved,
                raw_path.open("r", encoding="utf-8", errors="replace") as source,
            ):
                while chunk := source.read(65536):
                    saved.write(redactor.feed(chunk))
                saved.write(redactor.feed("", final=True))
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        # Read the redacted copy, not the raw output, for user-facing errors.
        details = _ios_failure_details(destination)
        return code, details, destination
    finally:
        raw_path.unlink(missing_ok=True)


def _ios_failure_details(path: Path) -> str:
    """Select a specific Xcode failure rather than repeating Flutter's generic 74."""
    generic = ("xcodebuild encountered an error", "failed to build ios app", "encountered error while archiving")
    selected: deque[str] = deque(maxlen=3)
    with path.open(encoding="utf-8", errors="replace") as log:
        for line in log:
            text = line.strip()
            if not text or any(phrase in text.lower() for phrase in generic):
                continue
            if re.search(
                r"\berror:|\*\* (?:archive|export) failed \*\*|Error Domain="
                r"|provisioning profile .* (?:missing|expired|does not)",
                text,
                re.I,
            ):
                selected.append(text[:500])
    return "\n".join(selected)


def _spawn(command: list[str], *, cwd: Path) -> tuple[subprocess.Popen, Path | None]:
    if config.UI_MODE == "verbose":
        proc = subprocess.Popen(command, cwd=cwd)
        return proc, None

    with tempfile.NamedTemporaryFile(prefix="cdt-", suffix=".log", delete=False) as tmp:
        log_path = Path(tmp.name)
    f = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        command,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=f,
        stderr=subprocess.STDOUT,
    )
    f.close()
    return proc, log_path


def _prepare_git_clean_main(repo: Path) -> None:
    if _run(["git", "rev-parse", "--is-inside-work-tree"], cwd=repo) != 0:
        raise typer.BadParameter(f"Not a git repository: {repo}")

    if _run(["git", "restore", "."], cwd=repo) != 0:
        raise typer.BadParameter("Failed to restore tracked files")
    if _run(["git", "clean", "-fd"], cwd=repo) != 0:
        raise typer.BadParameter("Failed to clean untracked files")

    # Prefer main, fallback to master.
    if _run(["git", "checkout", "main"], cwd=repo) != 0:
        if _run(["git", "checkout", "master"], cwd=repo) != 0:
            raise typer.BadParameter("Neither main nor master branch is available")
