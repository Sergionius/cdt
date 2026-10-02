"""Pipeline step that publishes one AAB to one explicit Google Play track.

The step is a thin adapter: it validates its explicit options after input
interpolation, resolves the registered AAB artifact and delegates the whole
publication to :class:`cdt.services.google_play_state.GooglePlayPublishOperation`
(checkpoints, lockning and safe recovery live in the service layer, never in
``ctx.values``). Parameters are never derived from Firebase credentials, the
pipeline name or shared mutable values — only from the step options and the
existing ``${inputs.*}`` interpolation.

Production safety does not depend on a prompt inside the step: any Google Play
step requires ``risk: production`` (enforced by pipeline validation) and the
existing exact direct/detached CLI confirmation.
"""

from __future__ import annotations

import math
import re
from typing import Any

import typer

from ..artifacts import ArtifactKind
from ..pipeline import PipelineContext
from ..services.google_play import GooglePlayClient, GooglePlayError
from ..services.google_play_state import (
    RELEASE_STATUS_COMPLETED,
    RELEASE_STATUS_DRAFT,
    RELEASE_STATUS_IN_PROGRESS,
    GooglePlayPublishOperation,
    GooglePlayStateError,
    PublishIntent,
)
from ..sounds import _play_fail_sound

STEP_NAME = "google_play.upload_aab"

ALLOWED_RELEASE_STATUSES = (RELEASE_STATUS_DRAFT, RELEASE_STATUS_IN_PROGRESS, RELEASE_STATUS_COMPLETED)

# Android package names: at least two dot-separated segments, each starting
# with a letter (com.example.app). Dynamic tracks cannot bypass validation
# because the track itself is an explicit, validated option.
_PACKAGE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+$")
_TRACK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def _option_error(detail: str) -> typer.BadParameter:
    return typer.BadParameter(detail, param_hint=STEP_NAME)


