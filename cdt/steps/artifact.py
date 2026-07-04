import shutil
from pathlib import Path

import typer

from ..pipeline import PipelineContext


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
        destination = Path(self.destination).expanduser() if self.destination else Path.home() / "Downloads"
        if not destination.is_absolute():
            destination = ctx.cwd / destination
        destination.mkdir(parents=True, exist_ok=True)
        dst = destination / src.name
        shutil.copy2(src, dst)
        typer.echo(f"==> Copied artifact '{self.artifact}' to: {dst}")
