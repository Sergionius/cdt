from collections.abc import Callable

import typer

from ..pipeline import PipelineContext
from ..services.appstore import _upload_testflight
from ..sounds import _play_fail_sound

ChangelogProvider = str | Callable[[PipelineContext], str]


class UploadTestFlightStep:
    name = "appstore.upload_testflight"

    def __init__(self, changelog: ChangelogProvider = "dev build", artifact: str = "ipa"):
        self.changelog = changelog
        self.artifact = artifact

    def run(self, ctx: PipelineContext) -> None:
        artifact = ctx.artifact(self.artifact)
        if not ctx.new_version:
            raise typer.BadParameter("Missing pipeline value: new_version")

        changelog = self.changelog(ctx) if callable(self.changelog) else self.changelog
        typer.echo(f"==> Uploading to TestFlight: {artifact.path}")
        if _upload_testflight(artifact.path, ctx.env, changelog, ctx.new_version) != 0:
            typer.echo("TestFlight upload failed", err=True)
            _play_fail_sound(ctx.env, ctx.cwd)
            raise typer.Exit(code=1)
