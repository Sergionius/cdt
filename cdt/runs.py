from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__

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
) -> RunPaths:
    run_id = run_id or generate_run_id(pipeline)
    paths = run_paths(cwd, run_id)
    paths.root.mkdir(parents=True, exist_ok=False)
    paths.log.touch()
    manifest = {
        "schema_version": RUN_SCHEMA_VERSION,
        "run_id": run_id,
        "pipeline": pipeline,
        "ids": list(ids or []),
        "cdt_version": __version__,
        "project_root": str(cwd.resolve()),
        "git_commit": _git_value(cwd, ["rev-parse", "HEAD"]),
        "git_branch": _git_value(cwd, ["branch", "--show-current"]),
        "started_at": now(),
        "command": command or ["cdt", "run", pipeline],
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
) -> RunPaths:
    if run_id is not None:
        paths = run_paths(cwd, run_id)
        if paths.root.exists():
            return paths
    return create_run(cwd, pipeline, ids=ids, run_id=run_id, command=command, detached=detached)


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
        paths = run_paths(cwd, run_id)
        return paths if paths.root.is_dir() else None
    if pipeline:
        marker = latest_marker(cwd, pipeline)
        try:
            latest_id = marker.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        paths = run_paths(cwd, latest_id)
        return paths if paths.root.is_dir() else None
    return None


def list_runs(cwd: Path, limit: int = 20) -> list[dict[str, Any]]:
    base = runs_dir(cwd)
    if not base.is_dir():
        return []
    result: list[dict[str, Any]] = []
    roots = sorted((path for path in base.iterdir() if path.is_dir()), key=lambda path: path.name, reverse=True)
    for root in roots[: max(0, limit)]:
        paths = run_paths(cwd, root.name)
        manifest = read_json(paths.manifest) or {}
        status = read_json(paths.status) or {}
        result.append(
            {
                "schema_version": RUN_SCHEMA_VERSION,
                "run_id": paths.run_id,
                "pipeline": status.get("pipeline") or manifest.get("pipeline"),
                "status": _effective_status(paths, status),
                "started_at": status.get("started_at") or manifest.get("started_at"),
                "finished_at": status.get("finished_at"),
                "log": str(paths.log),
            }
        )
    return result


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
