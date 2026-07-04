from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock

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
    _artifact_lock: Lock = field(default_factory=Lock, repr=False)

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
