"""Persisted identification of the last successfully completed TestFlight build.

After the full ``appstore.upload_testflight`` step or the
``appstore.complete_testflight`` step finishes successfully, CDT records which
build the cycle made ready: bundle id, marketing version, build number and the
completion time. The record lives under
``.cdt/appstore/uploads/<bundle-key>.json`` where ``<bundle-key>`` is the
SHA-256 of the bundle id, so two apps in the same checkout never overwrite each
other's records.

The file is deliberately outside ``ctx.values`` (mutable per-run pipeline
state): a standalone submit pipeline relies on it to identify the build without
rebuilding or re-uploading. Only operational identification is stored — no
tokens, keys or other secrets — and the stored bundle id is re-checked against
the app on every read.

The record is only the *source of the choice*: it never replaces remote
verification. :func:`resolve_review_target` re-validates the selected build in
App Store Connect on every submission, because Apple remains the source of
truth about app ownership, processing state and expiry.

On top of the upload records this module implements the resumable App Review
submission operation (:class:`AppStoreReviewOperation`). One submission of one
build is tracked by a checkpoint file under
``.cdt/appstore/operations/<operation-id>.json`` and coordinated by a per-app
lock file under ``.cdt/appstore/locks/`` — the same checkout-local pattern as
``google_play_state``. The operation identifier is derived from the app,
platform, version, build number and the canonical submission parameters, so an
identical re-run resumes the same checkpoint while any parameter change starts
a new operation (and is blocked while the old one is unfinished). Every
external change is preceded by a durable checkpoint transition, and every
re-run re-verifies the checkpoint against App Store Connect — the local phase
is never treated as proof that Apple accepted anything.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import filelock

from . import appstore_review
from .appstore import AscAmbiguousResultError
from .appstore_review import (
    ASC_PLATFORM_IOS,
    RELEASE_MODES,
    BuildConflictError,
    SubmissionConflictError,
    add_review_submission_item,
    create_review_submission,
    ensure_version_editable,
    find_app_id,
    find_app_store_version,
    find_build,
    find_open_review_submission,
    find_version_item,
    get_app_store_version,
    get_or_create_app_store_version,
    get_phased_release,
    get_review_submission,
    get_review_submission_items,
    get_version_build_id,
    get_whats_new,
    item_version_id,
    set_phased_release,
    set_release_type,
    set_version_build,
    set_whats_new,
    submission_is_submitted,
    submission_state,
    submission_version_id,
    submit_review_submission,
    version_platform,
    version_release_type,
    version_string,
)

if TYPE_CHECKING:
    from .appstore import _AscClient

UPLOADS_SUBDIR = Path(".cdt") / "appstore" / "uploads"
OPERATIONS_SUBDIR = Path(".cdt") / "appstore" / "operations"
LOCKS_SUBDIR = Path(".cdt") / "appstore" / "locks"

RECORD_SCHEMA_VERSION = 1
REVIEW_CHECKPOINT_SCHEMA_VERSION = 1

# Where a selected target came from: the current pipeline context or the
# persisted upload record for the app.
SOURCE_CONTEXT = "context"
SOURCE_RECORD = "record"


class AppStoreStateError(Exception):
    """Base class for persisted App Store upload state failures."""


class RecordError(AppStoreStateError):
    """An upload record is corrupted, has an unknown version or belongs to a different app."""


class RecordWriteError(AppStoreStateError):
    """An upload record could not be saved; the completed build stays unrecorded."""


class CheckpointError(AppStoreStateError):
    """A review operation checkpoint is corrupted or has an unknown version; submission must stop."""


class CheckpointWriteError(AppStoreStateError):
    """A review checkpoint could not be saved; no external changes may proceed."""


class OperationLockedError(AppStoreStateError):
    """Another review operation for the same app holds the checkout lock."""


class ConflictingOperationError(AppStoreStateError):
    """An unfinished review operation for the same app has different parameters."""


class UnknownResultError(AppStoreStateError):
    """The review submission result cannot be established; verify in App Store Connect."""


# Phases of the resumable review submission operation (checkpoint "phase" field).
PHASE_INTENT = "intent"  # parameters recorded, nothing changed remotely yet
PHASE_VERSION_READY = "version_ready"  # app verified, build verified, version exists/created
PHASE_BUILD_BOUND = "build_bound"  # build binding applied and verified
PHASE_WHATS_NEW_SET = "whats_new_set"  # whatsNew localizations written and verified
PHASE_RELEASE_TYPE_SET = "release_type_set"  # releaseType written and verified
PHASE_PHASED_RELEASE_SET = "phased_release_set"  # phased release presence set and verified
PHASE_SUBMISSION_READY = "submission_ready"  # draft submission with exactly our item recorded
PHASE_SUBMITTED = "submitted"  # submit request sent, Apple's acceptance not yet confirmed
PHASE_CONFIRMED = "confirmed"  # Apple accepted the submission beyond the unsubmitted stage
PHASES = (
    PHASE_INTENT,
    PHASE_VERSION_READY,
    PHASE_BUILD_BOUND,
    PHASE_WHATS_NEW_SET,
    PHASE_RELEASE_TYPE_SET,
    PHASE_PHASED_RELEASE_SET,
    PHASE_SUBMISSION_READY,
    PHASE_SUBMITTED,
    PHASE_CONFIRMED,
)
_PHASE_ORDER = {phase: index for index, phase in enumerate(PHASES)}


@dataclass(frozen=True)
class ReviewIntent:
    """Explicit, canonical parameters of one App Review submission.

    Everything that changes the remote submission is part of the identity:
    the app (bundle id), the platform, the exact build (marketing version +
    build number) and the submission parameters (whatsNew texts, release mode
    and phased release). Any change produces a new operation id.
    """

    bundle_id: str
    marketing_version: str
    build_number: str
    whats_new: dict[str, str]
    release_mode: str
    phased_release: bool
    platform: str = ASC_PLATFORM_IOS

    def __post_init__(self) -> None:
        if not isinstance(self.bundle_id, str) or not self.bundle_id.strip():
            raise AppStoreStateError("bundle id must be a non-empty string")
        for name in ("marketing_version", "build_number"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise AppStoreStateError(f"{name} must be a non-empty string")
        if (
            not isinstance(self.whats_new, dict)
            or not self.whats_new
            or not all(
                isinstance(locale, str) and locale.strip() and isinstance(text, str) and text.strip()
                for locale, text in self.whats_new.items()
            )
        ):
            raise AppStoreStateError("whats_new must map non-empty locales to non-empty texts")
        if self.release_mode not in RELEASE_MODES:
            raise AppStoreStateError(
                f"Unknown release mode {self.release_mode!r}; expected 'manual' or 'automatic'"
            )
        if not isinstance(self.phased_release, bool):
            raise AppStoreStateError("phased_release must be a real boolean")
        if not isinstance(self.platform, str) or not self.platform.strip():
            raise AppStoreStateError("platform must be a non-empty string")

    def canonical(self) -> dict[str, Any]:
        """Return the JSON-ready canonical form used for the operation identity."""
        return {
            "bundle_id": self.bundle_id,
            "platform": self.platform,
            "marketing_version": self.marketing_version,
            "build_number": self.build_number,
            "whats_new": {locale: text for locale, text in sorted(self.whats_new.items())},
            "release_mode": self.release_mode,
            "phased_release": self.phased_release,
        }


def compute_operation_id(intent: ReviewIntent) -> str:
    """Derive the stable operation identity from the canonical submission parameters.

    A re-run with identical parameters maps to the same checkpoint, while any
    changed parameter (version, build, texts, release options) is a new
    operation — and stays blocked until the previous unfinished one is
    resolved.
    """
    canonical = {"kind": "appstore-submit-review", "parameters": intent.canonical()}
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass
class ReviewCheckpoint:
    """Non-secret checkpoint of one App Review submission operation.

    Only operational data is recorded: the canonical parameters, the recorded
    remote ids (app, version, build, submission, submission item), the phase,
    an optional blocking state saved after an unresolved ambiguity and the
    confirmed result. Tokens, keys and other credentials are never stored.
    """

    operation_id: str
    bundle_id: str
    platform: str
    marketing_version: str
    build_number: str
    whats_new: dict[str, str]
    release_mode: str
    phased_release: bool
    phase: str = PHASE_INTENT
    app_id: str | None = None
    version_id: str | None = None
    build_id: str | None = None
    submission_id: str | None = None
    submission_item_id: str | None = None
    blocked: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    created_at: str | None = None
    updated_at: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": REVIEW_CHECKPOINT_SCHEMA_VERSION,
            "operation_id": self.operation_id,
            "bundle_id": self.bundle_id,
            "platform": self.platform,
            "marketing_version": self.marketing_version,
            "build_number": self.build_number,
            "whats_new": self.whats_new,
            "release_mode": self.release_mode,
            "phased_release": self.phased_release,
            "phase": self.phase,
            "app_id": self.app_id,
            "version_id": self.version_id,
            "build_id": self.build_id,
            "submission_id": self.submission_id,
            "submission_item_id": self.submission_item_id,
            "blocked": self.blocked,
            "result": self.result,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_json(cls, payload: Any, source: str) -> "ReviewCheckpoint":
        """Parse and strictly validate one checkpoint payload.

        Any deviation from the versioned format raises :class:`CheckpointError`:
        a checkpoint that cannot be fully trusted must stop the submission
        instead of being guessed at.
        """

        def fail(detail: str) -> CheckpointError:
            return CheckpointError(
                f"App Store review checkpoint {source} is corrupted: {detail}; "
                "inspect and resolve the file manually before submitting"
            )

        def text(key: str) -> str | None:
            value = payload.get(key)
            if value is None:
                return None
            if not isinstance(value, str):
                raise fail(f"{key} must be a string")
            return value

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
        if version != REVIEW_CHECKPOINT_SCHEMA_VERSION:
            raise fail(f"unsupported schema version {version!r}")
        required: dict[str, str] = {}
        for key in (
            "operation_id",
            "bundle_id",
            "platform",
            "marketing_version",
            "build_number",
            "release_mode",
            "phase",
        ):
            value = text(key)
            if value is None:
                raise fail(f"missing {key}")
            required[key] = value
        phase = required["phase"]
        if phase not in PHASES:
            raise fail(f"unknown phase {phase!r}")
        if required["release_mode"] not in RELEASE_MODES:
            raise fail(f"unknown release mode {required['release_mode']!r}")
        whats_new = mapping("whats_new")
        if whats_new is None:
            raise fail("missing whats_new")
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in whats_new.items()):
            raise fail("whats_new must map locales to strings")
        phased_release = payload.get("phased_release")
        if not isinstance(phased_release, bool):
            raise fail("phased_release must be a boolean")
        blocked = mapping("blocked")
        if blocked is not None and not isinstance(blocked.get("detail"), str):
            raise fail("blocked state requires a detail string")
        result = mapping("result")
        if phase == PHASE_CONFIRMED and result is None:
            raise fail("confirmed checkpoint requires a recorded result")
        return cls(
            operation_id=required["operation_id"],
            bundle_id=required["bundle_id"],
            platform=required["platform"],
            marketing_version=required["marketing_version"],
            build_number=required["build_number"],
            whats_new=whats_new,
            release_mode=required["release_mode"],
            phased_release=phased_release,
            phase=phase,
            app_id=text("app_id"),
            version_id=text("version_id"),
            build_id=text("build_id"),
            submission_id=text("submission_id"),
            submission_item_id=text("submission_item_id"),
            blocked=blocked,
            result=result,
            created_at=text("created_at"),
            updated_at=text("updated_at"),
        )


@dataclass(frozen=True)
class ReviewOutcome:
    """Confirmed result of one App Review submission operation."""

    operation_id: str
    bundle_id: str
    app_id: str
    version_id: str
    build_id: str
    marketing_version: str
    build_number: str
    submission_id: str
    submission_state: str
    release_mode: str
    release_type: str
    phased_release: bool
    resumed: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "bundle_id": self.bundle_id,
            "app_id": self.app_id,
            "version_id": self.version_id,
            "build_id": self.build_id,
            "marketing_version": self.marketing_version,
            "build_number": self.build_number,
            "submission_id": self.submission_id,
            "submission_state": self.submission_state,
            "release_mode": self.release_mode,
            "release_type": self.release_type,
            "phased_release": self.phased_release,
        }


def bundle_key(bundle_id: str) -> str:
    """Return the stable record file key: the SHA-256 of the bundle id."""
    return hashlib.sha256(bundle_id.encode("utf-8")).hexdigest()


def uploads_dir(cwd: Path) -> Path:
    """Directory holding upload records, separate from ctx.values."""
    return cwd / UPLOADS_SUBDIR


def record_path(cwd: Path, bundle_id: str) -> Path:
    return uploads_dir(cwd) / f"{bundle_key(bundle_id)}.json"


def operations_dir(cwd: Path) -> Path:
    """Directory holding review submission checkpoints, separate from ctx.values."""
    return cwd / OPERATIONS_SUBDIR


def checkpoint_path(cwd: Path, operation_id: str) -> Path:
    return operations_dir(cwd) / f"{operation_id}.json"


def review_lock_path(cwd: Path, bundle_id: str) -> Path:
    """Per-app lock file inside this checkout.

    Different applications map to different lock files, so parallel branches
    submitting different apps never block each other.
    """
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", bundle_id)
    return cwd / LOCKS_SUBDIR / f"{safe}.lock"


def load_review_checkpoint(path: Path) -> "ReviewCheckpoint | None":
    """Load one review checkpoint; ``None`` when it does not exist yet.

    Raises :class:`CheckpointError` for unreadable, corrupted or
    unknown-version files: submission must stop instead of guessing the
    recorded state.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CheckpointError(f"cannot read App Store review checkpoint {path.name}: {exc}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CheckpointError(
            f"App Store review checkpoint {path.name} is corrupted (invalid JSON); "
            "inspect and resolve the file manually before submitting"
        ) from exc
    return ReviewCheckpoint.from_json(payload, path.name)


