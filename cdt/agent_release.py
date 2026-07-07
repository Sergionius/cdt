import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def artifact_paths(pipeline: str) -> dict[str, Path]:
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


def start_release(pipeline: str, ids: list[str] | None = None) -> dict[str, Any]:
    ids = ids or []
    paths = artifact_paths(pipeline)
    paths["dir"].mkdir(parents=True, exist_ok=True)
    paths["exit"].unlink(missing_ok=True)

    cmd = [
        sys.executable,
        "-m",
        "cdt.agent_release_worker",
        "--pipeline",
        pipeline,
        "--log",
        str(paths["log"]),
        "--exit-file",
        str(paths["exit"]),
        "--status-file",
        str(paths["status"]),
    ]
    for task_id in ids:
        cmd.extend(["--id", task_id])

    process = subprocess.Popen(
        cmd,
        cwd=Path.cwd(),
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    paths["pid"].write_text(f"{process.pid}\n", encoding="utf-8")
    meta = {
        "pipeline": pipeline,
        "pid": process.pid,
        "ids": ids,
        "log": str(paths["log"]),
        "status_file": str(paths["status"]),
        "exit_file": str(paths["exit"]),
        "started_at": _now(),
        "command": ["cdt", "run", pipeline, *sum((["--id", value] for value in ids), [])],
    }
    _write_json(paths["meta"], meta)
    return release_status(pipeline)


def release_status(pipeline: str) -> dict[str, Any]:
    paths = artifact_paths(pipeline)
    pid = _read_pid(paths["pid"])
    exit_code = _read_exit(paths["exit"])
    running = _pid_running(pid) if pid is not None else False
    status_payload = _read_json(paths["status"])

    if exit_code == 0:
        status = "success"
    elif exit_code is not None:
        status = "failed"
    elif running:
        status = "running"
    elif pid is not None:
        status = "stale"
    else:
        status = "unknown"

    payload: dict[str, Any] = {
        "status": status,
        "pipeline": pipeline,
        "pid": pid,
        "exit_code": exit_code,
        "log": str(paths["log"]),
        "status_file": str(paths["status"]),
        "last_log_update": _mtime(paths["log"]),
    }
    if isinstance(status_payload, dict):
        status_keys = (
            "current_step",
            "completed_steps",
            "failed_step",
            "error",
            "artifacts",
            "started_at",
            "finished_at",
        )
        for key in status_keys:
            if key in status_payload:
                payload[key] = status_payload[key]
    return payload


def wait_for_release(
    pipeline: str,
    timeout_seconds: int | None = None,
    interval_seconds: float = 5.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None
    while True:
        payload = release_status(pipeline)
        if payload["status"] in {"success", "failed", "stale", "unknown"}:
            return payload
        if deadline is not None and time.monotonic() >= deadline:
            payload["status"] = "timeout"
            return payload
        time.sleep(interval_seconds)


def stop_release(pipeline: str, timeout_seconds: int = 30) -> dict[str, Any]:
    paths = artifact_paths(pipeline)
    pid = _read_pid(paths["pid"])
    if pid is None:
        payload = release_status(pipeline)
        payload["stop_result"] = "missing_pid"
        return payload
    if not _pid_running(pid):
        payload = release_status(pipeline)
        payload["stop_result"] = "not_running"
        return payload

    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        payload = release_status(pipeline)
        payload["stop_result"] = "not_running"
        return payload

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not _pid_running(pid):
            payload = release_status(pipeline)
            payload["stop_result"] = "terminated"
            return payload
        time.sleep(0.5)

    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    payload = release_status(pipeline)
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
    lines: list[str] = []
    for key, value in payload.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {item}")
        elif isinstance(value, dict):
            lines.append(f"{key}: {json.dumps(value, ensure_ascii=False, sort_keys=True)}")
        else:
            rendered = "null" if value is None else str(value)
            lines.append(f"{key}: {rendered}")
    return "\n".join(lines)


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
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
    except OSError:
        return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
