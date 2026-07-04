import os
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import typer

from . import config


@dataclass
class SpawnedProcess:
    proc: subprocess.Popen
    log_path: Path | None


class CommandRunner:
    def run(self, command: list[str], *, cwd: Path) -> int:
        return _run(command, cwd=cwd)

    def spawn(self, command: list[str], *, cwd: Path) -> SpawnedProcess:
        proc, log_path = _spawn(command, cwd=cwd)
        return SpawnedProcess(proc=proc, log_path=log_path)

    def tail(self, path: Path, lines: int = 60) -> str:
        return _tail_text(path, lines=lines)


def _tail_text(path: Path, lines: int = 60) -> str:
    try:
        data = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(data[-lines:])
    except Exception:
        return ""


def _run(command: list[str], *, cwd: Path) -> int:
    cmd_preview = " ".join(shlex.quote(x) for x in command)
    if config.UI_MODE == "verbose":
        typer.echo(f"$ {cmd_preview} (cwd={cwd})")
        proc = subprocess.Popen(command, cwd=cwd)
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