def save_review_checkpoint(path: Path, checkpoint: "ReviewCheckpoint") -> None:
    """Atomically persist a review checkpoint (temporary file + rename)."""
    payload = checkpoint.to_json()
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        temporary.replace(path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass  # cleanup is best-effort; the original write failure is what matters
        raise CheckpointWriteError(
            f"cannot save App Store review checkpoint {path.name}: {exc}; submission stopped"
        ) from exc


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_new_version(new_version: str) -> tuple[str, str]:
    """Split ``marketing+build`` (e.g. ``1.2.3+5``) into its two non-empty parts."""
    parts = new_version.strip().split("+")
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        raise AppStoreStateError(
            f"Invalid pipeline version {new_version!r}: expected 'marketing+build' like 1.2.3+5"
        )
    return parts[0].strip(), parts[1].strip()


@dataclass(frozen=True)
class UploadRecord:
    """Non-secret identification of one successfully completed TestFlight build."""

    bundle_id: str
    marketing_version: str
    build_number: str
    completed_at: str

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": RECORD_SCHEMA_VERSION,
            "bundle_id": self.bundle_id,
            "marketing_version": self.marketing_version,
            "build_number": self.build_number,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_json(cls, payload: Any, expected_bundle_id: str, source: str) -> "UploadRecord":
        """Parse and strictly validate one record payload.

        Any deviation from the versioned format — or a stored bundle id that
        does not match the app being operated on — raises :class:`RecordError`:
        an untrustworthy record must stop the submission instead of being
        guessed at.
        """

        def fail(detail: str) -> RecordError:
            return RecordError(
                f"App Store upload record {source} is corrupted: {detail}; "
                "inspect and resolve the file manually before submitting"
            )

        if not isinstance(payload, dict):
            raise fail("expected a JSON object")
        if payload.get("schema_version") != RECORD_SCHEMA_VERSION:
            raise fail(f"unsupported schema version {payload.get('schema_version')!r}")
        for key in ("bundle_id", "marketing_version", "build_number", "completed_at"):
            value = payload.get(key)
            if not isinstance(value, str) or not value.strip():
                raise fail(f"{key} must be a non-empty string")
        if payload["bundle_id"] != expected_bundle_id:
            raise fail(f"record belongs to app {payload['bundle_id']!r}, expected {expected_bundle_id!r}")
        return cls(
            bundle_id=payload["bundle_id"],
            marketing_version=payload["marketing_version"],
            build_number=payload["build_number"],
            completed_at=payload["completed_at"],
        )


def save_upload_record(
    cwd: Path,
    bundle_id: str,
    new_version: str,
    completed_at: str | None = None,
) -> UploadRecord:
    """Atomically record the build that a full TestFlight cycle just made ready.

    Only the full ``appstore.upload_testflight`` step and the
    ``appstore.complete_testflight`` step call this — upload-only must not
    declare the build ready. A write failure raises :class:`RecordWriteError`
    so callers fail the step instead of pretending the build was recorded.
    """
    if not bundle_id.strip():
        raise AppStoreStateError("Cannot save an App Store upload record without a bundle id")
    marketing_version, build_number = parse_new_version(new_version)
    record = UploadRecord(
        bundle_id=bundle_id,
        marketing_version=marketing_version,
        build_number=build_number,
        completed_at=completed_at or _now(),
    )
    path = record_path(cwd, bundle_id)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(record.to_json(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass  # cleanup is best-effort; the original write failure is what matters
        raise RecordWriteError(f"cannot save App Store upload record {path.name}: {exc}") from exc
    return record


def load_upload_record(cwd: Path, bundle_id: str) -> UploadRecord | None:
    """Load the persisted record for *bundle_id*; ``None`` when it does not exist yet.

    Raises :class:`RecordError` for unreadable, corrupted, unknown-version or
    mismatched-app files: the submission must stop instead of guessing.
    """
    path = record_path(cwd, bundle_id)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RecordError(f"cannot read App Store upload record {path.name}: {exc}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RecordError(
            f"App Store upload record {path.name} is corrupted (invalid JSON); "
            "inspect and resolve the file manually before submitting"
        ) from exc
    return UploadRecord.from_json(payload, bundle_id, path.name)


@dataclass(frozen=True)
class UploadTarget:
    """The exact build a submission should target, and where the choice came from."""

    bundle_id: str
    marketing_version: str
    build_number: str
    source: str  # SOURCE_CONTEXT or SOURCE_RECORD


def select_upload_target(cwd: Path, bundle_id: str, new_version: str | None) -> UploadTarget:
    """Choose the exact build for an App Review submission.

    A non-empty current pipeline version always wins (``ctx.new_version`` has
    priority); the persisted record for the app is read only when there is no
    current version. An invalid current value is an error — it never falls back
    to a stale record, which could silently target an older build.

    The returned identification is the *source of the choice only*: callers
    must re-verify the build in App Store Connect (exact app/version/build
    lookup, ``VALID`` processing, iOS platform, not expired) before any
    submission — the local record never replaces remote verification.
    """
    if new_version and new_version.strip():
        marketing_version, build_number = parse_new_version(new_version)
        return UploadTarget(
            bundle_id=bundle_id,
            marketing_version=marketing_version,
            build_number=build_number,
            source=SOURCE_CONTEXT,
        )
    record = load_upload_record(cwd, bundle_id)
    if record is None:
        raise AppStoreStateError(
            f"No recorded TestFlight build for {bundle_id}: run appstore.upload_testflight or "
            "appstore.complete_testflight for this app first; CDT does not pick an arbitrary "
            "latest Apple build"
        )
    return UploadTarget(
        bundle_id=record.bundle_id,
        marketing_version=record.marketing_version,
        build_number=record.build_number,
        source=SOURCE_RECORD,
    )


def resolve_review_target(
    cwd: Path, bundle_id: str, new_version: str | None, client: "_AscClient"
) -> tuple[UploadTarget, dict]:
    """Select the submission target and immediately re-verify it in App Store Connect.

    Selection follows :func:`select_upload_target` (current context wins over
    the persisted record). The selected build is then always re-checked in ASC
    — exact app, marketing version and build number lookup via
    :func:`appstore_review.find_build`, which also enforces the iOS platform,
    ``VALID`` processing and a non-expired build — so the local record confirms
    only where the choice came from, never that the build is still usable.
    """
    target = select_upload_target(cwd, bundle_id, new_version)
    app_id = appstore_review.find_app_id(target.bundle_id, client)
    build = appstore_review.find_build(app_id, target.marketing_version, target.build_number, client)
    return target, build


class AppStoreReviewOperation:
    """One resumable App Review submission with checkpointed recovery.

    The whole flow runs under the per-app checkout lock. The intent is durable
    before the first external call; every remote id (app, version, build,
    submission, submission item), the phase, any blocking state and the
    confirmed result are saved atomically. On every run — fresh or resumed —
    each step re-verifies the remote state with a GET before and after
    mutating, so the local checkpoint phase is never treated as proof.
    """

    def __init__(self, client: "_AscClient", cwd: Path, intent: ReviewIntent):
        self._client = client
        self._cwd = cwd
        self._intent = intent
        self._operation_id = compute_operation_id(intent)

    # -- public entry point ---------------------------------------------------

    def run(self) -> ReviewOutcome:
        path = checkpoint_path(self._cwd, self._operation_id)
        lock = self._acquire_bundle_lock()
        try:
            self._reject_conflicting_operations()
            checkpoint = load_review_checkpoint(path)
            if checkpoint is not None and checkpoint.phase == PHASE_CONFIRMED:
                # Identical confirmed operation: verify it against App Store
                # Connect instead of trusting the local result, then return it.
                return self._verify_confirmed(checkpoint)
            if checkpoint is not None and checkpoint.blocked:
                # A saved blocking state stays until the situation is resolved
                # in App Store Connect; no external calls are made.
                raise UnknownResultError(self._blocked_message(checkpoint))
            if checkpoint is None:
                checkpoint = self._new_checkpoint()
                # The intent is durable before the first external call; if it
                # cannot be saved, nothing touches App Store Connect.
                self._save(path, checkpoint)
            return self._execute(path, checkpoint)
        finally:
            lock.release()

    # -- state machine ----------------------------------------------------------

    def _execute(self, path: Path, checkpoint: ReviewCheckpoint) -> ReviewOutcome:
        # A submitted version is no longer editable. Reconcile before the
        # preparation gate, including a crash after PATCH but before checkpoint save.
        if checkpoint.submission_id is not None:
            submission = get_review_submission(checkpoint.submission_id, self._client)
            if submission is not None and submission_is_submitted(submission_state(submission)):
                self._verify_confirmed(checkpoint)
                return self._complete(path, checkpoint, submission, resumed=True)
        self._ensure_version(path, checkpoint)
        self._ensure_build_binding(path, checkpoint)
        self._ensure_whats_new(path, checkpoint)
        self._ensure_release_type(path, checkpoint)
        self._ensure_phased_release(path, checkpoint)
        self._ensure_submission(path, checkpoint)
        return self._submit(path, checkpoint)

    def _ensure_version(self, path: Path, checkpoint: ReviewCheckpoint) -> None:
        if checkpoint.app_id is None:
            checkpoint.app_id = find_app_id(self._intent.bundle_id, self._client)
            self._save(path, checkpoint)

        # The build is re-verified in ASC on every run: exact app + marketing
        # version + build number lookup, which also enforces the iOS platform,
        # VALID processing and a non-expired build.
        build = find_build(
            checkpoint.app_id,
            self._intent.marketing_version,
            self._intent.build_number,
            self._client,
        )
        if checkpoint.build_id is not None and checkpoint.build_id != build["id"]:
            self._block(
                path,
                checkpoint,
                "build_changed",
                f"App Store Connect reports build {build['id']} for "
                f"{self._intent.marketing_version} ({self._intent.build_number}) while the operation "
                f"recorded build {checkpoint.build_id}",
            )
            return
        if checkpoint.build_id != build["id"]:
            checkpoint.build_id = build["id"]
            self._save(path, checkpoint)

        version = None
        if checkpoint.version_id is not None:
            version = get_app_store_version(checkpoint.version_id, self._client)
            if version is None:
                # The version recorded earlier was deleted remotely; start the
                # version step over through the exact versionString lookup.
                checkpoint.version_id = None
                checkpoint.phase = PHASE_INTENT
                self._save(path, checkpoint)
            elif (
                version_string(version) != self._intent.marketing_version
                or str(version_platform(version) or "").upper() != self._intent.platform.upper()
            ):
                self._block(
                    path,
                    checkpoint,
                    "version_mismatch",
                    f"the recorded version {checkpoint.version_id} no longer matches "
                    f"{self._intent.marketing_version} ({self._intent.platform}) in App Store Connect",
                )
                return
        if version is None:
            version = find_app_store_version(
                checkpoint.app_id, self._intent.marketing_version, self._client, self._intent.platform
            )
        if version is None:
            # Missing versions are created; a lost creation response is
            # reconciled inside the primitive by a read-only re-lookup.
            try:
                version = get_or_create_app_store_version(
                    checkpoint.app_id, self._intent.marketing_version, self._client, self._intent.platform
                )
            except AscAmbiguousResultError as exc:
                self._block(path, checkpoint, "version_create_lost", "version creation remains unconfirmed", exc)
                raise AssertionError("unreachable: _block always raises")
        ensure_version_editable(version)
        if checkpoint.version_id != version["id"]:
            checkpoint.version_id = version["id"]
            checkpoint.phase = PHASE_VERSION_READY
            self._save(path, checkpoint)
        self._advance(path, checkpoint, PHASE_VERSION_READY)

    def _ensure_build_binding(self, path: Path, checkpoint: ReviewCheckpoint) -> None:
        version_id = checkpoint.version_id
        assert version_id is not None and checkpoint.build_id is not None
        current = get_version_build_id(version_id, self._client)
        if current == checkpoint.build_id:
            self._advance(path, checkpoint, PHASE_BUILD_BOUND)  # matching binding is reused
            return
        if current is not None:
            raise BuildConflictError(
                f"App Store version {version_id} already has build {current} selected; CDT does not "
                "overwrite an existing build selection automatically - resolve the version in "
                "App Store Connect"
            )
        self._advance(path, checkpoint, PHASE_BUILD_BOUND)  # durable intent before the PATCH
        try:
            set_version_build(version_id, checkpoint.build_id, self._client)
        except AscAmbiguousResultError as exc:
            current = get_version_build_id(version_id, self._client)
            if current == checkpoint.build_id:
                return  # applied; confirmed by GET without repeating the PATCH
            self._block(
                path,
                checkpoint,
                "build_binding_lost",
                f"the build binding response was lost and version {version_id} is still not bound to "
                f"build {checkpoint.build_id}",
                exc,
            )
            return
        current = get_version_build_id(version_id, self._client)
        if current != checkpoint.build_id:
            self._block(
                path,
                checkpoint,
                "build_binding_lost",
                f"after the binding request version {version_id} is not verifiably bound to build "
                f"{checkpoint.build_id}",
            )

    # -- submission settings --------------------------------------------------

    def _whats_new_applied(self, version_id: str) -> bool:
        remote = get_whats_new(version_id, self._client)
        return all(remote.get(locale) == text for locale, text in self._intent.whats_new.items())

    def _ensure_whats_new(self, path: Path, checkpoint: ReviewCheckpoint) -> None:
        version_id = checkpoint.version_id
        assert version_id is not None
        if self._whats_new_applied(version_id):
            self._advance(path, checkpoint, PHASE_WHATS_NEW_SET)
            return
        self._advance(path, checkpoint, PHASE_WHATS_NEW_SET)  # durable intent before the writes
        try:
            # set_whats_new validates every locale before the first write and
            # patches only the passed localizations; nothing else is touched.
            set_whats_new(version_id, dict(self._intent.whats_new), self._client)
        except AscAmbiguousResultError as exc:
            if self._whats_new_applied(version_id):
                return  # applied; confirmed by GET without repeating the PATCHes
            self._block(
                path,
                checkpoint,
                "whats_new_lost",
                f"the whatsNew update response was lost and version {version_id} does not verifiably "
                "contain the requested texts",
                exc,
            )
            return
        if not self._whats_new_applied(version_id):
            self._block(
                path,
                checkpoint,
                "whats_new_lost",
                f"after the update version {version_id} does not verifiably contain the requested "
                "whatsNew texts",
            )

    def _ensure_release_type(self, path: Path, checkpoint: ReviewCheckpoint) -> None:
        version_id = checkpoint.version_id
        assert version_id is not None
        expected = RELEASE_MODES[self._intent.release_mode]

        def release_type_matches() -> bool:
            version = get_app_store_version(version_id, self._client)
            return version is not None and version_release_type(version) == expected

        if release_type_matches():
            self._advance(path, checkpoint, PHASE_RELEASE_TYPE_SET)
            return
        self._advance(path, checkpoint, PHASE_RELEASE_TYPE_SET)  # durable intent before the PATCH
        try:
            set_release_type(version_id, self._intent.release_mode, self._client)
        except AscAmbiguousResultError as exc:
            if release_type_matches():
                return  # applied; confirmed by GET without repeating the PATCH
            self._block(
                path,
                checkpoint,
                "release_type_lost",
                f"the releaseType update response was lost and version {version_id} does not "
                f"verifiably use {expected}",
                exc,
            )
            return
        if not release_type_matches():
            self._block(
                path,
                checkpoint,
                "release_type_lost",
                f"after the update version {version_id} does not verifiably use releaseType {expected}",
            )

    def _ensure_phased_release(self, path: Path, checkpoint: ReviewCheckpoint) -> None:
        version_id = checkpoint.version_id
        assert version_id is not None
        desired = self._intent.phased_release

        def phased_matches() -> bool:
            return (get_phased_release(version_id, self._client) is not None) == desired

        if phased_matches():
            self._advance(path, checkpoint, PHASE_PHASED_RELEASE_SET)
            return
        self._advance(path, checkpoint, PHASE_PHASED_RELEASE_SET)  # durable intent before the change
        try:
            # Creation of a missing phased release is reconciled inside the
            # primitive; a lost DELETE response surfaces here.
            set_phased_release(version_id, desired, self._client)
        except AscAmbiguousResultError as exc:
            if phased_matches():
                return  # applied; confirmed by GET without repeating the request
            self._block(
                path,
                checkpoint,
                "phased_release_lost",
                f"the phased release response was lost and version {version_id} does not verifiably "
                f"have {'the seven-day phased release' if desired else 'no phased release'}",
                exc,
            )
            return
        if not phased_matches():
            self._block(
                path,
                checkpoint,
                "phased_release_lost",
                f"after the update version {version_id} does not verifiably have "
                f"{'the seven-day phased release' if desired else 'no phased release'}",
            )

    # -- review submission ------------------------------------------------------

    def _ensure_submission(self, path: Path, checkpoint: ReviewCheckpoint) -> None:
        submission = None
        if checkpoint.submission_id is not None:
            submission = get_review_submission(checkpoint.submission_id, self._client)
            if submission is None:
                # The recorded draft disappeared remotely; start the submission
                # step over through the open-submission lookup.
                checkpoint.submission_id = None
                checkpoint.submission_item_id = None
                checkpoint.phase = PHASE_PHASED_RELEASE_SET
                self._save(path, checkpoint)

        if submission is not None and submission_is_submitted(submission_state(submission)):
            # The submit request was already applied (e.g. its response was
            # lost, or a previous run ended before the confirmation was saved).
            # Verify the composition, then confirm without submitting again.
            self._reject_foreign_items(checkpoint, submission["id"], include_for_review=True)
            self._advance(path, checkpoint, PHASE_SUBMITTED)
            return

        if submission is None:
            submission = self._find_or_create_draft(path, checkpoint)
        else:
            self._reject_foreign_submission(checkpoint, submission)
        submission_id = submission["id"]
        if checkpoint.submission_id != submission_id:
            checkpoint.submission_id = submission_id
            checkpoint.submission_item_id = None
            self._save(path, checkpoint)
        checkpoint.submission_item_id = self._ensure_submission_item(path, checkpoint, submission_id)
        self._advance(path, checkpoint, PHASE_SUBMISSION_READY)

    def _find_or_create_draft(self, path: Path, checkpoint: ReviewCheckpoint) -> dict:
        existing = find_open_review_submission(
            checkpoint.app_id or "", self._client, self._intent.platform
        )
        if existing is not None:
            self._reject_foreign_submission(checkpoint, existing)
            return existing
        self._advance(path, checkpoint, PHASE_SUBMISSION_READY)  # durable intent before the POST
        try:
            return create_review_submission(
                checkpoint.app_id or "", self._client, self._intent.platform
            )
        except AscAmbiguousResultError as exc:
            existing = find_open_review_submission(
                checkpoint.app_id or "", self._client, self._intent.platform
            )
            if existing is not None:
                # Creation has no version relationship: the draft may still be
                # empty. Apply the same composition checks as ordinary adoption.
                self._reject_foreign_submission(checkpoint, existing)
                self._reject_foreign_items(checkpoint, existing["id"], include_for_review=False)
                return existing  # adopted; confirmed by GET without repeating the POST
            self._block(
                path,
                checkpoint,
                "submission_create_lost",
                "the review submission creation response was lost and no matching open submission "
                "can be verified in App Store Connect",
                exc,
            )
            raise AssertionError("unreachable: _block always raises")  # keeps flow explicit

    def _ensure_submission_item(self, path: Path, checkpoint: ReviewCheckpoint, submission_id: str) -> str:
        items = get_review_submission_items(submission_id, self._client)
        self._reject_foreign_items(checkpoint, submission_id, include_for_review=False, items=items)
        ours = find_version_item(items, checkpoint.version_id or "")
        if ours is not None:
            return ours["id"]
        self._advance(path, checkpoint, PHASE_SUBMISSION_READY)  # durable intent before the POST
        try:
            item = add_review_submission_item(submission_id, checkpoint.version_id or "", self._client)
        except AscAmbiguousResultError as exc:
            self._block(path, checkpoint, "item_create_lost", "submission item creation remains unconfirmed", exc)
            raise AssertionError("unreachable: _block always raises")
        return item["id"]

    def _submit(self, path: Path, checkpoint: ReviewCheckpoint) -> ReviewOutcome:
        assert checkpoint.submission_id is not None
        submission = get_review_submission(checkpoint.submission_id, self._client)
        if submission is None:
            self._block(
                path,
                checkpoint,
                "submission_missing",
                f"the recorded submission {checkpoint.submission_id} no longer exists in "
                "App Store Connect",
            )
            raise AssertionError("unreachable: _block always raises")
        did_submit_here = False
        if not submission_is_submitted(submission_state(submission)):
            # Never submit a submission that contains other versions' items.
            self._reject_foreign_items(checkpoint, submission["id"], include_for_review=True)
            self._advance(path, checkpoint, PHASE_SUBMITTED)  # durable intent before the submit PATCH
            did_submit_here = True
            try:
                submission = submit_review_submission(
                    checkpoint.submission_id, self._client
                )
            except AscAmbiguousResultError as exc:
                submission = get_review_submission(checkpoint.submission_id, self._client)
                if submission is not None and submission_is_submitted(submission_state(submission)):
                    pass  # applied; confirmed by GET without repeating the PATCH
                else:
                    self._block(
                        path,
                        checkpoint,
                        "submit_lost",
                        "the submit response was lost and the submission is still in an unsubmitted "
                        "stage",
                        exc,
                    )
                    raise AssertionError("unreachable: _block always raises")
        state = submission_state(submission)
        if not submission_is_submitted(state):
            self._block(
                path,
                checkpoint,
                "submit_unconfirmed",
                f"the submission is still in state {state!r}, so Apple's acceptance is not proven",
            )
            raise AssertionError("unreachable: _block always raises")
        return self._complete(path, checkpoint, submission, resumed=not did_submit_here)

    # -- completion and blocking ---------------------------------------------

    def _complete(
        self, path: Path, checkpoint: ReviewCheckpoint, submission: dict, *, resumed: bool
    ) -> ReviewOutcome:
        state = submission_state(submission) or ""
        checkpoint.phase = PHASE_CONFIRMED
        checkpoint.submission_id = submission["id"]
        checkpoint.blocked = None
        checkpoint.result = {
            "bundle_id": checkpoint.bundle_id,
            "app_id": checkpoint.app_id,
            "version_id": checkpoint.version_id,
            "build_id": checkpoint.build_id,
            "marketing_version": checkpoint.marketing_version,
            "build_number": checkpoint.build_number,
            "submission_id": submission["id"],
            "submission_state": state,
            "release_mode": checkpoint.release_mode,
            "release_type": RELEASE_MODES[checkpoint.release_mode],
            "phased_release": checkpoint.phased_release,
        }
        self._save(path, checkpoint)
        return ReviewOutcome(
            operation_id=checkpoint.operation_id,
            bundle_id=checkpoint.bundle_id,
            app_id=checkpoint.app_id or "",
            version_id=checkpoint.version_id or "",
            build_id=checkpoint.build_id or "",
            marketing_version=checkpoint.marketing_version,
            build_number=checkpoint.build_number,
            submission_id=submission["id"],
            submission_state=state,
            release_mode=checkpoint.release_mode,
            release_type=RELEASE_MODES[checkpoint.release_mode],
            phased_release=checkpoint.phased_release,
            resumed=resumed,
        )

    def _verify_confirmed(self, checkpoint: ReviewCheckpoint) -> ReviewOutcome:
        """Re-verify a confirmed checkpoint against ASC before reusing its result.

        The submitted matching submission completes the operation; a missing
        submission, a foreign version or a submission back in an unsubmitted
        stage means the local record can no longer be trusted.
        """
        submission = (
            get_review_submission(checkpoint.submission_id, self._client)
            if checkpoint.submission_id
            else None
        )
        if submission is None:
            raise UnknownResultError(
                f"App Store review operation {checkpoint.operation_id} is recorded as confirmed but its "
                "submission cannot be found in App Store Connect; verify the submission in App Store "
                "Connect before doing anything else"
            )
        state = submission_state(submission)
        if not submission_is_submitted(state):
            raise UnknownResultError(
                f"App Store review operation {checkpoint.operation_id} is recorded as confirmed but the "
                f"submission is in state {state!r}; verify the submission in App Store Connect before "
                "doing anything else"
            )
        for_review = submission_version_id(submission)
        if for_review is not None and for_review != checkpoint.version_id:
            raise UnknownResultError(
                f"App Store review operation {checkpoint.operation_id} is recorded as confirmed but the "
                f"submission targets version {for_review}; verify the submission in App Store Connect "
                "before doing anything else"
            )
        self._reject_foreign_items(checkpoint, submission["id"], include_for_review=True)
        version_id = checkpoint.version_id or ""
        version = get_app_store_version(version_id, self._client)
        if (
            version is None
            or version_string(version) != checkpoint.marketing_version
            or version_platform(version) != checkpoint.platform
            or get_version_build_id(version_id, self._client) != checkpoint.build_id
            or version_release_type(version) != RELEASE_MODES[checkpoint.release_mode]
            or not self._whats_new_applied(version_id)
        ):
            raise UnknownResultError(
                "Submitted version/build/settings no longer match the saved operation; "
                "verify the version in App Store Connect before doing anything else"
            )
        result = checkpoint.result or {}
        return ReviewOutcome(
            operation_id=checkpoint.operation_id,
            bundle_id=checkpoint.bundle_id,
            app_id=checkpoint.app_id or str(result.get("app_id") or ""),
            version_id=checkpoint.version_id or str(result.get("version_id") or ""),
            build_id=checkpoint.build_id or str(result.get("build_id") or ""),
            marketing_version=checkpoint.marketing_version,
            build_number=checkpoint.build_number,
            submission_id=submission["id"],
            submission_state=state or "",
            release_mode=checkpoint.release_mode,
            release_type=RELEASE_MODES[checkpoint.release_mode],
            phased_release=checkpoint.phased_release,
            resumed=True,
        )

    def _reject_foreign_submission(self, checkpoint: ReviewCheckpoint, submission: dict) -> None:
        """Refuse foreign submissions and states unsafe to submit again."""
        state = submission_state(submission)
        if (
            state not in appstore_review.UNSUBMITTED_REVIEW_SUBMISSION_STATES
            and not submission_is_submitted(state)
        ):
            raise SubmissionConflictError(
                f"Review submission {submission['id']} is in state {state!r}; "
                "resolve it in App Store Connect before submitting"
            )
        for_review = submission_version_id(submission)
        if for_review is not None and for_review != checkpoint.version_id:
            raise SubmissionConflictError(
                f"Review submission {submission['id']} was created for version {for_review}; CDT does "
                "not add the version to a foreign submission and does not submit it automatically - "
                "resolve the submission in App Store Connect"
            )

    def _reject_foreign_items(
        self,
        checkpoint: ReviewCheckpoint,
        submission_id: str,
        *,
        include_for_review: bool,
        items: list[dict] | None = None,
    ) -> None:
        """Refuse to add into / submit / claim a submission with foreign items."""
        if items is None:
            items = get_review_submission_items(submission_id, self._client)
        foreign = [
            str(item["id"])
            for item in items
            if item_version_id(item) != checkpoint.version_id
        ]
        if foreign:
            raise SubmissionConflictError(
                f"Review submission {submission_id} contains items for other versions "
                f"({', '.join(foreign)}); CDT does not add the version to a foreign submission and "
                "does not submit it automatically - resolve the submission in App Store Connect"
            )
        if include_for_review:
            if len(items) != 1 or item_version_id(items[0]) != checkpoint.version_id:
                raise SubmissionConflictError(
                    f"Review submission {submission_id} must contain exactly the target version item"
                )
            submission = get_review_submission(submission_id, self._client)
            if submission is not None:
                self._reject_foreign_submission(checkpoint, submission)

    # -- checkpoint bookkeeping -------------------------------------------------

    def _new_checkpoint(self) -> ReviewCheckpoint:
        now = _now()
        return ReviewCheckpoint(
            operation_id=self._operation_id,
            bundle_id=self._intent.bundle_id,
            platform=self._intent.platform,
            marketing_version=self._intent.marketing_version,
            build_number=self._intent.build_number,
            whats_new=dict(self._intent.whats_new),
            release_mode=self._intent.release_mode,
            phased_release=self._intent.phased_release,
            phase=PHASE_INTENT,
            created_at=now,
            updated_at=now,
        )

    def _save(self, path: Path, checkpoint: ReviewCheckpoint) -> None:
        # A checkpoint write failure always stops submission: no external
        # mutation may happen without its durable intent.
        checkpoint.updated_at = _now()
        save_review_checkpoint(path, checkpoint)

    def _advance(self, path: Path, checkpoint: ReviewCheckpoint, phase: str) -> None:
        if _PHASE_ORDER[phase] > _PHASE_ORDER[checkpoint.phase]:
            checkpoint.phase = phase
            self._save(path, checkpoint)

    def _block(
        self,
        path: Path,
        checkpoint: ReviewCheckpoint,
        category: str,
        detail: str,
        exc: BaseException | None = None,
    ) -> None:
        """Persist a blocking state and stop: the ambiguity requires human verification."""
        checkpoint.blocked = {"category": category, "detail": detail, "at": _now()}
        self._save(path, checkpoint)
        raise UnknownResultError(self._blocked_message(checkpoint)) from exc

    def _blocked_message(self, checkpoint: ReviewCheckpoint) -> str:
        blocked = checkpoint.blocked or {}
        return (
            f"App Store review operation {checkpoint.operation_id} is blocked: "
            f"{blocked.get('detail') or 'unknown reason'} (checkpoint "
            f".cdt/appstore/operations/{checkpoint.operation_id}.json). Verify the version and the "
            "submission in App Store Connect before doing anything else; do not rerun blindly and do "
            "not delete the checkpoint to force a retry"
        )

    # -- checkout-local coordination ----------------------------------------------

    def _acquire_bundle_lock(self) -> filelock.FileLock:
        path = review_lock_path(self._cwd, self._intent.bundle_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = filelock.FileLock(path, timeout=0, thread_local=False)
        try:
            lock.acquire()
        except filelock.Timeout as exc:
            raise OperationLockedError(
                f"another App Store review operation for {self._intent.bundle_id} is already running "
                f"in this checkout (lock file {path.name} is held)"
            ) from exc
        return lock

    def _reject_conflicting_operations(self) -> None:
        """Stop on unfinished operations of the same app with other parameters."""
        directory = operations_dir(self._cwd)
        if not directory.is_dir():
            return
        own_name = f"{self._operation_id}.json"
        for candidate in sorted(directory.glob("*.json")):
            if candidate.name == own_name:
                continue
            other = load_review_checkpoint(candidate)  # corrupted files stop submission here
            if other.bundle_id != self._intent.bundle_id or other.phase == PHASE_CONFIRMED:
                continue
            raise ConflictingOperationError(
                f"unfinished App Store review operation {other.operation_id} for {other.bundle_id} "
                f"(phase {other.phase!r}, version {other.marketing_version} ({other.build_number})) "
                "blocks a submission with changed parameters; resolve it before submitting"
            )
