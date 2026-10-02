"""Persistable Google Play publication operations with safe recovery.

One publication of one AAB to one explicitly chosen track is tracked by a
checkpoint file under ``.cdt/google-play/operations/<operation-id>.json`` —
deliberately outside ``ctx.values``, which is mutable per-run pipeline state.
The operation identifier is derived from the canonical publication parameters
and the SHA-256 of the AAB, so an identical re-run maps to the same checkpoint
while any parameter change starts a new operation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import filelock

from .google_play import (
    STAGE_EDIT_GET,
    STAGE_TRACK_GET,
    GooglePlayError,
    compute_file_sha256,
)

OPERATIONS_SUBDIR = Path(".cdt") / "google-play" / "operations"

CHECKPOINT_SCHEMA_VERSION = 1

# Phases of the persistable operation (checkpoint "phase" field).
PHASE_INTENT = "intent"  # parameters recorded, nothing published yet
PHASE_EDIT_CREATED = "edit_created"  # edit open, target track checked
PHASE_UPLOADED = "uploaded"  # bundle upload confirmed by hash
PHASE_TRACK_UPDATED = "track_updated"  # target track updated (or verified as applied)
PHASE_CONFIRMED = "confirmed"  # commit confirmed, result recorded
PHASES = (PHASE_INTENT, PHASE_EDIT_CREATED, PHASE_UPLOADED, PHASE_TRACK_UPDATED, PHASE_CONFIRMED)

RELEASE_STATUS_DRAFT = "draft"
RELEASE_STATUS_IN_PROGRESS = "inProgress"
RELEASE_STATUS_COMPLETED = "completed"


class GooglePlayStateError(Exception):
    """Base class for Google Play publication state failures."""


class CheckpointError(GooglePlayStateError):
    """A checkpoint is corrupted or has an unknown version; publication must stop."""


class CheckpointWriteError(GooglePlayStateError):
    """A checkpoint could not be saved; no external changes may proceed."""


class OperationLockedError(GooglePlayStateError):
    """Another operation for the same package holds the checkout lock."""


class ConflictingOperationError(GooglePlayStateError):
    """An unfinished operation for the same package has different parameters."""


class TrackConflictError(GooglePlayStateError):
    """The target track is in a state CDT must not replace."""


class UploadMismatchError(GooglePlayStateError):
    """An upload response does not match the local AAB or stays ambiguous."""


class UnknownResultError(GooglePlayStateError):
    """The publication result cannot be established; verify in Play Console."""


def operations_dir(cwd: Path) -> Path:
    """Directory holding publication checkpoints, separate from ctx.values."""
    return cwd / OPERATIONS_SUBDIR


def checkpoint_path(cwd: Path, operation_id: str) -> Path:
    return operations_dir(cwd) / f"{operation_id}.json"


def compute_operation_id(intent: "PublishIntent", aab_sha256: str) -> str:
    """Derive the stable operation identity from canonical parameters and AAB hash.

    Every parameter that changes the external publication changes the identity:
    a re-run with identical parameters resumes the same checkpoint, while any
    changed parameter (track, release options, AAB content) is a new operation.
    """
    canonical = {"parameters": intent.canonical(), "aab_sha256": aab_sha256.lower()}
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass
class PublishCheckpoint:
    """Non-secret checkpoint of one publication operation.

    Only operational data is recorded: package, track, release parameters, the
    AAB hash, edit id/expiry, version code, the phase, the retained source
    release observed on the target track and the confirmed result. Credentials
    and upload-session URLs are never stored — this file is safe to inspect.
    """

    operation_id: str
    package_name: str
    track: str
    release_status: str
    release_notes: dict[str, str] | None
    release_name: str | None
    user_fraction: float | None
    aab_sha256: str
    phase: str = PHASE_INTENT
    edit_id: str | None = None
    edit_expiry_seconds: str | None = None
    version_code: int | None = None
    source_release: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    created_at: str | None = None
    updated_at: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "operation_id": self.operation_id,
            "package_name": self.package_name,
            "track": self.track,
            "release_status": self.release_status,
            "release_notes": self.release_notes,
            "release_name": self.release_name,
            "user_fraction": self.user_fraction,
            "aab_sha256": self.aab_sha256,
            "phase": self.phase,
            "edit_id": self.edit_id,
            "edit_expiry_seconds": self.edit_expiry_seconds,
            "version_code": self.version_code,
            "source_release": self.source_release,
            "result": self.result,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_json(cls, payload: Any, source: str) -> "PublishCheckpoint":
        """Parse and strictly validate one checkpoint payload.

        Any deviation from the versioned format raises :class:`CheckpointError`:
        a checkpoint that cannot be fully trusted must stop publication instead
        of being guessed at.
        """

        def fail(detail: str) -> CheckpointError:
            return CheckpointError(
                f"Google Play operation checkpoint {source} is corrupted: {detail}; "
                "inspect and resolve the file manually before publishing"
            )

        def text(key: str) -> str | None:
            value = payload.get(key)
            if value is None:
                return None
            if not isinstance(value, str):
                raise fail(f"{key} must be a string")
            return value

        def number(key: str) -> float | None:
            value = payload.get(key)
            if value is None:
                return None
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise fail(f"{key} must be a number")
            return float(value)

        def integer(key: str) -> int | None:
            value = payload.get(key)
            if value is None:
                return None
            if isinstance(value, bool) or not isinstance(value, int):
                raise fail(f"{key} must be an integer")
            return int(value)

        def mapping(key: str) -> dict[str, Any] | None:
            value = payload.get(key)
            if value is None:
                return None
            if not isinstance(value, dict):
                raise fail(f"{key} must be an object")
            return value

        if not isinstance(payload, dict):
            raise fail("expected a JSON object")
        version = payload.get("schema_version")
        if version != CHECKPOINT_SCHEMA_VERSION:
            raise fail(f"unsupported schema version {version!r}")
        required: dict[str, str] = {}
        for key in ("operation_id", "package_name", "track", "release_status", "aab_sha256", "phase"):
            value = text(key)
            if value is None:
                raise fail(f"missing {key}")
            required[key] = value
        phase = required["phase"]
        if phase not in PHASES:
            raise fail(f"unknown phase {phase!r}")
        notes = mapping("release_notes")
        if notes is not None and not all(isinstance(k, str) and isinstance(v, str) for k, v in notes.items()):
            raise fail("release_notes must map languages to strings")
        result = mapping("result")
        if phase == PHASE_CONFIRMED and result is None:
            raise fail("confirmed checkpoint requires a recorded result")
        return cls(
            operation_id=required["operation_id"],
            package_name=required["package_name"],
            track=required["track"],
            release_status=required["release_status"],
            release_notes=notes,
            release_name=text("release_name"),
            user_fraction=number("user_fraction"),
            aab_sha256=required["aab_sha256"],
            phase=phase,
            edit_id=text("edit_id"),
            edit_expiry_seconds=text("edit_expiry_seconds"),
            version_code=integer("version_code"),
            source_release=mapping("source_release"),
            result=result,
            created_at=text("created_at"),
            updated_at=text("updated_at"),
        )


@dataclass(frozen=True)
class PublishIntent:
    """Explicit, canonical publication parameters for one AAB and one track."""

    package_name: str
    track: str
    release_status: str
    release_notes: dict[str, str] | None = None
    release_name: str | None = None
    user_fraction: float | None = None

    def canonical(self) -> dict[str, Any]:
        """Return the JSON-ready canonical form used for the operation identity."""
        notes = {language: text for language, text in sorted((self.release_notes or {}).items())}
        return {
            "package_name": self.package_name,
            "track": self.track,
            "release_status": self.release_status,
            "release_notes": notes or None,
            "release_name": self.release_name,
            "user_fraction": self.user_fraction,
        }


@dataclass(frozen=True)
class PublishOutcome:
    """Confirmed result of one publication operation."""

    operation_id: str
    package_name: str
    track: str
    release_status: str
    version_code: int
    aab_sha256: str
    changes_sent_for_review: bool
    resumed: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "version_code": self.version_code,
            "aab_sha256": self.aab_sha256,
            "track": self.track,
            "release_status": self.release_status,
            "changes_sent_for_review": self.changes_sent_for_review,
        }


def load_checkpoint(path: Path) -> PublishCheckpoint | None:
    """Load one checkpoint; ``None`` when it does not exist yet.

    Raises :class:`CheckpointError` for unreadable, corrupted or unknown-version
    files: publication must stop instead of guessing the recorded state.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CheckpointError(f"cannot read Google Play operation checkpoint {path.name}: {exc}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CheckpointError(
            f"Google Play operation checkpoint {path.name} is corrupted (invalid JSON); "
            "inspect and resolve the file manually before publishing"
        ) from exc
    return PublishCheckpoint.from_json(payload, path.name)