def _require_text(option: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _option_error(f"{STEP_NAME} option '{option}' must be a non-empty string")
    return value.strip()


def _validate_package_name(value: Any) -> str:
    package_name = _require_text("package_name", value)
    if _PACKAGE_NAME_RE.fullmatch(package_name) is None:
        raise _option_error(
            f"{STEP_NAME} option 'package_name' must be an Android package name like com.example.app, "
            f"got {package_name!r}"
        )
    return package_name


def _validate_track(value: Any) -> str:
    track = _require_text("track", value)
    if _TRACK_RE.fullmatch(track) is None:
        raise _option_error(
            f"{STEP_NAME} option 'track' must contain only letters, digits, '-' and '_' (no defaults are "
            f"applied), got {track!r}"
        )
    return track


def _validate_release_status(value: Any) -> str:
    release_status = _require_text("release_status", value)
    if release_status not in ALLOWED_RELEASE_STATUSES:
        allowed = ", ".join(repr(status) for status in ALLOWED_RELEASE_STATUSES)
        raise _option_error(
            f"{STEP_NAME} option 'release_status' must be one of {allowed} (no default is applied), "
            f"got {release_status!r}"
        )
    return release_status


def _validate_release_notes(value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise _option_error(
            f"{STEP_NAME} option 'release_notes' must map language codes to non-empty strings, "
            f"got {type(value).__name__}"
        )
    if not value:
        raise _option_error(
            f"{STEP_NAME} option 'release_notes' must not be empty; omit it or provide at least one language"
        )
    notes: dict[str, str] = {}
    for language, text in value.items():
        if not isinstance(language, str) or not language.strip():
            raise _option_error(f"{STEP_NAME} option 'release_notes' has an empty language code: {language!r}")
        if not isinstance(text, str) or not text.strip():
            raise _option_error(
                f"{STEP_NAME} option 'release_notes[{language!r}]' must be a non-empty localized text"
            )
        notes[language.strip()] = text.strip()
    return notes


def _validate_release_name(value: Any) -> str | None:
    if value is None:
        return None
    release_name = _require_text("release_name", value)
    return release_name


def _parse_user_fraction(value: Any) -> float | None:
    """Parse ``user_fraction`` after input interpolation.

    Accepts real numbers and numeric strings (``${inputs.fraction}`` always
    yields strings). Booleans, NaN, infinity and non-numeric values are
    rejected instead of being silently coerced.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise _option_error(f"{STEP_NAME} option 'user_fraction' must be a number, not a boolean")
    if isinstance(value, (int, float)):
        fraction = float(value)
    elif isinstance(value, str):
        try:
            fraction = float(value.strip())
        except ValueError:
            raise _option_error(
                f"{STEP_NAME} option 'user_fraction' must be a number, got {value!r}"
            ) from None
    else:
        raise _option_error(
            f"{STEP_NAME} option 'user_fraction' must be a number or numeric string, got {type(value).__name__}"
        )
    if not math.isfinite(fraction):
        raise _option_error(f"{STEP_NAME} option 'user_fraction' must be finite (NaN and infinity are rejected)")
    return fraction


def _validate_status_fraction(release_status: str, user_fraction: float | None) -> None:
    if release_status == RELEASE_STATUS_IN_PROGRESS:
        if user_fraction is None:
            raise _option_error(
                f"{STEP_NAME} option 'user_fraction' is required for release_status 'inProgress' "
                "(staged rollout) and must satisfy 0 < user_fraction < 1"
            )
        if not 0 < user_fraction < 1:
            raise _option_error(
                f"{STEP_NAME} option 'user_fraction' must satisfy 0 < user_fraction < 1 for a staged "
                f"rollout, got {user_fraction!r}"
            )
        return
    if user_fraction is not None:
        raise _option_error(
            f"{STEP_NAME} option 'user_fraction' is only allowed for release_status 'inProgress', "
            f"not for {release_status!r}"
        )


class GooglePlayUploadAabStep:
    name = STEP_NAME

    def __init__(
        self,
        artifact: str,
        package_name: str,
        track: str,
        release_status: str,
        release_notes: dict[str, str] | None = None,
        release_name: str | None = None,
        user_fraction: float | str | None = None,
    ):
        # All four primary options are mandatory: no defaults for track and
        # release status, so a publication can never target an unspecified
        # track or silently fall back to a default release state.
        self.artifact = _require_text("artifact", artifact)
        self.package_name = _validate_package_name(package_name)
        self.track = _validate_track(track)
        self.release_status = _validate_release_status(release_status)
        self.release_notes = _validate_release_notes(release_notes)
        self.release_name = _validate_release_name(release_name)
        self.user_fraction = _parse_user_fraction(user_fraction)
        _validate_status_fraction(self.release_status, self.user_fraction)

    def run(self, ctx: PipelineContext) -> None:
        artifact = ctx.artifact(self.artifact)
        if artifact.kind != ArtifactKind.AAB:
            raise _option_error(
                f"{STEP_NAME} requires an AAB artifact, but artifact '{self.artifact}' has kind "
                f"'{artifact.kind.value}'"
            )
        aab_path = artifact.path
        if not aab_path.is_file():
            raise _option_error(f"{STEP_NAME} artifact file not found: {aab_path}")

        intent = PublishIntent(
            package_name=self.package_name,
            track=self.track,
            release_status=self.release_status,
            release_notes=self.release_notes,
            release_name=self.release_name,
            user_fraction=self.user_fraction,
        )
        typer.echo(
            f"==> Publishing AAB to Google Play: {aab_path} "
            f"(package {self.package_name}, track '{self.track}', requested status '{self.release_status}')"
        )
        # Credentials are created per call from the final pipeline env (ADC);
        # nothing here reads or mutates the global environment.
        client = GooglePlayClient(ctx.env, ctx.cwd)
        try:
            outcome = GooglePlayPublishOperation(client, ctx.cwd, intent, aab_path).run()
        except (GooglePlayError, GooglePlayStateError) as exc:
            _play_fail_sound(ctx.env, ctx.cwd)
            raise _option_error(str(exc)) from exc

        typer.echo(f"    package: {outcome.package_name}")
        typer.echo(f"    track: {outcome.track}")
        typer.echo(f"    version code: {outcome.version_code}")
        typer.echo(f"    requested status: {outcome.release_status}")
        typer.echo("    commit: confirmed")
        if outcome.resumed:
            typer.echo(
                "    (resumed) This operation was already confirmed earlier; nothing was uploaded or committed again."
            )
        if outcome.changes_sent_for_review:
            typer.echo(
                "✅ Google Play accepted the changes (edit committed for the standard Google review/publication "
                "flow). Review approval and user availability were NOT verified."
            )
            typer.echo(
                "ℹ️ Managed publishing: if it applies to these changes, the release still requires a manual "
                "Publish in Google Play Console; otherwise Google continues the release rollout automatically. "
                "CDT cannot detect which mode is enabled."
            )
        else:
            typer.echo(
                "✅ Draft release created in Google Play. It was NOT sent for review, nothing is published and "
                "the app is not available to users."
            )
