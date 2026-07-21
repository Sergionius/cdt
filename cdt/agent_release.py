from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from .runs import (
    RUN_SCHEMA_VERSION,
    RunPaths,
    create_run,
    now,
    read_json,
    resolve_run,
    write_exit_code,
    write_json_atomic,
    write_text_atomic,
)


def artifact_paths(pipeline: str) -> dict[str, Path]:
    """Return legacy pipeline-named paths for compatibility with older callers."""
    base = Path.cwd() / ".cdt"
    stem = f"agent-release-{pipeline}"
    return {
        "dir": base,
        "log": base / f"{stem}.log",
        "pid": base / f"{stem}.pid",
        "meta": base / f"{stem}.meta.json",
        "exit": base / f"{stem}.exit",
        "status": base / f"{stem}.status.json",
    }


def start_release(
    pipeline: str,
    ids: list[str] | None = None,
    *,
    run_id: str | None = None,
    confirm: str | None = None,
) -> dict[str, Any]:
    ids = ids or []
    cwd = Path.cwd()
    command = ["cdt", "run", pipeline]
    for task_id in ids:
        command.extend(["--id", task_id])
    if confirm is not None:
        command.extend(["--confirm", confirm])
    paths = create_run(cwd, pipeline, ids=ids, run_id=run_id, command=command, detached=True)

    worker_cmd = [
        sys.executable,
        "-m",
        "cdt.agent_release_worker",
        "--pipeline",
        pipeline,
        "--run-id",
        paths.run_id,
        "--log",
        str(paths.log),
        "--exit-file",
        str(paths.exit),
        "--status-file",
        str(paths.status),
    ]
    for task_id in ids:
        worker_cmd.extend(["--id", task_id])
    if confirm is not None:
        worker_cmd.extend(["--confirm", confirm])

    try:
        process = subprocess.Popen(
            worker_cmd,
            cwd=cwd,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        payload = read_json(paths.status) or {}
        payload.update({"status": "failed", "error": f"Failed to start release worker: {exc}", "finished_at": now()})
        payload["updated_at"] = now()
        write_json_atomic(paths.status, payload)
        write_exit_code(paths.exit, 1)
        return release_status(run_id=paths.run_id)

    write_text_atomic(paths.pid, f"{process.pid}\n")
    manifest = read_json(paths.manifest) or {}
    manifest.update({"pid": process.pid, "worker_command": worker_cmd})
    write_json_atomic(paths.manifest, manifest)
    return release_status(run_id=paths.run_id)


def release_status(pipeline: str | None = None, *, run_id: str | None = None) -> dict[str, Any]:
    paths, legacy = _resolve_paths(pipeline=pipeline, run_id=run_id)
    if paths is None:
        return {
            "schema_version": RUN_SCHEMA_VERSION,
            "status": "unknown",
            "run_id": run_id,
            "pipeline": pipeline,
            "pid": None,
            "exit_code": None,
            "log": None,
            "status_file": None,
            "last_log_update": None,
        }

    pid = _read_pid(paths.pid)
    exit_code = _read_exit(paths.exit)
    running = _pid_running(pid) if pid is not None else False
    status_payload = read_json(paths.status) or {}
    manifest = read_json(paths.manifest) or {}
    recorded_status = status_payload.get("status")

    if exit_code == 0:
        status = "success"
    elif exit_code is not None:
        status = "failed" if recorded_status != "cancelled" else "cancelled"
    elif running:
        status = "running"
    elif recorded_status in {"success", "failed", "cancelled", "blocked"}:
        status = str(recorded_status)
    elif pid is not None:
        status = "stale"
    elif recorded_status == "queued":
        status = "queued"
    else:
        status = "unknown"

    payload: dict[str, Any] = {
        "schema_version": RUN_SCHEMA_VERSION,
        "status": status,
        "run_id": None if legacy else paths.run_id,
        "pipeline": status_payload.get("pipeline") or manifest.get("pipeline") or pipeline,
        "pid": pid,
        "exit_code": exit_code,
        "log": str(paths.log),
        "status_file": str(paths.status),
        "last_log_update": _mtime(paths.log),
    }
    status_keys = (
        "current_step",
        "completed_steps",
        "running_steps",
        "parallel_completed",
        "parallel_failed",
        "failed_step",
        "error",
        "artifacts",
        "old_version",
        "new_version",
        "started_at",
        "finished_at",
        "updated_at",
    )
    for key in status_keys:
        if key in status_payload:
            payload[key] = status_payload[key]
    return payload


def wait_for_release(
    pipeline: str | None = None,
    timeout_seconds: int | None = None,
    interval_seconds: float = 5.0,
    *,
    run_id: str | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None
    while True:
        payload = release_status(pipeline, run_id=run_id)
        if payload["status"] in {"success", "failed", "cancelled", "blocked", "stale", "unknown"}:
            return payload
        if deadline is not None and time.monotonic() >= deadline:
            payload["wait_status"] = "timeout"
            return payload
        time.sleep(interval_seconds)


def stop_release(
    pipeline: str | None = None,
    timeout_seconds: int = 30,
    *,
    run_id: str | None = None,
) -> dict[str, Any]:
    paths, _ = _resolve_paths(pipeline=pipeline, run_id=run_id)
    if paths is None:
        payload = release_status(pipeline, run_id=run_id)
        payload["stop_result"] = "missing_pid"
        return payload
    manifest = read_json(paths.manifest) or {}
    if manifest.get("detached") is False:
        payload = release_status(pipeline, run_id=run_id)
        payload["stop_result"] = "not_detached"
        return payload
    pid = _read_pid(paths.pid)
    if pid is None:
        payload = release_status(pipeline, run_id=run_id)
        payload["stop_result"] = "missing_pid"
        return payload
    if not _pid_running(pid):
        payload = release_status(pipeline, run_id=run_id)
        payload["stop_result"] = "not_running"
        return payload

    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        payload = release_status(pipeline, run_id=run_id)
        payload["stop_result"] = "not_running"
        return payload

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not _pid_running(pid):
            _mark_cancelled(paths)
            payload = release_status(pipeline, run_id=run_id)
            payload["stop_result"] = "terminated"
            return payload
        time.sleep(0.5)

    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    _mark_cancelled(paths)
    payload = release_status(pipeline, run_id=run_id)
    payload["stop_result"] = "killed"
    return payload


def parse_duration(value: str) -> int:
    raw = value.strip().lower()
    if raw.endswith("ms"):
        return max(1, int(raw[:-2]) // 1000)
    if raw.endswith("s"):
        return int(raw[:-1])
    if raw.endswith("m"):
        return int(raw[:-1]) * 60
    if raw.endswith("h"):
        return int(raw[:-1]) * 3600
    return int(raw)


def format_yamlish(payload: dict[str, Any]) -> str:
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).strip()


def _resolve_paths(*, pipeline: str | None, run_id: str | None) -> tuple[RunPaths | None, bool]:
    cwd = Path.cwd()
    paths = resolve_run(cwd, run_id=run_id, pipeline=pipeline)
    if paths is not None:
        return paths, False
    if pipeline is None:
        return None, False
    legacy = artifact_paths(pipeline)
    if not any(legacy[key].exists() for key in ("pid", "exit", "status", "log")):
        return None, False
    return (
        RunPaths(
            run_id=pipeline,
            root=legacy["dir"],
            manifest=legacy["meta"],
            status=legacy["status"],
            log=legacy["log"],
            exit=legacy["exit"],
            pid=legacy["pid"],
        ),
        True,
    )


def _mark_cancelled(paths: RunPaths) -> None:
    payload = read_json(paths.status) or {}
    payload.update({"status": "cancelled", "finished_at": now(), "updated_at": now()})
    write_json_atomic(paths.status, payload)
    write_exit_code(paths.exit, 130)


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _read_exit(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _pid_running(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _mtime(path: Path) -> str | None:
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(path.stat().st_mtime))
    except OSError:
        return None
