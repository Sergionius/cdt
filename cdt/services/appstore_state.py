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
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import appstore_review

if TYPE_CHECKING:
    from .appstore import _AscClient

UPLOADS_SUBDIR = Path(".cdt") / "appstore" / "uploads"

RECORD_SCHEMA_VERSION = 1

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


def bundle_key(bundle_id: str) -> str:
    """Return the stable record file key: the SHA-256 of the bundle id."""
    return hashlib.sha256(bundle_id.encode("utf-8")).hexdigest()


def uploads_dir(cwd: Path) -> Path:
    """Directory holding upload records, separate from ctx.values."""
    return cwd / UPLOADS_SUBDIR


def record_path(cwd: Path, bundle_id: str) -> Path:
    return uploads_dir(cwd) / f"{bundle_key(bundle_id)}.json"


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
