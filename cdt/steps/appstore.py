from collections.abc import Callable
from typing import Any

import typer

from ..pipeline import PipelineContext
from ..services import appstore, appstore_state
from ..services.appstore import _complete_testflight_after_upload, _upload_testflight, _upload_testflight_ipa
from ..services.appstore_review import RELEASE_MODES, AppStoreReviewError
from ..services.appstore_state import AppStoreReviewOperation, AppStoreStateError, ReviewIntent
from ..sounds import _play_fail_sound

ChangelogProvider = str | Callable[[PipelineContext], str]

SUBMIT_REVIEW_STEP_NAME = "appstore.submit_review"


def _option_error(detail: str) -> typer.BadParameter:
    return typer.BadParameter(detail, param_hint=SUBMIT_REVIEW_STEP_NAME)


def _validate_whats_new(value: Any) -> dict[str, str]:
    """Require a non-empty mapping of non-empty locales to non-empty texts."""
    if not isinstance(value, dict):
        raise _option_error(
            f"{SUBMIT_REVIEW_STEP_NAME} option 'whats_new' must map locales to non-empty strings, "
            f"got {type(value).__name__}"
        )
    if not value:
        raise _option_error(
            f"{SUBMIT_REVIEW_STEP_NAME} option 'whats_new' must not be empty; "
            "provide at least one localized text"
        )
    whats_new: dict[str, str] = {}
    for locale, text in value.items():
        if not isinstance(locale, str) or not locale.strip():
            raise _option_error(
                f"{SUBMIT_REVIEW_STEP_NAME} option 'whats_new' has an empty or non-string locale: {locale!r}"
            )
        if not isinstance(text, str) or not text.strip():
            raise _option_error(
                f"{SUBMIT_REVIEW_STEP_NAME} option 'whats_new[{locale!r}]' must be a non-empty localized text"
            )
        whats_new[locale.strip()] = text.strip()
    return whats_new


def _validate_release_mode(value: Any) -> str:
    if not isinstance(value, str) or value.strip() not in RELEASE_MODES:
        raise _option_error(
            f"{SUBMIT_REVIEW_STEP_NAME} option 'release_mode' must be 'manual' or 'automatic' "
            f"(no default is applied), got {value!r}"
        )
    return value.strip()


def _validate_phased_release(value: Any) -> bool:
    # A real boolean only: YAML "false" arrives here as the string 'false'
    # after ${inputs.*} interpolation, which must never count as a truthy
    # phased-release choice.
    if not isinstance(value, bool):
        raise _option_error(
            f"{SUBMIT_REVIEW_STEP_NAME} option 'phased_release' must be a real boolean (true/false), "
            f"got {value!r}"
        )
    return value


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


class SubmitReviewStep:
    """Submit the completed build of the app for App Store review.

    A thin adapter over the service layer: the app comes from ``IOS_BUNDLE_ID``,
    the exact build comes from the Task 2 mechanism (``ctx.new_version`` first,
    otherwise the persisted upload record) and every remote mutation, recovery
    and confirmation lives in :class:`AppStoreReviewOperation`. No artifact, no
    Flutter/Xcode tooling and no manual app/version/build input is involved.

    Production safety does not depend on a prompt inside the step: the step
    requires ``risk: production`` (enforced by pipeline validation) and the
    existing exact direct/detached CLI confirmation.
    """

    name = SUBMIT_REVIEW_STEP_NAME

    def __init__(self, whats_new: dict[str, str], release_mode: str, phased_release: bool):
        # All three options are mandatory and validated at construction time,
        # which happens after ${inputs.*} interpolation: a submission can never
        # run with an unspecified text, release mode or phased-release choice.
        self.whats_new = _validate_whats_new(whats_new)
        self.release_mode = _validate_release_mode(release_mode)
        self.phased_release = _validate_phased_release(phased_release)

    def run(self, ctx: PipelineContext) -> None:
        bundle_id = ctx.env.get("IOS_BUNDLE_ID", "").strip()
        if not bundle_id:
            raise _option_error(f"{SUBMIT_REVIEW_STEP_NAME} requires IOS_BUNDLE_ID in the project .env")

        # Credentials are created per call from the final pipeline env; nothing
        # here reads or mutates the global environment.
        client = appstore._AscClient(ctx.env)
        try:
            # Selection (current context wins, otherwise the persisted record)
            # plus an immediate ASC re-verification of the chosen build: the
            # local record is only the source of the choice, never the proof.
            target, _build = appstore_state.resolve_review_target(ctx.cwd, bundle_id, ctx.new_version, client)
        except (AppStoreReviewError, AppStoreStateError) as exc:
            _play_fail_sound(ctx.env, ctx.cwd)
            raise _option_error(str(exc)) from exc

        intent = ReviewIntent(
            bundle_id=target.bundle_id,
            marketing_version=target.marketing_version,
            build_number=target.build_number,
            whats_new=self.whats_new,
            release_mode=self.release_mode,
            phased_release=self.phased_release,
        )
        typer.echo(
            f"==> Submitting {target.marketing_version}+{target.build_number} of {target.bundle_id} for App Store "
            f"review ({self.release_mode} release, phased release {'enabled' if self.phased_release else 'disabled'})"
        )
        try:
            outcome = AppStoreReviewOperation(client, ctx.cwd, intent).run()
        except (AppStoreReviewError, AppStoreStateError) as exc:
            _play_fail_sound(ctx.env, ctx.cwd)
            raise _option_error(str(exc)) from exc

        typer.echo(f"    app: {outcome.bundle_id}")
        typer.echo(f"    version: {outcome.marketing_version} ({outcome.version_id})")
        typer.echo(f"    build: {outcome.build_number} ({outcome.build_id})")
        typer.echo(f"    submission: {outcome.submission_id} (state: {outcome.submission_state})")
        typer.echo(
            f"    release: {outcome.release_mode} after approval, "
            f"phased release {'enabled (seven days)' if outcome.phased_release else 'disabled'}"
        )
        if outcome.resumed:
            typer.echo(
                "    (resumed) This submission was already confirmed earlier; nothing was submitted to Apple again."
            )
        ctx.register_release_results(
            {
                "appstore_review_bundle_id": outcome.bundle_id,
                "appstore_review_marketing_version": outcome.marketing_version,
                "appstore_review_build_number": outcome.build_number,
                "appstore_review_submission_id": outcome.submission_id,
                "appstore_review_submission_state": outcome.submission_state,
                "appstore_review_release_mode": outcome.release_mode,
                "appstore_review_phased_release": "true" if outcome.phased_release else "false",
            }
        )
        typer.echo(
            "✅ Submitted for App Store review: Apple accepted the submission and it left the unsubmitted stage."
        )
        typer.echo(
            "   This does NOT mean approval or user availability: the version reaches users only after Apple "
            "approves it and the chosen release mode releases it."
        )