def save_checkpoint(path: Path, checkpoint: PublishCheckpoint) -> None:
    """Atomically persist a checkpoint (temporary file + rename)."""
    payload = checkpoint.to_json()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        temporary.replace(path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise CheckpointWriteError(
            f"cannot save Google Play operation checkpoint {path.name}: {exc}; publication stopped"
        ) from exc


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_target_releases(
    source_release: dict[str, Any] | None, intent: PublishIntent, version_code: int
) -> list[dict[str, Any]]:
    """Build the releases payload for the target track only.

    A new draft or staged rollout keeps the previously observed completed
    release alongside the new one (as the current/base release); a new
    completed release replaces the old one outright. Old version codes are
    never merged into the new release.
    """
    release: dict[str, Any] = {"status": intent.release_status, "versionCodes": [version_code]}
    if intent.release_name:
        release["name"] = intent.release_name
    if intent.release_notes:
        release["releaseNotes"] = [
            {"language": language, "text": note_text}
            for language, note_text in sorted(intent.release_notes.items())
        ]
    if intent.release_status == RELEASE_STATUS_IN_PROGRESS:
        release["userFraction"] = intent.user_fraction
    if intent.release_status == RELEASE_STATUS_COMPLETED:
        return [release]
    releases: list[dict[str, Any]] = []
    if source_release is not None:
        releases.append(dict(source_release))
    releases.append(release)
    return releases


def _release_version_codes(release: dict[str, Any]) -> set[int]:
    try:
        return {int(code) for code in (release.get("versionCodes") or [])}
    except (TypeError, ValueError):
        return set()


def _release_key(release: dict[str, Any]) -> tuple[Any, ...]:
    """Comparable identity of a release payload: status, version codes, fraction."""
    fraction = release.get("userFraction")
    return (
        release.get("status"),
        tuple(sorted(_release_version_codes(release))),
        None if fraction is None else float(fraction),
    )


def _track_state_matches(remote: dict[str, Any], expected: list[dict[str, Any]]) -> bool:
    """Compare the observed track state with the exact expected release payload."""
    remote_releases = remote.get("releases") or []
    if len(remote_releases) != len(expected):
        return False
    return sorted(_release_key(release) for release in remote_releases) == sorted(
        _release_key(release) for release in expected
    )


def _required_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise UploadMismatchError(f"Google returned no usable {label}")
    return int(value)


def _is_lost_response(exc: GooglePlayError) -> bool:
    """True when a mutation response may or may not have been applied remotely.

    Timeouts and connection failures carry no HTTP status; late 5xx responses
    are equally ambiguous. Definite HTTP rejections (4xx, e.g. a commit while
    the app is under review) are never treated as lost responses.
    """
    return exc.http_status is None or exc.http_status >= 500


def lock_path(cwd: Path, package_name: str) -> Path:
    """Per-package lock file inside this checkout.

    Different applications map to different lock files, so parallel branches
    publishing different apps never block each other.
    """
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", package_name)
    return cwd / ".cdt" / "google-play" / "locks" / f"{safe}.lock"


class GooglePlayPublishOperation:
    """One persistable AAB publication with safe recovery.

    The whole flow runs under the per-package checkout lock. Every external
    mutation is preceded by a durable checkpoint transition, so an interrupted
    run resumes at the recorded phase without repeating confirmed mutations.
    """

    def __init__(self, client: Any, cwd: Path, intent: PublishIntent, aab_path: Path):
        self._client = client
        self._cwd = cwd
        self._intent = intent
        self._aab_path = aab_path
        self._package = intent.package_name

    # -- public entry point ---------------------------------------------------

    def run(self) -> "PublishOutcome":
        aab_sha256 = self._local_aab_sha256()
        operation_id = compute_operation_id(self._intent, aab_sha256)
        path = checkpoint_path(self._cwd, operation_id)
        lock = self._acquire_package_lock()
        try:
            self._reject_conflicting_operations(operation_id)
            checkpoint = load_checkpoint(path)
            if checkpoint is not None and checkpoint.phase == PHASE_CONFIRMED:
                # Already-confirmed identical operation: return the saved result
                # without a single external call.
                return self._outcome_from(checkpoint, resumed=True)
            return self._execute(aab_sha256, operation_id, path, checkpoint)
        finally:
            lock.release()

    # -- state machine ---------------------------------------------------------

    def _execute(
        self,
        aab_sha256: str,
        operation_id: str,
        path: Path,
        checkpoint: PublishCheckpoint | None,
    ) -> "PublishOutcome":
        if checkpoint is None:
            checkpoint = self._new_checkpoint(aab_sha256, operation_id)
            # The intent is durable before the first external mutation; if it
            # cannot be saved, publication never touches Google Play.
            self._save(path, checkpoint)

        if checkpoint.edit_id and not self._edit_is_live(checkpoint.edit_id):
            # The saved edit is gone: either it expired before the commit
            # (nothing was applied) or a commit succeeded without the response
            # reaching us. Distinguish through the remote app state first.
            outcome = self._verify_remote_result(checkpoint)
            if outcome is not None:
                self._save_confirmed(path, checkpoint, outcome)
                return outcome
            checkpoint = self._reset_for_new_edit(checkpoint)
            self._save(path, checkpoint)

        if checkpoint.phase == PHASE_INTENT:
            edit = self._client.create_edit(self._package)
            checkpoint.edit_id = str(edit.get("id"))
            expiry = edit.get("expiryTimeSeconds")
            checkpoint.edit_expiry_seconds = None if expiry is None else str(expiry)
            checkpoint.phase = PHASE_EDIT_CREATED
            self._save(path, checkpoint)

        if checkpoint.phase == PHASE_EDIT_CREATED:
            # The target track is checked before anything is uploaded.
            checkpoint.source_release = self._check_target_track(checkpoint.edit_id)
            self._save(path, checkpoint)
            self._ensure_bundle_uploaded(path, checkpoint, aab_sha256)

        if checkpoint.phase == PHASE_UPLOADED:
            self._update_target_track(path, checkpoint)

        if checkpoint.phase == PHASE_TRACK_UPDATED:
            return self._commit(path, checkpoint)

        raise GooglePlayStateError(
            f"Google Play operation {operation_id} stopped in unexpected phase {checkpoint.phase!r}"
        )

    # -- bundle upload ---------------------------------------------------------

    def _bundles_matching_sha(self, edit_id: str, aab_sha256: str) -> list[dict[str, Any]]:
        bundles = self._client.list_bundles(self._package, edit_id)
        needle = aab_sha256.lower()
        return [bundle for bundle in bundles if str(bundle.get("sha256", "")).lower() == needle]

    def _ensure_bundle_uploaded(self, path: Path, checkpoint: PublishCheckpoint, aab_sha256: str) -> None:
        edit_id = checkpoint.edit_id
        matches = self._bundles_matching_sha(edit_id, aab_sha256)
        if len(matches) > 1:
            raise UploadMismatchError(
                f"edit {edit_id} contains multiple bundles with SHA-256 {aab_sha256}; refusing to "
                "continue from ambiguous state"
            )
        if len(matches) == 1:
            # The bundle of a previous attempt is verifiably in this edit.
            checkpoint.version_code = _required_int(matches[0].get("versionCode"), "bundle versionCode")
        else:
            try:
                response = self._client.upload_bundle(self._package, edit_id, self._aab_path)
            except GooglePlayError as exc:
                if not _is_lost_response(exc):
                    raise
                # Lost upload response: resolve it by looking the bundle up by
                # its exact SHA-256 in the saved edit; never upload twice in a
                # row on a maybe-duplicated request.
                matches = self._bundles_matching_sha(edit_id, aab_sha256)
                if len(matches) != 1:
                    raise UploadMismatchError(
                        f"the AAB upload response was lost and the bundle cannot be identified by its "
                        f"SHA-256 in edit {edit_id}; publication stopped without re-uploading - verify "
                        "the bundle state in Google Play Console before retrying"
                    ) from exc
                checkpoint.version_code = _required_int(matches[0].get("versionCode"), "bundle versionCode")
            else:
                # Trust only the Google-returned version code, verified against
                # the local AAB hash.
                response_sha = str(response.get("sha256", "")).lower()
                if response_sha != aab_sha256.lower():
                    raise UploadMismatchError(
                        "the bundle hash reported by Google does not match the local AAB "
                        f"({response_sha or 'missing'} != {aab_sha256.lower()}); publication stopped"
                    )
                checkpoint.version_code = _required_int(response.get("versionCode"), "upload versionCode")
        checkpoint.phase = PHASE_UPLOADED
        self._save(path, checkpoint)

    # -- track update -------------------------------------------------------------

    def _update_target_track(self, path: Path, checkpoint: PublishCheckpoint) -> None:
        releases = build_target_releases(checkpoint.source_release, self._intent, checkpoint.version_code)
        try:
            self._client.update_track(self._package, checkpoint.edit_id, self._intent.track, releases)
        except GooglePlayError as exc:
            if not _is_lost_response(exc):
                raise
            # Lost track update response: compare the remote state with the
            # expected payload. Conflicting state is never overwritten.
            remote = self._get_track_or_none(checkpoint.edit_id)
            if remote is None or not _track_state_matches(remote, releases):
                raise UnknownResultError(
                    self._unknown_result_message(
                        checkpoint,
                        "the track update response was lost and the target track does not verifiably "
                        "match the expected release payload",
                    )
                ) from exc
        checkpoint.phase = PHASE_TRACK_UPDATED
        self._save(path, checkpoint)

    def _commit(self, path: Path, checkpoint: PublishCheckpoint) -> "PublishOutcome":
        try:
            self._client.commit_edit(self._package, checkpoint.edit_id)
        except GooglePlayError as exc:
            if not _is_lost_response(exc):
                # A definite Google rejection (e.g. the app is currently under
                # review) is surfaced as-is; flags are never changed to retry.
                raise
            if self._edit_is_live(checkpoint.edit_id):
                # The edit still exists, so the commit provably did not apply;
                # one evidence-based retry is safe (an edit commits once).
                try:
                    self._client.commit_edit(self._package, checkpoint.edit_id)
                except GooglePlayError as retry_error:
                    if not _is_lost_response(retry_error):
                        raise
                    raise UnknownResultError(
                        self._unknown_result_message(
                            checkpoint, "the commit response was lost twice while the edit remained open"
                        )
                    ) from retry_error
            else:
                outcome = self._verify_remote_result(checkpoint)
                if outcome is None:
                    raise UnknownResultError(
                        self._unknown_result_message(
                            checkpoint,
                            "the commit response was lost and the application state does not prove the "
                            "publication was applied",
                        )
                    ) from exc
                self._save_confirmed(path, checkpoint, outcome)
                return outcome
        outcome = self._outcome_from(checkpoint, resumed=False)
        self._save_confirmed(path, checkpoint, outcome)
        return outcome

    def _verify_remote_result(self, checkpoint: PublishCheckpoint) -> "PublishOutcome | None":
        """Establish whether the saved publication was already applied remotely.

        Opens a separate verification edit that is never committed (and is
        deleted afterwards). Returns the confirmed outcome only when the app
        contains a bundle with the exact SHA-256 and version code of this
        operation AND the target track matches the expected release parameters.
        Returns ``None`` when it is provably not applied (no matching bundle).
        Anything else raises :class:`UnknownResultError`.
        """
        edit = self._client.create_edit(self._package)
        verification_edit_id = str(edit.get("id"))
        try:
            matches = self._bundles_matching_sha(verification_edit_id, checkpoint.aab_sha256)
            if not matches:
                # No bundle with this hash in the app: the edit expired before
                # the commit (or the commit never applied). Safe to restart.
                return None
            if len(matches) > 1:
                raise UnknownResultError(
                    self._unknown_result_message(
                        checkpoint, "multiple bundles match the operation SHA-256 in the application"
                    )
                )
            version_code = _required_int(matches[0].get("versionCode"), "bundle versionCode")
            if checkpoint.version_code is not None and version_code != checkpoint.version_code:
                raise UnknownResultError(
                    self._unknown_result_message(
                        checkpoint,
                        f"the application contains the AAB as version code {version_code} while the "
                        f"operation recorded {checkpoint.version_code}",
                    )
                )
            track = self._get_track_or_none(verification_edit_id) or {}
            releases = [
                release
                for release in (track.get("releases") or [])
                if version_code in _release_version_codes(release)
            ]
            if len(releases) != 1 or not self._release_matches_intent(releases[0], version_code):
                raise UnknownResultError(
                    self._unknown_result_message(
                        checkpoint,
                        "the bundle is present in the application but the target track does not match "
                        "the expected release parameters",
                    )
                )
            return PublishOutcome(
                operation_id=checkpoint.operation_id,
                package_name=checkpoint.package_name,
                track=checkpoint.track,
                release_status=checkpoint.release_status,
                version_code=version_code,
                aab_sha256=checkpoint.aab_sha256,
                changes_sent_for_review=checkpoint.release_status != RELEASE_STATUS_DRAFT,
                resumed=False,
            )
        finally:
            # A verification edit is never committed and never validated.
            self._best_effort_delete_edit(verification_edit_id)

    def _release_matches_intent(self, release: dict[str, Any], version_code: int) -> bool:
        if release.get("status") != self._intent.release_status:
            return False
        if _release_version_codes(release) != {version_code}:
            return False
        fraction = release.get("userFraction")
        if self._intent.release_status == RELEASE_STATUS_IN_PROGRESS:
            return fraction is not None and float(fraction) == float(self._intent.user_fraction)
        return fraction is None

    def _unknown_result_message(self, checkpoint: PublishCheckpoint, detail: str) -> str:
        return (
            f"Google Play publication result is unknown: {detail} (operation {checkpoint.operation_id}, "
            f"checkpoint .cdt/google-play/operations/{checkpoint.operation_id}.json). Verify the release "
            "in Google Play Console (Bundle Explorer and the track pages) before doing anything else; "
            "do not re-run the upload or commit blindly and do not delete the checkpoint to force a retry."
        )

    def _best_effort_delete_edit(self, edit_id: str) -> None:
        try:
            self._client.delete_edit(self._package, edit_id)
        except GooglePlayError:
            pass

    # -- remote reads and safety checks -----------------------------------------

    def _edit_is_live(self, edit_id: str) -> bool:
        try:
            self._client.get_edit(self._package, edit_id)
        except GooglePlayError as exc:
            if exc.stage == STAGE_EDIT_GET and exc.http_status == 404:
                return False
            raise
        return True

    def _get_track_or_none(self, edit_id: str) -> dict[str, Any] | None:
        """Read the target track; ``None`` when the track does not exist yet."""
        try:
            return self._client.get_track(self._package, edit_id, self._intent.track)
        except GooglePlayError as exc:
            if exc.stage == STAGE_TRACK_GET and exc.http_status == 404:
                return None
            raise

    def _check_target_track(self, edit_id: str) -> dict[str, Any] | None:
        """Validate the target track before uploading; return the retained release.

        An empty track or a single regular completed release is accepted. Draft,
        ``inProgress``, ``halted``, staged or otherwise complex states stop the
        publication instead of replacing the existing release.
        """
        track = self._get_track_or_none(edit_id) or {}
        releases = track.get("releases") or []
        if not releases:
            return None
        if len(releases) > 1:
            raise TrackConflictError(
                f"target track {self._intent.track!r} already contains {len(releases)} releases; CDT does "
                "not replace complex release states - resolve the track in Google Play Console"
            )
        release = releases[0]
        status = release.get("status")
        if status != RELEASE_STATUS_COMPLETED:
            raise TrackConflictError(
                f"target track {self._intent.track!r} has a release with status {status!r}; CDT stops "
                "instead of replacing it - resolve the track in Google Play Console"
            )
        fraction = release.get("userFraction")
        if fraction is not None and float(fraction) != 1.0:
            raise TrackConflictError(
                f"target track {self._intent.track!r} has a staged rollout in progress; CDT does not "
                "continue or replace it - resolve the rollout in Google Play Console"
            )
        if not release.get("versionCodes"):
            raise TrackConflictError(
                f"target track {self._intent.track!r} has a completed release without version codes; "
                "CDT does not replace unknown release states"
            )
        return dict(release)

    # -- checkpoint bookkeeping -------------------------------------------------

    def _local_aab_sha256(self) -> str:
        try:
            return compute_file_sha256(self._aab_path)
        except OSError as exc:
            raise GooglePlayStateError(f"cannot read AAB file {self._aab_path}: {exc}") from exc

    def _new_checkpoint(self, aab_sha256: str, operation_id: str) -> PublishCheckpoint:
        now = _now()
        return PublishCheckpoint(
            operation_id=operation_id,
            package_name=self._intent.package_name,
            track=self._intent.track,
            release_status=self._intent.release_status,
            release_notes=dict(self._intent.release_notes) if self._intent.release_notes else None,
            release_name=self._intent.release_name,
            user_fraction=self._intent.user_fraction,
            aab_sha256=aab_sha256,
            phase=PHASE_INTENT,
            created_at=now,
            updated_at=now,
        )

    def _save(self, path: Path, checkpoint: PublishCheckpoint) -> None:
        # A checkpoint write failure always stops publication: no external
        # mutation may happen without its durable intent.
        checkpoint.updated_at = _now()
        save_checkpoint(path, checkpoint)

    def _save_confirmed(self, path: Path, checkpoint: PublishCheckpoint, outcome: "PublishOutcome") -> None:
        checkpoint.phase = PHASE_CONFIRMED
        checkpoint.version_code = outcome.version_code
        checkpoint.result = outcome.to_json()
        self._save(path, checkpoint)

    def _outcome_from(self, checkpoint: PublishCheckpoint, *, resumed: bool) -> "PublishOutcome":
        result = checkpoint.result or {}
        version_code = result.get("version_code", checkpoint.version_code)
        if isinstance(version_code, bool) or not isinstance(version_code, int):
            raise CheckpointError(
                f"Google Play operation checkpoint {checkpoint.operation_id}.json has an invalid result"
            )
        return PublishOutcome(
            operation_id=checkpoint.operation_id,
            package_name=checkpoint.package_name,
            track=checkpoint.track,
            release_status=checkpoint.release_status,
            version_code=version_code,
            aab_sha256=checkpoint.aab_sha256,
            changes_sent_for_review=checkpoint.release_status != RELEASE_STATUS_DRAFT,
            resumed=resumed,
        )

    def _reset_for_new_edit(self, checkpoint: PublishCheckpoint) -> PublishCheckpoint:
        """Restart from a fresh edit after verifying nothing was applied remotely."""
        checkpoint.edit_id = None
        checkpoint.edit_expiry_seconds = None
        checkpoint.version_code = None
        checkpoint.source_release = None
        checkpoint.phase = PHASE_INTENT
        return checkpoint

    # -- checkout-local coordination ----------------------------------------------

    def _acquire_package_lock(self) -> filelock.FileLock:
        path = lock_path(self._cwd, self._package)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = filelock.FileLock(path, timeout=0, thread_local=False)
        try:
            lock.acquire()
        except filelock.Timeout as exc:
            raise OperationLockedError(
                f"another Google Play publication for {self._package} is already running in this checkout "
                f"(lock file {path.name} is held)"
            ) from exc
        return lock

    def _reject_conflicting_operations(self, operation_id: str) -> None:
        """Stop on unfinished operations of the same package with other parameters."""
        directory = operations_dir(self._cwd)
        if not directory.is_dir():
            return
        own_name = f"{operation_id}.json"
        for candidate in sorted(directory.glob("*.json")):
            if candidate.name == own_name:
                continue
            other = load_checkpoint(candidate)  # corrupted files stop publication here
            if other.package_name != self._package or other.phase == PHASE_CONFIRMED:
                continue
            raise ConflictingOperationError(
                f"unfinished Google Play operation {other.operation_id} for {other.package_name} "
                f"(phase {other.phase!r}, track {other.track!r}, status {other.release_status!r}) blocks a "
                "publication with changed parameters; resolve it before publishing"
            )
