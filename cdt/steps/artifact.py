import shutil
from pathlib import Path

import typer

from ..artifacts import ArtifactKind
from ..pipeline import PipelineContext

ANDROID_ARTIFACT_PREFIX_ENV = "ANDROID_ARTIFACT_PREFIX"
_ANDROID_PREFIX_KINDS = (ArtifactKind.APK, ArtifactKind.AAB)


def _destination_name(ctx: PipelineContext, kind: ArtifactKind, src: Path) -> str:
    """Resolve the copy name, optionally prefixing Android artifacts from the environment."""
    if kind not in _ANDROID_PREFIX_KINDS:
        return src.name
    prefix = ctx.env_value(ANDROID_ARTIFACT_PREFIX_ENV)
    if not prefix:
        return src.name
    if "/" in prefix or "\\" in prefix or any(ord(char) < 32 or ord(char) == 127 for char in prefix):
        raise typer.BadParameter(
            f"{ANDROID_ARTIFACT_PREFIX_ENV} must not contain path separators or control characters"
        )
    return f"{prefix}_{src.name}"


class CopyArtifactToDownloadsStep:
    name = "artifact.copy_to_downloads"

    def __init__(self, artifact: str, destination: str | None = None):
        self.artifact = artifact
        self.destination = destination

    def run(self, ctx: PipelineContext) -> None:
        build_artifact = ctx.artifact(self.artifact)
        src = build_artifact.path
        if src.is_dir():
            raise typer.BadParameter(f"Artifact path is a directory, expected file: {src}")
        if not src.exists():
            raise typer.BadParameter(f"Artifact file not found: {src}")
        dst_name = _destination_name(ctx, build_artifact.kind, src)
        destination = Path(self.destination).expanduser() if self.destination else Path.home() / "Downloads"
        if not destination.is_absolute():
            destination = ctx.cwd / destination
        destination.mkdir(parents=True, exist_ok=True)
        dst = destination / dst_name
        shutil.copy2(src, dst)
        typer.echo(f"==> Copied artifact '{self.artifact}' to: {dst}")
