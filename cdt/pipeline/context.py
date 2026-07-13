import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

import typer

from ..artifacts import BuildArtifact
from ..runner import CommandRunner


@dataclass
class PipelineContext:
    cwd: Path
    env: dict[str, str]
    runner: CommandRunner
    ids: list[str] = field(default_factory=list)
    pipeline_name: str | None = None
    old_version: str | None = None
    new_version: str | None = None
    artifacts: dict[str, BuildArtifact] = field(default_factory=dict)
    values: dict[str, str] = field(default_factory=dict)
    status_file: Path | None = None
    current_step: str | None = None
    completed_steps: list[str] = field(default_factory=list)
    failed_step: str | None = None
    error: str | None = None
    running_steps: list[str] = field(default_factory=list)
    parallel_completed: list[str] = field(default_factory=list)
    parallel_failed: list[str] = field(default_factory=list)
    skip_completed: bool = False
    started_at: str | None = None
    finished_at: str | None = None
    _artifact_lock: Lock = field(default_factory=Lock, repr=False)
    _status_lock: Lock = field(default_factory=Lock, repr=False)

    def env_value(self, key: str, fallback_key: str | None = None, default: str = "") -> str:
        value = self.env.get(key, "").strip()
        if value:
            return value
        if fallback_key:
            fallback = self.env.get(fallback_key, "").strip()
            if fallback:
                return fallback
        return default

    def require_env(self, key: str, fallback_key: str | None = None) -> str:
        value = self.env_value(key, fallback_key)
        if not value:
            raise typer.BadParameter(f"Missing {key} in project .env")
        return value

    def project_path(self, raw_path: str) -> Path:
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = self.cwd / path
        return path

    def register_artifact(self, name: str, artifact: BuildArtifact) -> None:
        with self._artifact_lock:
            if name in self.artifacts:
                raise typer.BadParameter(f"Duplicate pipeline artifact: {name}")
            self.artifacts[name] = artifact

    def artifact(self, name: str) -> BuildArtifact:
        try:
            return self.artifacts[name]
        except KeyError as exc:
            raise typer.BadParameter(f"Missing pipeline artifact: {name}") from exc

    def mark_status_started(self) -> None:
        self.started_at = _now()
        self.write_status("running")

    def mark_step_started(self, step_id: str) -> None:
        self.current_step = step_id
        self.write_status("running")

    def mark_step_completed(self, step_id: str) -> None:
        self.current_step = None
        if step_id not in self.completed_steps:
            self.completed_steps.append(step_id)
        self.write_status("running")

    def should_skip_step(self, step_id: str) -> bool:
        return self.skip_completed and step_id in self.completed_steps

    def mark_parallel_step_started(self, step_id: str) -> None:
        if step_id not in self.running_steps:
            self.running_steps.append(step_id)
        self.write_status("running")

    def mark_parallel_step_completed(self, step_id: str) -> None:
        if step_id in self.running_steps:
            self.running_steps.remove(step_id)
        if step_id not in self.parallel_completed:
            self.parallel_completed.append(step_id)
        if step_id not in self.completed_steps:
            self.completed_steps.append(step_id)
        self.write_status("running")

    def mark_parallel_step_failed(self, step_id: str, error: str) -> None:
        if step_id in self.running_steps:
            self.running_steps.remove(step_id)
        failure = f"{step_id}: {error}"
        if failure not in self.parallel_failed:
            self.parallel_failed.append(failure)
        self.write_status("running")

    def mark_status_failed(self, step_id: str, error: str) -> None:
        self.current_step = None
        self.failed_step = step_id
        self.error = error
        self.finished_at = _now()
        self.write_status("failed")

    def mark_status_success(self) -> None:
        self.current_step = None
        self.finished_at = _now()
        self.write_status("success")

    def write_status(self, status: str) -> None:
        if self.status_file is None:
            return
        with self._status_lock:
            payload: dict[str, Any] = {
                "status": status,
                "pipeline": self.pipeline_name,
                "current_step": self.current_step,
                "completed_steps": list(self.completed_steps),
                "failed_step": self.failed_step,
                "error": self.error,
                "running_steps": list(self.running_steps),
                "parallel_completed": list(self.parallel_completed),
                "parallel_failed": list(self.parallel_failed),
                "artifacts": [artifact.to_json(name) for name, artifact in sorted(self.artifacts.items())],
                "old_version": self.old_version,
                "new_version": self.new_version,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "updated_at": _now(),
            }
            self.status_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.status_file.with_suffix(self.status_file.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            tmp.replace(self.status_file)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
