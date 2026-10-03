import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from threading import RLock
from typing import Any

import typer

from ..artifacts import BuildArtifact
from ..redaction import SecretRedactor
from ..runner import CommandRunner
from .values import ScopedValues


def _synchronized(method):
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self._status_lock:
            return method(self, *args, **kwargs)

    return locked


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
    values: ScopedValues | dict[str, str] = field(default_factory=ScopedValues)
    inputs: dict[str, str] = field(default_factory=dict)
    status_file: Path | None = None
    mirror_status_file: Path | None = None
    run_dir: Path | None = None
    run_id: str | None = None
    current_step: str | None = None
    completed_steps: list[str] = field(default_factory=list)
    skipped_steps: list[str] = field(default_factory=list)
    step_decisions: dict[str, str] = field(default_factory=dict)
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
    values_groups: dict[str, Any] = field(default_factory=dict)
    _rollback_snapshots: dict[Path, bytes] = field(default_factory=dict, repr=False)
    _status_lock: Any = field(default_factory=RLock, repr=False)
    _redactor: SecretRedactor = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._redactor = SecretRedactor.from_env(self.env)
        if not isinstance(self.values, ScopedValues):
            self.values = ScopedValues(self.values)

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
        with self._status_lock:
            if name in self.artifacts:
                raise typer.BadParameter(f"Duplicate pipeline artifact: {name}")
            self.artifacts[name] = artifact

    @_synchronized
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

    @_synchronized
    def mark_status_started(self) -> None:
        self.started_at = _now()
        self.write_status("running")

    @_synchronized
    def mark_step_started(self, step_id: str) -> None:
        self.current_step = step_id
        self.write_status("running")

    @_synchronized
    def mark_step_completed(self, step_id: str) -> None:
        self.current_step = None
        if step_id not in self.completed_steps:
            self.completed_steps.append(step_id)
        self.write_status("running")

    def should_skip_step(self, step_id: str) -> bool:
        return self.step_decisions.get(step_id) == "skip" or (self.skip_completed and step_id in self.completed_steps)

    @_synchronized
    def mark_parallel_step_started(self, step_id: str) -> None:
        if step_id not in self.running_steps:
            self.running_steps.append(step_id)
        self.write_status("running")

    @_synchronized
    def mark_parallel_step_completed(self, step_id: str) -> None:
        branch_id = self.values.branch_id
        if branch_id is not None:
            for group in self.values_groups.values():
                if branch_id in group["branches"]:
                    group["branches"][branch_id] = self.values.copy()
        if step_id in self.running_steps:
            self.running_steps.remove(step_id)
        if step_id not in self.parallel_completed:
            self.parallel_completed.append(step_id)
        if step_id not in self.completed_steps:
            self.completed_steps.append(step_id)
        self.write_status("running")

    @_synchronized
    def mark_parallel_step_failed(self, step_id: str, error: str) -> None:
        if step_id in self.running_steps:
            self.running_steps.remove(step_id)
        failure = f"{step_id}: {error}"
        if failure not in self.parallel_failed:
            self.parallel_failed.append(failure)
        self.write_status("running")

    @_synchronized
    def mark_status_failed(self, step_id: str, error: str) -> None:
        self.current_step = None
        self.failed_step = step_id
        self.error = error
        self.finished_at = _now()
        self.write_status("failed")

    @_synchronized
    def mark_status_success(self) -> None:
        self.current_step = None
        self.finished_at = _now()
        self.write_status("success")

    @_synchronized
    def begin_values_group(self, group_id: str, branch_ids: list[str]) -> tuple[dict, dict]:
        if group_id not in self.values_groups:
            base = deepcopy(self.values.root)
            self.values_groups[group_id] = {
                "base": base,
                "branches": {branch_id: deepcopy(base) for branch_id in branch_ids},
            }
        group = self.values_groups[group_id]
        self.write_status("running")
        return deepcopy(group["base"]), deepcopy(group["branches"])

    @_synchronized
    def merge_values_group(self, group_id: str, base: dict, branches: dict) -> None:
        self.values.merge(base, branches)
        del self.values_groups[group_id]
        if group_id not in self.completed_steps:
            self.completed_steps.append(group_id)
        self.write_status("running")

    def restore_values(self, state: Any) -> None:
        if not isinstance(state, dict) or state.get("version") != 1 or state.get("restorable") is not True:
            raise typer.BadParameter("Resume values checkpoint is unsupported or not restorable (redacted data).")
        if not isinstance(state.get("root"), dict) or not isinstance(state.get("groups"), dict):
            raise typer.BadParameter("Invalid resume values checkpoint")
        for group in state["groups"].values():
            if (
                not isinstance(group, dict)
                or not isinstance(group.get("base"), dict)
                or not isinstance(group.get("branches"), dict)
                or not all(isinstance(branch, dict) for branch in group["branches"].values())
            ):
                raise typer.BadParameter("Invalid resume branch checkpoint")
        self.values.root = deepcopy(state["root"])
        self.values_groups = deepcopy(state["groups"])

    def _redact_checkpoint(self, value):
        if isinstance(value, dict):
            return {self.redact(key): self._redact_checkpoint(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._redact_checkpoint(item) for item in value]
        return self._redactor.redact_data(value)

    def values_checkpoint(self) -> dict:
        return {
            "version": 1,
            "restorable": True,
            "root": deepcopy(self.values.root),
            "groups": deepcopy(self.values_groups),
        }

    def write_status(self, status: str) -> None:
        if self.status_file is None:
            return
        with self._status_lock:
            payload: dict[str, Any] = {
                "schema_version": 1,
                "values_state": self.values_checkpoint(),
                "run_id": self.run_id,
                "status": status,
                "pipeline": self.pipeline_name,
                "current_step": self.current_step,
                "completed_steps": list(self.completed_steps),
                "skipped_steps": list(self.skipped_steps),
                "step_decisions": dict(self.step_decisions),
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
                checkpoint = payload["values_state"]
                # Redact keys too: arbitrary plugin keys can themselves contain secrets.
                safe_checkpoint = self._redact_checkpoint(checkpoint)
                safe_checkpoint["restorable"] = safe_checkpoint == checkpoint
                sanitized["values_state"] = safe_checkpoint
                serialized = json.dumps(sanitized, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
                tmp.write_text(serialized, encoding="utf-8")
                tmp.replace(path)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _snapshot_name(path: Path) -> str:
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:12]
    return f"{digest}-{path.name}"
