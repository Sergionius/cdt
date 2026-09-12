from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any

from . import __version__
from .redaction import SecretRedactor, StreamingRedactor

RUN_SCHEMA_VERSION = 1
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True)
class RunPaths:
    run_id: str
    root: Path
    manifest: Path
    status: Path
    log: Path
    exit: Path
    pid: Path


def runs_dir(cwd: Path) -> Path:
    return cwd / ".cdt" / "runs"


def create_run(
    cwd: Path,
    pipeline: str,
    *,
    ids: list[str] | None = None,
    run_id: str | None = None,
    command: list[str] | None = None,
    detached: bool = False,
    inputs: dict[str, str] | None = None,
    env: dict[str, str] | None = None,
) -> RunPaths:
    run_id = run_id or generate_run_id(pipeline)
    paths = run_paths(cwd, run_id)
    paths.root.mkdir(parents=True, exist_ok=False)
    paths.log.touch()
    # Defense-in-depth: inputs are declared non-secret, but run records still pass
    # through the same redaction layer as every other persisted value.
    redactor = SecretRedactor.from_env(env or {})
    manifest = {
        "schema_version": RUN_SCHEMA_VERSION,
        "run_id": run_id,
        "pipeline": pipeline,
        "ids": list(ids or []),
        "inputs": redactor.redact_data(dict(inputs or {})),
        "cdt_version": __version__,
        "project_root": str(cwd.resolve()),
        "git_commit": _git_value(cwd, ["rev-parse", "HEAD"]),
        "git_branch": _git_value(cwd, ["branch", "--show-current"]),
        "started_at": now(),
        "command": redactor.redact_data(command or ["cdt", "run", pipeline]),
        "detached": detached,
    }
    write_json_atomic(paths.manifest, manifest)
    write_json_atomic(
        paths.status,
        {
            "schema_version": RUN_SCHEMA_VERSION,
            "run_id": run_id,
            "status": "queued",
            "pipeline": pipeline,
            "current_step": None,
            "completed_steps": [],
            "failed_step": None,
            "error": None,
            "running_steps": [],
            "parallel_completed": [],
            "parallel_failed": [],
            "artifacts": [],
            "inputs": redactor.redact_data(dict(inputs or {})),
            "old_version": None,
            "new_version": None,
            "started_at": manifest["started_at"],
            "finished_at": None,
            "updated_at": now(),
        },
    )
    set_latest_run(cwd, pipeline, run_id)
    return paths


def ensure_run(
    cwd: Path,
    pipeline: str,
    *,
    ids: list[str] | None = None,
    run_id: str | None = None,
    command: list[str] | None = None,
    detached: bool = False,
    inputs: dict[str, str] | None = None,
    env: dict[str, str] | None = None,
) -> RunPaths:
    if run_id is not None:
        paths = run_paths(cwd, run_id)
        if paths.root.exists():
            return paths
    return create_run(
        cwd,
        pipeline,
        ids=ids,
        run_id=run_id,
        command=command,
        detached=detached,
        inputs=inputs,
        env=env,
    )


def generate_run_id(pipeline: str) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", pipeline).strip("-.") or "pipeline"
    return f"{timestamp}-{slug[:48]}-{secrets.token_hex(2)}"


def run_paths(cwd: Path, run_id: str) -> RunPaths:
    if not _RUN_ID_RE.fullmatch(run_id) or ".." in run_id:
        raise ValueError(f"Invalid run id: {run_id}")
    root = runs_dir(cwd) / run_id
    return RunPaths(
        run_id=run_id,
        root=root,
        manifest=root / "manifest.json",
        status=root / "status.json",
        log=root / "output.log",
        exit=root / "exit-code",
        pid=root / "pid",
    )


def resolve_run(cwd: Path, *, run_id: str | None = None, pipeline: str | None = None) -> RunPaths | None:
    if run_id:
        try:
            paths = run_paths(cwd, run_id)
        except ValueError:
            return None
        return paths if paths.root.is_dir() else None
    if pipeline:
        marker = latest_marker(cwd, pipeline)
        try:
            latest_id = marker.read_text(encoding="utf-8").strip()
            paths = run_paths(cwd, latest_id)
        except (OSError, ValueError):
            paths = None
        if paths is not None and paths.root.is_dir():
            return paths
        recent = list_runs(cwd, limit=1, pipeline=pipeline)
        return run_paths(cwd, recent[0]["run_id"]) if recent else None
    recent = list_runs(cwd, limit=1)
    return run_paths(cwd, recent[0]["run_id"]) if recent else None


