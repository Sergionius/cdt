import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

import typer

from ..artifacts import BuildArtifact
from ..redaction import SecretRedactor
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
    inputs: dict[str, str] = field(default_factory=dict)
    status_file: Path | None = None
    mirror_status_file: Path | None = None
    run_dir: Path | None = None
    run_id: str | None = None
    current_step: str | None = None
    completed_steps: list[str] = field(default_factory=list)
    failed_step: str | None = None
    error: str | None = None
    running_steps: list[str] = field(default_factory=list)
    parallel_completed: list[str] = field(default_factory=list)
    parallel_failed: list[str] = field(default_factory=list)
    skip_completed: bool = False
    resume_from: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    rolled_back: bool = False
    rollback_closed: bool = False
    release_results: dict[str, str] = field(default_factory=dict)
    _rollback_snapshots: dict[Path, bytes] = field(default_factory=dict, repr=False)
    _artifact_lock: Lock = field(default_factory=Lock, repr=False)
    _status_lock: Lock = field(default_factory=Lock, repr=False)
    _redactor: SecretRedactor = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._redactor = SecretRedactor.from_env(self.env)

    def redact(self, text: str) -> str:
        return self._redactor.redact(text)

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

    def register_release_results(self, results: dict[str, str]) -> None:
        """Record confirmed GitHub Release/PyPI URLs and results in the context and status file."""
        for key, value in results.items():
            normalized = str(value).strip()
            if normalized:
                self.release_results[key] = normalized
        self.write_status("running")

    def register_rollback_file(self, path: Path) -> None:
        """Snapshot the exact current content of a release file for pre-commit rollback."""
        if self.rollback_closed:
            raise typer.BadParameter(
                f"Cannot snapshot {path}: the release rollback boundary is closed after the release commit"
            )
        resolved = path.expanduser().resolve()
        if not resolved.is_file():
            raise typer.BadParameter(f"Cannot snapshot missing release file: {path}")
        if resolved not in self._rollback_snapshots:
            self._rollback_snapshots[resolved] = resolved.read_bytes()
            self._persist_snapshot(resolved)

    def close_rollback_boundary(self) -> None:
        """Close the pre-commit rollback boundary after the release commit succeeded."""
        self.rollback_closed = True

    @property
    def rollback_pending(self) -> bool:
        """True while release files are modified and the release commit has not succeeded yet."""
        return bool(self._rollback_snapshots) and not self.rollback_closed

    def perform_rollback(self) -> list[Path]:
        """Restore only the snapshotted release files; returns the restored paths."""
        restored: list[Path] = []
        for path, data in self._rollback_snapshots.items():
            path.write_bytes(data)
            restored.append(path)
        self._rollback_snapshots.clear()
        self.rolled_back = bool(restored)
        return restored

    def _persist_snapshot(self, path: Path) -> None:
        if self.run_dir is None:
            return
        snapshots_dir = self.run_dir / "snapshots"
        try:
            snapshots_dir.mkdir(parents=True, exist_ok=True)
            target = snapshots_dir / _snapshot_name(path)
            if not target.exists():
                target.write_bytes(self._rollback_snapshots[path])
        except OSError:
            # Snapshot persistence is best-effort; the in-memory copy stays authoritative.
            pass

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
                "schema_version": 1,
                "run_id": self.run_id,
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
                "inputs": dict(self.inputs),
                "old_version": self.old_version,
                "new_version": self.new_version,
                "release_results": dict(self.release_results),
                "rolled_back": self.rolled_back,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "updated_at": _now(),
            }
            paths = [self.status_file]
            if self.mirror_status_file is not None and self.mirror_status_file != self.status_file:
                paths.append(self.mirror_status_file)
            for path in paths:
                if path is None:
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(f".{path.name}.tmp")
                sanitized = self._redactor.redact_data(payload)
                serialized = json.dumps(sanitized, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
                tmp.write_text(serialized, encoding="utf-8")
                tmp.replace(path)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _snapshot_name(path: Path) -> str:
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:12]
    return f"{digest}-{path.name}"
