from collections.abc import Callable

import typer

from ..pipeline import PipelineContext
from ..services import appstore_state
from ..services.appstore import _complete_testflight_after_upload, _upload_testflight, _upload_testflight_ipa
from ..services.appstore_state import AppStoreStateError
from ..sounds import _play_fail_sound

ChangelogProvider = str | Callable[[PipelineContext], str]


def _record_uploaded_build(ctx: PipelineContext) -> None:
    """Persist the identification of the build a full TestFlight cycle just made ready.

    Only the full ``appstore.upload_testflight`` and the
    ``appstore.complete_testflight`` steps call this — the upload-only step
    must not declare the build ready. A failed save is an explicit step error:
    the result must not be presented as recorded when it is not. Without
    ``IOS_BUNDLE_ID`` the app (and therefore the record key) is unknown, which
    cannot happen after a real upload or completion; the step then only warns
    that no record was written.
    """
    bundle_id = ctx.env.get("IOS_BUNDLE_ID", "").strip()
    if not bundle_id:
        typer.echo(
            "==> WARNING: IOS_BUNDLE_ID is not set; the completed build was not recorded",
            err=True,
        )
        return
    try:
        record = appstore_state.save_upload_record(ctx.cwd, bundle_id, ctx.new_version or "")
    except AppStoreStateError as exc:
        typer.echo(f"Failed to record the completed TestFlight build: {exc}", err=True)
        _play_fail_sound(ctx.env, ctx.cwd)
        raise typer.Exit(code=1) from exc
    typer.echo(f"==> Recorded uploaded build {record.marketing_version}+{record.build_number} for {bundle_id}")


class UploadTestFlightIpaStep:
    name = "appstore.upload_testflight_ipa"

    def __init__(self, artifact: str = "ipa"):
        self.artifact = artifact

    def run(self, ctx: PipelineContext) -> None:
        artifact = ctx.artifact(self.artifact)
        typer.echo(f"==> Uploading to TestFlight (upload-only): {artifact.path}")
        if _upload_testflight_ipa(artifact.path, ctx.env) != 0:
            typer.echo("TestFlight upload failed", err=True)
            _play_fail_sound(ctx.env, ctx.cwd)
            raise typer.Exit(code=1)


class CompleteTestFlightStep:
    name = "appstore.complete_testflight"

    def __init__(self, changelog: ChangelogProvider = "dev build"):
        self.changelog = changelog

    def run(self, ctx: PipelineContext) -> None:
        if not ctx.new_version:
            raise typer.BadParameter("Missing pipeline value: new_version")

        changelog = self.changelog(ctx) if callable(self.changelog) else self.changelog
        typer.echo(f"==> Completing TestFlight upload for build {ctx.new_version.rsplit('+', 1)[-1]}")
        if _complete_testflight_after_upload(ctx.env, changelog, ctx.new_version) != 0:
            typer.echo("TestFlight completion failed", err=True)
            _play_fail_sound(ctx.env, ctx.cwd)
            raise typer.Exit(code=1)
        _record_uploaded_build(ctx)


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
        _record_uploaded_build(ctx)