def list_runs(
    cwd: Path,
    limit: int = 20,
    *,
    pipeline: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    base = runs_dir(cwd)
    if not base.is_dir():
        return []
    result: list[dict[str, Any]] = []
    roots = sorted((path for path in base.iterdir() if path.is_dir()), key=lambda path: path.name, reverse=True)
    for root in roots:
        try:
            paths = run_paths(cwd, root.name)
        except ValueError:
            continue
        manifest = read_json(paths.manifest) or {}
        status_payload = read_json(paths.status) or {}
        item = {
            "schema_version": RUN_SCHEMA_VERSION,
            "run_id": paths.run_id,
            "pipeline": status_payload.get("pipeline") or manifest.get("pipeline"),
            "status": _effective_status(paths, status_payload),
            "started_at": status_payload.get("started_at") or manifest.get("started_at"),
            "finished_at": status_payload.get("finished_at"),
            "log": str(paths.log),
        }
        if pipeline is not None and item["pipeline"] != pipeline:
            continue
        if status is not None and item["status"] != status:
            continue
        result.append(item)
    result.sort(key=lambda item: (str(item["started_at"] or ""), item["run_id"]), reverse=True)
    return result[: max(0, limit)]


def latest_marker(cwd: Path, pipeline: str) -> Path:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", pipeline).strip("-.") or "pipeline"
    return runs_dir(cwd) / f"latest-{slug}"


def set_latest_run(cwd: Path, pipeline: str, run_id: str) -> None:
    marker = latest_marker(cwd, pipeline)
    marker.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(marker, run_id + "\n")


def write_exit_code(path: Path, exit_code: int) -> None:
    write_text_atomic(path, f"{exit_code}\n")


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def write_text_atomic(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


class _TeeStream:
    """Text stream forwarding writes to the terminal and the run log recorder."""

    def __init__(self, original: Any, recorder: RunOutputRecorder) -> None:
        self._original = original
        self._recorder = recorder

    def write(self, text: str) -> int:
        written = self._original.write(text)
        self._recorder.record(text)
        return written if isinstance(written, int) else len(text)

    def flush(self) -> None:
        self._original.flush()

    def isatty(self) -> bool:
        return bool(self._original.isatty())

    def __getattr__(self, name: str) -> Any:
        return getattr(self._original, name)


class RunOutputRecorder:
    """Run-scoped thread-safe tee of CDT-owned stdout/stderr into ``output.log``.

    Writes keep flowing to the original terminal streams; only the saved copy
    passes through :class:`StreamingRedactor`. A lock serializes concurrent
    writes (parallel branches) so saved lines never interleave mid-line, and
    :meth:`close` flushes the redaction remainder on success, failure and
    interrupt alike. Logging failures never break the run.
    """

    def __init__(self, log_path: Path, redactor: SecretRedactor) -> None:
        self._log_path = log_path
        self._stream = StreamingRedactor(redactor)
        self._lock = threading.Lock()
        self._log: IO[str] | None = None
        self._log_disabled = False
        self._closed = False
        self._saved_stdout: Any = None
        self._saved_stderr: Any = None

    def install(self) -> None:
        """Start teeing the process-level stdout/stderr into the run log."""
        with self._lock:
            if self._closed:
                return
            self._open_log_locked()
            if self._log is None:
                return
            self._saved_stdout = sys.stdout
            self._saved_stderr = sys.stderr
            sys.stdout = _TeeStream(self._saved_stdout, self)
            sys.stderr = _TeeStream(self._saved_stderr, self)

    def record(self, text: str) -> None:
        """Append ``text`` to the redacted log copy; callable from any thread."""
        if not text:
            return
        with self._lock:
            self._append_locked(text)

    def record_line(self, text: str) -> None:
        """Append ``text`` as one complete line to the redacted log copy."""
        self.record(text.rstrip("\n") + "\n")

    def close(self) -> None:
        """Restore terminal streams and flush the redaction remainder; idempotent."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            log = self._log
            self._log = None
            if log is not None:
                try:
                    log.write(self._stream.feed("", final=True))
                except Exception:
                    pass
        if self._saved_stdout is not None:
            sys.stdout = self._saved_stdout
            self._saved_stdout = None
        if self._saved_stderr is not None:
            sys.stderr = self._saved_stderr
            self._saved_stderr = None
        if log is not None:
            try:
                log.close()
            except Exception:
                pass

    def _append_locked(self, text: str) -> None:
        if self._closed:
            return
        self._open_log_locked()
        if self._log is None:
            return
        try:
            self._log.write(self._stream.feed(text))
            self._log.flush()
        except Exception:
            pass

    def _open_log_locked(self) -> None:
        if self._log is not None or self._log_disabled:
            return
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log = self._log_path.open("a", encoding="utf-8")
        except OSError:
            self._log_disabled = True


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _effective_status(paths: RunPaths, status: dict[str, Any]) -> str:
    exit_code = _read_int(paths.exit)
    recorded = status.get("status")
    if exit_code == 0:
        return "success"
    if exit_code is not None:
        return "cancelled" if recorded == "cancelled" else "failed"
    pid = _read_int(paths.pid)
    if pid is not None:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return "stale"
        except PermissionError:
            pass
        return "running"
    return str(recorded or "unknown")


def _read_int(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _git_value(cwd: Path, args: list[str]) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired, TypeError, AttributeError):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None
