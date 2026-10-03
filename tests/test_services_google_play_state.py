"""Scenario tests for the persistable Google Play publication operation.

Every scenario runs against an in-memory fake of the Android Publisher API and
temporary directories: no credentials, no network and no real Google Play. The
fake models edit lifetime and commit semantics, so lost responses, expired
edits and crashes between remote success and local record are all reproducible.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import filelock
import pytest

from cdt.services import google_play, google_play_state
from cdt.services.google_play import (
    STAGE_BUNDLE_UPLOAD,
    STAGE_EDIT_COMMIT,
    STAGE_EDIT_GET,
    STAGE_TRACK_GET,
    GooglePlayError,
)
from cdt.services.google_play_state import (
    PHASE_CONFIRMED,
    PHASE_EDIT_CREATED,
    PHASE_INTENT,
    PHASE_TRACK_UPDATED,
    PHASE_UPLOADED,
    CheckpointError,
    CheckpointWriteError,
    ConflictingOperationError,
    GooglePlayStateError,
    OperationLockedError,
    PublishCheckpoint,
    PublishIntent,
    TrackConflictError,
    UnknownResultError,
    UploadMismatchError,
    checkpoint_path,
    compute_operation_id,
    load_checkpoint,
)

PACKAGE = "com.example.app"
OTHER_PACKAGE = "com.other.app"
AAB_BYTES = b"android-app-bundle-bytes"


def completed_release(version_code: int, name: str | None = None) -> dict[str, Any]:
    release: dict[str, Any] = {"status": "completed", "versionCodes": [version_code]}
    if name is not None:
        release["name"] = name
    return release


class FakePlayBackend:
    """In-memory Android Publisher state with edit and commit semantics."""

    def __init__(self) -> None:
        self.committed_bundles: dict[int, dict[str, Any]] = {}
        self.tracks: dict[str, list[dict[str, Any]]] = {}
        self.edits: dict[str, dict[str, Any]] = {}
        self.committed_edits: list[str] = []
        self.calls: list[str] = []
        self.before_call: Any = None
        self.failures: dict[str, list[tuple[Exception, str | None]]] = {}
        self.next_version_code = 40
        self.upload_sha_override: str | None = None
        self._edit_counter = 0

    # -- test configuration ----------------------------------------------------

    def set_track(self, track: str, releases: list[dict[str, Any]]) -> None:
        self.tracks[track] = releases

    def add_committed_bundle(self, version_code: int, sha256: str) -> None:
        self.committed_bundles[version_code] = {"versionCode": version_code, "sha256": sha256}

    def fail_once(self, label: str, error: Exception, mode: str | None = None) -> None:
        """Fail the next call of ``label``.

        Modes: ``None`` fail before anything happens; ``"landed"`` (upload) the
        bundle lands in the edit before the response is lost; ``"applied"``
        (commit) the edit is committed before the response is lost.
        """
        self.failures.setdefault(label, []).append((error, mode))

    # -- fake internals ----------------------------------------------------------

    def _record(self, label: str) -> None:
        self.calls.append(label)
        if self.before_call is not None:
            self.before_call(label)

    def _failure(self, label: str) -> tuple[Exception, str | None] | None:
        pending = self.failures.get(label)
        if pending:
            return pending.pop(0)
        return None


class FakePlayClient:
    """Duck-typed stand-in for GooglePlayClient backed by FakePlayBackend."""

    def __init__(self, backend: FakePlayBackend):
        self.backend = backend

    def create_edit(self, package_name: str) -> dict[str, Any]:
        backend = self.backend
        backend._record("edits.insert")
        failure = backend._failure("edits.insert")
        if failure is not None:
            raise failure[0]
        backend._edit_counter += 1
        edit_id = f"edit-{backend._edit_counter}"
        backend.edits[edit_id] = {
            "bundles": dict(backend.committed_bundles),
            "tracks": {track: list(releases) for track, releases in backend.tracks.items()},
        }
        return {"id": edit_id, "expiryTimeSeconds": "43200"}

    def get_edit(self, package_name: str, edit_id: str) -> dict[str, Any]:
        backend = self.backend
        backend._record("edits.get")
        failure = backend._failure("edits.get")
        if failure is not None:
            raise failure[0]
        if edit_id not in backend.edits:
            raise GooglePlayError(STAGE_EDIT_GET, f"edit not found: {edit_id}", http_status=404)
        return {"id": edit_id, "expiryTimeSeconds": "43200"}

    def delete_edit(self, package_name: str, edit_id: str) -> None:
        backend = self.backend
        backend._record("edits.delete")
        failure = backend._failure("edits.delete")
        if failure is not None:
            raise failure[0]
        backend.edits.pop(edit_id, None)

    def list_bundles(self, package_name: str, edit_id: str) -> list[dict[str, Any]]:
        backend = self.backend
        backend._record("edits.bundles.list")
        failure = backend._failure("edits.bundles.list")
        if failure is not None:
            raise failure[0]
        edit = backend.edits[edit_id]
        return [dict(bundle) for _, bundle in sorted(edit["bundles"].items())]

    def upload_bundle(self, package_name: str, edit_id: str, aab_path: Path) -> dict[str, Any]:
        backend = self.backend
        backend._record("edits.bundles.upload")
        failure = backend._failure("edits.bundles.upload")
        if failure is not None:
            error, mode = failure
            if mode == "landed":
                self._land_bundle(edit_id, aab_path)
            raise error
        return dict(self._land_bundle(edit_id, aab_path))

    def get_track(self, package_name: str, edit_id: str, track: str) -> dict[str, Any]:
        backend = self.backend
        backend._record("edits.tracks.get")
        failure = backend._failure("edits.tracks.get")
        if failure is not None:
            raise failure[0]
        edit = backend.edits[edit_id]
        if track not in edit["tracks"]:
            raise GooglePlayError(
                STAGE_TRACK_GET, f"No track found for track name: {track}.", http_status=404
            )
        return {"track": track, "releases": [dict(release) for release in edit["tracks"][track]]}

    def update_track(
        self, package_name: str, edit_id: str, track: str, releases: list[dict[str, Any]]
    ) -> dict[str, Any]:
        backend = self.backend
        backend._record("edits.tracks.update")
        failure = backend._failure("edits.tracks.update")
        if failure is not None:
            error, mode = failure
            if mode == "landed":
                backend.edits[edit_id]["tracks"][track] = [dict(release) for release in releases]
            raise error
        backend.edits[edit_id]["tracks"][track] = [dict(release) for release in releases]
        return {"track": track, "releases": [dict(release) for release in releases]}

    def commit_edit(self, package_name: str, edit_id: str) -> dict[str, Any]:
        backend = self.backend
        backend._record("edits.commit")
        failure = backend._failure("edits.commit")
        if failure is not None:
            error, mode = failure
            if mode == "applied":
                self._apply_commit(edit_id)
            raise error
        self._apply_commit(edit_id)
        return {"id": edit_id}

    # -- fake internals ----------------------------------------------------------

    def _land_bundle(self, edit_id: str, aab_path: Path) -> dict[str, Any]:
        backend = self.backend
        sha256 = backend.upload_sha_override or google_play.compute_file_sha256(aab_path)
        version_code = backend.next_version_code
        backend.next_version_code += 1
        bundle = {"versionCode": version_code, "sha256": sha256}
        backend.edits[edit_id]["bundles"][version_code] = bundle
        return bundle

    def _apply_commit(self, edit_id: str) -> None:
        backend = self.backend
        edit = backend.edits.pop(edit_id)
        backend.committed_bundles.update(edit["bundles"])
        backend.tracks.update(edit["tracks"])
        backend.committed_edits.append(edit_id)


def lost_response(stage: str) -> GooglePlayError:
    return GooglePlayError(stage, "request timed out")


@pytest.fixture
def backend() -> FakePlayBackend:
    return FakePlayBackend()


@pytest.fixture
def aab(tmp_path: Path) -> Path:
    path = tmp_path / "app-release.aab"
    path.write_bytes(AAB_BYTES)
    return path


def make_intent(**overrides: Any) -> PublishIntent:
    params: dict[str, Any] = {
        "package_name": PACKAGE,
        "track": "internal",
        "release_status": "completed",
    }
    params.update(overrides)
    return PublishIntent(**params)


def make_operation(
    backend: FakePlayBackend, tmp_path: Path, intent: PublishIntent, aab: Path
) -> google_play_state.GooglePlayPublishOperation:
    return google_play_state.GooglePlayPublishOperation(
        FakePlayClient(backend), tmp_path, intent, aab
    )


def seed_checkpoint(
    tmp_path: Path, intent: PublishIntent, aab: Path, **overrides: Any
) -> PublishCheckpoint:
    """Persist a checkpoint for ``intent``/``aab`` as if a previous run wrote it."""
    sha256 = google_play.compute_file_sha256(aab)
    operation_id = compute_operation_id(intent, sha256)
    checkpoint = PublishCheckpoint(
        operation_id=operation_id,
        package_name=intent.package_name,
        track=intent.track,
        release_status=intent.release_status,
        release_notes=intent.release_notes,
        release_name=intent.release_name,
        user_fraction=intent.user_fraction,
        aab_sha256=sha256,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        **overrides,
    )
    google_play_state.save_checkpoint(checkpoint_path(tmp_path, operation_id), checkpoint)
    return checkpoint


# -- operation identity and checkpoint format -------------------------------------


def test_operation_id_binds_canonical_parameters_and_aab_hash(tmp_path):
    aab = tmp_path / "app.aab"
    aab.write_bytes(AAB_BYTES)
    sha = google_play.compute_file_sha256(aab)
    intent = make_intent(release_notes={"en-US": "Fix"})

    identical = compute_operation_id(make_intent(release_notes={"en-US": "Fix"}), sha)
    assert identical == compute_operation_id(intent, sha)
    assert identical != compute_operation_id(intent, "f" * 64)
    assert identical != compute_operation_id(make_intent(track="beta"), sha)
    assert identical != compute_operation_id(make_intent(release_status="draft"), sha)
    assert identical != compute_operation_id(make_intent(user_fraction=0.5), sha)
    assert identical != compute_operation_id(make_intent(release_notes={"de-DE": "Fix"}), sha)


def test_checkpoint_roundtrip_preserves_non_secret_state(tmp_path):
    checkpoint = PublishCheckpoint(
        operation_id="op",
        package_name=PACKAGE,
        track="internal",
        release_status="inProgress",
        release_notes={"en-US": "Fixes"},
        release_name="1.2.3",
        user_fraction=0.25,
        aab_sha256="a" * 64,
        phase=PHASE_UPLOADED,
        edit_id="edit-1",
        edit_expiry_seconds="43200",
        version_code=42,
        source_release=completed_release(11),
    )
    path = tmp_path / "op.json"
    google_play_state.save_checkpoint(path, checkpoint)

    assert load_checkpoint(path) == checkpoint


def test_checkpoint_loader_rejects_corruption_and_unknown_versions(tmp_path):
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")
    with pytest.raises(CheckpointError):
        load_checkpoint(corrupt)

    unknown = tmp_path / "unknown.json"
    unknown.write_text(json.dumps({"schema_version": 99, "operation_id": "op"}), encoding="utf-8")
    with pytest.raises(CheckpointError):
        load_checkpoint(unknown)

    incomplete = tmp_path / "incomplete.json"
    incomplete.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
    with pytest.raises(CheckpointError):
        load_checkpoint(incomplete)

    assert load_checkpoint(tmp_path / "missing.json") is None


# -- happy paths -------------------------------------------------------------------


@pytest.mark.parametrize(
    "track_setup",
    [
        pytest.param("missing", id="missing-track"),
        pytest.param("empty", id="empty-track"),
        pytest.param("completed", id="single-completed-release"),
    ],
)
def test_happy_path_completed_uses_google_version_code(tmp_path, backend, aab, track_setup):
    if track_setup == "completed":
        backend.set_track("internal", [completed_release(11, name="1.0.0")])
    elif track_setup == "empty":
        backend.set_track("internal", [])
    intent = make_intent()
    operation = make_operation(backend, tmp_path, intent, aab)

    outcome = operation.run()

    # The version code comes from the Google upload response (40), nothing else.
    assert outcome.version_code == 40
    assert outcome.changes_sent_for_review is True
    assert outcome.resumed is False
    assert backend.calls == [
        "edits.insert",
        "edits.tracks.get",
        "edits.bundles.list",
        "edits.bundles.upload",
        "edits.tracks.update",
        "edits.commit",
    ]
    checkpoint = load_checkpoint(checkpoint_path(tmp_path, outcome.operation_id))
    assert checkpoint.phase == PHASE_CONFIRMED
    assert checkpoint.result == {
        "version_code": 40,
        "aab_sha256": google_play.compute_file_sha256(aab),
        "track": "internal",
        "release_status": "completed",
        "changes_sent_for_review": True,
    }
    assert backend.committed_bundles[40]["sha256"] == google_play.compute_file_sha256(aab)


def test_draft_keeps_completed_base_release_and_does_not_send_for_review(tmp_path, backend, aab):
    backend.set_track("internal", [completed_release(11, name="1.0.0")])
    intent = make_intent(release_status="draft", release_notes={"en-US": "New draft"})
    operation = make_operation(backend, tmp_path, intent, aab)

    outcome = operation.run()

    assert outcome.changes_sent_for_review is False
    # The retained completed release is passed through untouched, next to the
    # new draft; the draft contains only its own version code.
    assert backend.committed_edits, "a draft publication still commits the edit to persist the draft"
    checkpoint = load_checkpoint(checkpoint_path(tmp_path, outcome.operation_id))
    assert checkpoint.source_release == completed_release(11, name="1.0.0")
    assert backend.tracks["internal"] == [
        completed_release(11, name="1.0.0"),
        {
            "status": "draft",
            "versionCodes": [40],
            "releaseNotes": [{"language": "en-US", "text": "New draft"}],
        },
    ]


def test_staged_rollout_keeps_completed_base_release(tmp_path, backend, aab):
    backend.set_track("internal", [completed_release(11, name="1.0.0")])
    intent = make_intent(release_status="inProgress", user_fraction=0.25)
    operation = make_operation(backend, tmp_path, intent, aab)

    outcome = operation.run()

    assert outcome.version_code == 40
    assert backend.tracks["internal"] == [
        completed_release(11, name="1.0.0"),
        {"status": "inProgress", "versionCodes": [40], "userFraction": 0.25},
    ]


def test_completed_release_replaces_old_without_merging_version_codes(tmp_path, backend, aab):
    backend.set_track("internal", [completed_release(11, name="1.0.0")])
    operation = make_operation(backend, tmp_path, make_intent(), aab)

    operation.run()

    assert backend.tracks["internal"] == [{"status": "completed", "versionCodes": [40]}]


@pytest.mark.parametrize(
    ("status_options", "expected_release"),
    [
        pytest.param(
            {"release_status": "draft", "release_notes": {"en-US": "New draft"}},
            {
                "status": "draft",
                "versionCodes": [40],
                "releaseNotes": [{"language": "en-US", "text": "New draft"}],
            },
            id="draft",
        ),
        pytest.param(
            {"release_status": "inProgress", "user_fraction": 0.25},
            {"status": "inProgress", "versionCodes": [40], "userFraction": 0.25},
            id="staged-rollout",
        ),
        pytest.param(
            {"release_status": "completed"},
            {"status": "completed", "versionCodes": [40]},
            id="completed",
        ),
    ],
)
@pytest.mark.parametrize("track_setup", [pytest.param("missing"), pytest.param("empty")])
def test_new_release_on_track_without_completed_base_stands_alone(
    tmp_path, backend, aab, status_options, expected_release, track_setup
):
    if track_setup == "empty":
        backend.set_track("internal", [])
    options = dict(status_options)
    if options["release_status"] == "completed":
        options.pop("release_notes", None)
    intent = make_intent(**options)
    operation = make_operation(backend, tmp_path, intent, aab)

    outcome = operation.run()

    assert outcome.changes_sent_for_review is (intent.release_status != "draft")
    assert backend.tracks["internal"] == [expected_release]
    checkpoint = load_checkpoint(checkpoint_path(tmp_path, outcome.operation_id))
    assert checkpoint.source_release is None


def test_draft_and_staged_rollout_retain_the_previous_completed_release(tmp_path, backend, aab):
    attempts = (
        ("draft", {"release_notes": {"en-US": "Next"}}),
        ("inProgress", {"user_fraction": 0.5}),
    )
    for release_status, extra in attempts:
        other_aab = tmp_path / f"app-{release_status}.aab"
        other_aab.write_bytes(AAB_BYTES + release_status.encode("utf-8"))
        backend.set_track("beta", [completed_release(11, name="1.0.0")])
        intent = make_intent(track="beta", release_status=release_status, **extra)
        operation = make_operation(backend, tmp_path, intent, other_aab)

        outcome = operation.run()

        statuses = [release["status"] for release in backend.tracks["beta"]]
        assert statuses == ["completed", release_status], release_status
        retained, new_release = backend.tracks["beta"]
        assert retained == completed_release(11, name="1.0.0"), release_status
        assert new_release["versionCodes"] == [outcome.version_code]
        assert new_release["status"] == release_status
        if release_status == "inProgress":
            assert new_release["userFraction"] == 0.5
        checkpoint = load_checkpoint(checkpoint_path(tmp_path, outcome.operation_id))
        assert checkpoint.source_release == completed_release(11, name="1.0.0")


def test_intent_checkpoint_exists_before_first_external_call(tmp_path, backend, aab):
    intent = make_intent()
    sha = google_play.compute_file_sha256(aab)
    path = checkpoint_path(tmp_path, compute_operation_id(intent, sha))
    observed: dict[str, bool] = {}

    def before_call(label: str) -> None:
        if label == "edits.insert":
            checkpoint = load_checkpoint(path)
            observed["intent_first"] = checkpoint is not None and checkpoint.phase == PHASE_INTENT

    backend.before_call = before_call
    make_operation(backend, tmp_path, intent, aab).run()

    assert observed["intent_first"] is True


# -- upload stage -----------------------------------------------------------------


def test_upload_response_hash_mismatch_stops_before_track_update(tmp_path, backend, aab):
    backend.upload_sha_override = "ff" * 32
    operation = make_operation(backend, tmp_path, make_intent(), aab)

    with pytest.raises(UploadMismatchError) as excinfo:
        operation.run()

    assert "does not match the local AAB" in str(excinfo.value)
    assert "edits.tracks.update" not in backend.calls
    assert "edits.commit" not in backend.calls
    sha = google_play.compute_file_sha256(aab)
    checkpoint = load_checkpoint(checkpoint_path(tmp_path, compute_operation_id(make_intent(), sha)))
    assert checkpoint.phase == PHASE_EDIT_CREATED


def test_lost_upload_response_is_resolved_by_unique_bundle_match(tmp_path, backend, aab):
    backend.fail_once("edits.bundles.upload", lost_response(STAGE_BUNDLE_UPLOAD), mode="landed")
    operation = make_operation(backend, tmp_path, make_intent(), aab)

    outcome = operation.run()

    assert outcome.version_code == 40
    assert backend.calls.count("edits.bundles.upload") == 1
    checkpoint = load_checkpoint(checkpoint_path(tmp_path, outcome.operation_id))
    assert checkpoint.phase == PHASE_CONFIRMED


def test_lost_upload_response_without_bundle_stops_without_reupload(tmp_path, backend, aab):
    backend.fail_once("edits.bundles.upload", lost_response(STAGE_BUNDLE_UPLOAD))
    operation = make_operation(backend, tmp_path, make_intent(), aab)

    with pytest.raises(UploadMismatchError) as excinfo:
        operation.run()

    assert "without re-uploading" in str(excinfo.value)
    assert backend.calls.count("edits.bundles.upload") == 1
    assert "edits.tracks.update" not in backend.calls
    assert "edits.commit" not in backend.calls


def test_duplicate_version_code_error_is_never_treated_as_success(tmp_path, backend, aab):
    backend.add_committed_bundle(11, "old" * 16)
    backend.fail_once(
        "edits.bundles.upload",
        GooglePlayError(STAGE_BUNDLE_UPLOAD, "Version code 40 has already been used", http_status=403),
    )
    operation = make_operation(backend, tmp_path, make_intent(), aab)

    with pytest.raises(GooglePlayError) as excinfo:
        operation.run()

    assert excinfo.value.http_status == 403
    assert not isinstance(excinfo.value, UnknownResultError)
    assert "edits.commit" not in backend.calls
    sha = google_play.compute_file_sha256(aab)
    checkpoint = load_checkpoint(checkpoint_path(tmp_path, compute_operation_id(make_intent(), sha)))
    assert checkpoint.phase != PHASE_CONFIRMED


# -- lost track update ---------------------------------------------------------------


def test_lost_track_update_response_with_matching_state_continues(tmp_path, backend, aab):
    backend.fail_once("edits.tracks.update", lost_response("track_update"), mode="landed")
    operation = make_operation(backend, tmp_path, make_intent(), aab)

    outcome = operation.run()

    assert outcome.version_code == 40
    assert backend.calls.count("edits.tracks.update") == 1
    checkpoint = load_checkpoint(checkpoint_path(tmp_path, outcome.operation_id))
    assert checkpoint.phase == PHASE_CONFIRMED


def test_lost_track_update_response_with_conflicting_state_stops(tmp_path, backend, aab):
    backend.fail_once("edits.tracks.update", lost_response("track_update"))
    operation = make_operation(backend, tmp_path, make_intent(), aab)

    with pytest.raises(UnknownResultError) as excinfo:
        operation.run()

    assert "Play Console" in str(excinfo.value)
    assert backend.calls.count("edits.tracks.update") == 1
    sha = google_play.compute_file_sha256(aab)
    checkpoint = load_checkpoint(checkpoint_path(tmp_path, compute_operation_id(make_intent(), sha)))
    assert checkpoint.phase == PHASE_UPLOADED


# -- lost commit, expired edit and crash recovery --------------------------------------


def test_lost_commit_response_with_live_edit_retries_once(tmp_path, backend, aab):
    backend.fail_once("edits.commit", lost_response(STAGE_EDIT_COMMIT))
    operation = make_operation(backend, tmp_path, make_intent(), aab)

    outcome = operation.run()

    assert outcome.version_code == 40
    assert backend.calls.count("edits.commit") == 2
    checkpoint = load_checkpoint(checkpoint_path(tmp_path, outcome.operation_id))
    assert checkpoint.phase == PHASE_CONFIRMED


def test_lost_commit_response_with_gone_edit_verifies_through_separate_edit(tmp_path, backend, aab):
    backend.fail_once("edits.commit", lost_response(STAGE_EDIT_COMMIT), mode="applied")
    operation = make_operation(backend, tmp_path, make_intent(), aab)

    outcome = operation.run()

    assert outcome.version_code == 40
    assert backend.calls.count("edits.commit") == 1
    assert backend.committed_edits == ["edit-1"], "the verification edit is never committed"
    assert backend.calls.count("edits.delete") == 1
    checkpoint = load_checkpoint(checkpoint_path(tmp_path, outcome.operation_id))
    assert checkpoint.phase == PHASE_CONFIRMED


def test_crash_between_remote_success_and_local_record_confirms_without_mutations(tmp_path, backend, aab):
    # Simulate a crash right after Google applied the commit: the edit is gone,
    # the app has the bundle and track release, but the checkpoint is stale.
    intent = make_intent()
    sha = google_play.compute_file_sha256(aab)
    seed_checkpoint(
        tmp_path,
        intent,
        aab,
        phase=PHASE_TRACK_UPDATED,
        edit_id="edit-99",
        edit_expiry_seconds="43200",
        version_code=40,
        source_release=None,
    )
    backend.add_committed_bundle(40, sha)
    backend.set_track("internal", [{"status": "completed", "versionCodes": [40]}])
    operation = make_operation(backend, tmp_path, intent, aab)

    outcome = operation.run()

    assert outcome.version_code == 40
    assert outcome.resumed is False
    # Only verification reads happened: probing the saved edit, then a fresh
    # (deleted, uncommitted) verification edit.
    assert backend.calls == [
        "edits.get",
        "edits.insert",
        "edits.bundles.list",
        "edits.tracks.get",
        "edits.delete",
    ]
    assert backend.committed_edits == []
    checkpoint = load_checkpoint(checkpoint_path(tmp_path, compute_operation_id(intent, sha)))
    assert checkpoint.phase == PHASE_CONFIRMED
    assert checkpoint.result["version_code"] == 40


@pytest.mark.parametrize("matches", [True, False])
def test_recovery_verifies_explicit_release_name_and_notes(tmp_path, backend, aab, matches):
    intent = make_intent(release_name="1.2.3", release_notes={"ru": "Исправления"})
    sha = google_play.compute_file_sha256(aab)
    seed_checkpoint(tmp_path, intent, aab, phase=PHASE_TRACK_UPDATED, edit_id="gone", version_code=40)
    backend.add_committed_bundle(40, sha)
    backend.set_track("internal", [{
        "status": "completed", "versionCodes": [40],
        "name": "1.2.3" if matches else "other",
        "releaseNotes": [{"language": "ru", "text": "Исправления" if matches else "other"}],
    }])
    operation = make_operation(backend, tmp_path, intent, aab)
    if matches:
        assert operation.run().version_code == 40
    else:
        with pytest.raises(UnknownResultError):
            operation.run()
    assert "edits.commit" not in backend.calls
    assert "edits.bundles.upload" not in backend.calls


def test_expired_edit_recovers_with_fresh_edit_after_verification(tmp_path, backend, aab):
    intent = make_intent()
    sha = google_play.compute_file_sha256(aab)
    seed_checkpoint(
        tmp_path,
        intent,
        aab,
        phase=PHASE_UPLOADED,
        edit_id="edit-99",
        edit_expiry_seconds="43200",
        version_code=40,
        source_release=completed_release(11),
    )
    operation = make_operation(backend, tmp_path, intent, aab)

    outcome = operation.run()

    assert outcome.version_code == 40
    # Exactly one new upload, justified by the verified absence of the bundle.
    assert backend.calls.count("edits.bundles.upload") == 1
    assert backend.calls.count("edits.insert") == 2  # recovery verification edit + fresh edit
    checkpoint = load_checkpoint(checkpoint_path(tmp_path, compute_operation_id(intent, sha)))
    assert checkpoint.phase == PHASE_CONFIRMED


def test_verification_rejects_bundle_without_matching_release_state(tmp_path, backend, aab):
    intent = make_intent()
    sha = google_play.compute_file_sha256(aab)
    seed_checkpoint(
        tmp_path,
        intent,
        aab,
        phase=PHASE_TRACK_UPDATED,
        edit_id="edit-99",
        version_code=40,
    )
    backend.add_committed_bundle(40, sha)
    backend.set_track("internal", [{"status": "completed", "versionCodes": [99]}])
    operation = make_operation(backend, tmp_path, intent, aab)

    with pytest.raises(UnknownResultError) as excinfo:
        operation.run()

    message = str(excinfo.value)
    assert "result is unknown" in message
    assert "Google Play Console" in message
    assert "do not re-run the upload or commit blindly" in message
    sha = google_play.compute_file_sha256(aab)
    checkpoint = load_checkpoint(checkpoint_path(tmp_path, compute_operation_id(intent, sha)))
    assert checkpoint.phase != PHASE_CONFIRMED


def test_verification_rejects_version_code_mismatch(tmp_path, backend, aab):
    intent = make_intent()
    sha = google_play.compute_file_sha256(aab)
    seed_checkpoint(
        tmp_path,
        intent,
        aab,
        phase=PHASE_TRACK_UPDATED,
        edit_id="edit-99",
        version_code=40,
    )
    backend.add_committed_bundle(99, sha)  # same content, unexpected version code
    operation = make_operation(backend, tmp_path, intent, aab)

    with pytest.raises(UnknownResultError) as excinfo:
        operation.run()

    assert "version code" in str(excinfo.value)
    assert "edits.commit" not in backend.calls


def test_expired_edit_with_present_bundle_and_matching_track_confirms(tmp_path, backend, aab):
    intent = make_intent(release_status="draft")
    sha = google_play.compute_file_sha256(aab)
    seed_checkpoint(
        tmp_path,
        intent,
        aab,
        phase=PHASE_EDIT_CREATED,
        edit_id="edit-99",
        version_code=40,
        source_release=completed_release(11),
    )
    backend.add_committed_bundle(40, sha)
    backend.set_track(
        "internal",
        [completed_release(11), {"status": "draft", "versionCodes": [40]}],
    )
    operation = make_operation(backend, tmp_path, intent, aab)

    outcome = operation.run()

    assert outcome.resumed is False
    assert outcome.changes_sent_for_review is False
    assert backend.calls == [
        "edits.get",
        "edits.insert",
        "edits.bundles.list",
        "edits.tracks.get",
        "edits.delete",
    ]


def test_staged_rollout_recovery_requires_matching_fraction(tmp_path, backend, aab):
    intent = make_intent(release_status="inProgress", user_fraction=0.25)
    sha = google_play.compute_file_sha256(aab)
    seed_checkpoint(
        tmp_path,
        intent,
        aab,
        phase=PHASE_TRACK_UPDATED,
        edit_id="edit-99",
        version_code=40,
    )
    backend.add_committed_bundle(40, sha)
    backend.set_track(
        "internal",
        [{"status": "inProgress", "versionCodes": [40], "userFraction": 0.5}],
    )
    operation = make_operation(backend, tmp_path, intent, aab)

    with pytest.raises(UnknownResultError):
        operation.run()


# -- definite Google rejections ------------------------------------------------------


def test_commit_rejection_while_under_review_is_propagated_without_retry(tmp_path, backend, aab):
    backend.fail_once(
        "edits.commit",
        GooglePlayError(STAGE_EDIT_COMMIT, "app is currently under review", http_status=409),
    )
    operation = make_operation(backend, tmp_path, make_intent(), aab)

    with pytest.raises(GooglePlayError) as excinfo:
        operation.run()

    assert excinfo.value.http_status == 409
    assert backend.calls.count("edits.commit") == 1
    assert backend.calls.count("edits.insert") == 1, "no verification edit for definite rejections"
    sha = google_play.compute_file_sha256(aab)
    checkpoint = load_checkpoint(checkpoint_path(tmp_path, compute_operation_id(make_intent(), sha)))
    assert checkpoint.phase == PHASE_TRACK_UPDATED


def test_retry_after_review_completes_commits_again_without_reupload(tmp_path, backend, aab):
    backend.fail_once(
        "edits.commit",
        GooglePlayError(STAGE_EDIT_COMMIT, "app is currently under review", http_status=409),
    )
    intent = make_intent()
    with pytest.raises(GooglePlayError):
        make_operation(backend, tmp_path, intent, aab).run()

    # The review finished; the same operation is retried after the rejection.
    outcome = make_operation(backend, tmp_path, intent, aab).run()

    assert outcome.version_code == 40
    # The edit that Google kept open is committed once more, unchanged: the same
    # upload and the same track payload, never a re-upload and never a flag
    # switch like changesNotSentForReview=true.
    assert backend.calls.count("edits.commit") == 2
    assert backend.calls.count("edits.bundles.upload") == 1
    assert backend.calls.count("edits.insert") == 1
    sha = google_play.compute_file_sha256(aab)
    checkpoint = load_checkpoint(checkpoint_path(tmp_path, compute_operation_id(intent, sha)))
    assert checkpoint.phase == PHASE_CONFIRMED
    assert backend.tracks["internal"] == [{"status": "completed", "versionCodes": [40]}]


# -- track conflicts ------------------------------------------------------------------


@pytest.mark.parametrize(
    "releases",
    [
        pytest.param([{"status": "draft", "versionCodes": [5]}], id="draft"),
        pytest.param([{"status": "inProgress", "versionCodes": [5], "userFraction": 0.5}], id="inProgress"),
        pytest.param([{"status": "halted", "versionCodes": [5]}], id="halted"),
        pytest.param([{"status": "future", "versionCodes": [5]}], id="unknown-status"),
        pytest.param(
            [completed_release(5), completed_release(6)], id="two-releases"
        ),
        pytest.param([{"status": "completed", "versionCodes": [5], "userFraction": 0.5}], id="stale-fraction"),
        pytest.param([{"status": "completed"}], id="completed-without-version-codes"),
    ],
)
def test_track_conflicts_stop_before_any_upload(tmp_path, backend, aab, releases):
    backend.set_track("internal", releases)
    operation = make_operation(backend, tmp_path, make_intent(), aab)

    with pytest.raises(TrackConflictError) as excinfo:
        operation.run()

    assert "Google Play Console" in str(excinfo.value) or "version codes" in str(excinfo.value)
    assert "edits.bundles.upload" not in backend.calls
    assert "edits.tracks.update" not in backend.calls
    assert "edits.commit" not in backend.calls


# -- checkpoint corruption and conflicting operations ----------------------------------


def test_corrupted_checkpoint_stops_publication_before_any_api_call(tmp_path, backend, aab):
    intent = make_intent()
    sha = google_play.compute_file_sha256(aab)
    path = checkpoint_path(tmp_path, compute_operation_id(intent, sha))
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")

    with pytest.raises(CheckpointError):
        make_operation(backend, tmp_path, intent, aab).run()

    assert backend.calls == []


def test_unfinished_operation_with_changed_parameters_blocks_publication(tmp_path, backend, aab):
    seed_checkpoint(
        tmp_path,
        make_intent(),
        aab,
        phase=PHASE_TRACK_UPDATED,
        edit_id="edit-1",
        version_code=40,
    )
    changed = make_intent(track="beta")
    operation = make_operation(backend, tmp_path, changed, aab)

    with pytest.raises(ConflictingOperationError) as excinfo:
        operation.run()

    assert "blocks a publication with changed parameters" in str(excinfo.value)
    assert backend.calls == []


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"track": "beta"}, id="track"),
        pytest.param({"release_status": "draft"}, id="release-status"),
        pytest.param({"release_notes": {"en-US": "Different notes"}}, id="release-notes"),
        pytest.param({"release_name": "Release 2.0"}, id="release-name"),
        pytest.param({"release_status": "inProgress", "user_fraction": 0.25}, id="staged-fraction"),
    ],
)
def test_changed_release_parameters_between_attempts_are_blocked(
    tmp_path, backend, aab, change
):
    seed_checkpoint(
        tmp_path,
        make_intent(),
        aab,
        phase=PHASE_TRACK_UPDATED,
        edit_id="edit-1",
        version_code=40,
    )
    operation = make_operation(backend, tmp_path, make_intent(**change), aab)

    with pytest.raises(ConflictingOperationError):
        operation.run()

    assert backend.calls == [], "a blocked attempt must not touch the publishing API"


def test_changed_aab_content_between_attempts_maps_to_new_blocked_operation(tmp_path, backend, aab):
    seed_checkpoint(
        tmp_path,
        make_intent(),
        aab,
        phase=PHASE_EDIT_CREATED,
        edit_id="edit-1",
        version_code=40,
    )
    other_aab = aab.with_name("app-release-v2.aab")
    other_aab.write_bytes(AAB_BYTES + b"-v2")
    base_sha = google_play.compute_file_sha256(aab)
    other_sha = google_play.compute_file_sha256(other_aab)
    assert compute_operation_id(make_intent(), other_sha) != compute_operation_id(make_intent(), base_sha)

    operation = make_operation(backend, tmp_path, make_intent(), other_aab)

    with pytest.raises(ConflictingOperationError):
        operation.run()

    assert backend.calls == []


def test_changed_package_between_attempts_publishes_as_separate_operation(tmp_path, backend, aab):
    seed_checkpoint(
        tmp_path,
        make_intent(),
        aab,
        phase=PHASE_TRACK_UPDATED,
        edit_id="edit-1",
        version_code=40,
    )
    operation = make_operation(backend, tmp_path, make_intent(package_name=OTHER_PACKAGE), aab)

    outcome = operation.run()

    assert outcome.package_name == OTHER_PACKAGE
    assert outcome.operation_id != compute_operation_id(make_intent(), google_play.compute_file_sha256(aab))
    assert outcome.version_code == 40


def test_confirmed_operation_with_other_parameters_does_not_block(tmp_path, backend, aab):
    seed_checkpoint(tmp_path, make_intent(track="beta"), aab, phase=PHASE_CONFIRMED, result={
        "version_code": 7,
        "aab_sha256": "b" * 64,
        "track": "beta",
        "release_status": "completed",
        "changes_sent_for_review": True,
    })
    operation = make_operation(backend, tmp_path, make_intent(), aab)

    outcome = operation.run()

    assert outcome.version_code == 40


def test_unfinished_operation_of_other_package_does_not_block(tmp_path, backend, aab):
    seed_checkpoint(
        tmp_path,
        make_intent(package_name=OTHER_PACKAGE),
        aab,
        phase=PHASE_TRACK_UPDATED,
        edit_id="edit-1",
        version_code=40,
    )
    operation = make_operation(backend, tmp_path, make_intent(), aab)

    outcome = operation.run()

    assert outcome.version_code == 40


def test_checkpoint_write_failure_prevents_external_changes(tmp_path, backend, aab, monkeypatch):
    def broken_save(path: Path, checkpoint: PublishCheckpoint) -> None:
        raise CheckpointWriteError("disk full")

    monkeypatch.setattr(google_play_state, "save_checkpoint", broken_save)

    with pytest.raises(CheckpointWriteError):
        make_operation(backend, tmp_path, make_intent(), aab).run()

    assert backend.calls == []


# -- already-confirmed operations ------------------------------------------------------


def test_confirmed_checkpoint_returns_saved_result_without_mutations(tmp_path, backend, aab):
    intent = make_intent()
    sha = google_play.compute_file_sha256(aab)
    seed_checkpoint(
        tmp_path,
        intent,
        aab,
        phase=PHASE_CONFIRMED,
        version_code=40,
        result={
            "version_code": 40,
            "aab_sha256": sha,
            "track": "internal",
            "release_status": "completed",
            "changes_sent_for_review": True,
        },
    )
    operation = make_operation(backend, tmp_path, intent, aab)

    outcome = operation.run()

    assert outcome == google_play_state.PublishOutcome(
        operation_id=compute_operation_id(intent, sha),
        package_name=PACKAGE,
        track="internal",
        release_status="completed",
        version_code=40,
        aab_sha256=sha,
        changes_sent_for_review=True,
        resumed=True,
    )
    assert backend.calls == []


# -- checkout-local locking ------------------------------------------------------------


def test_busy_package_lock_stops_operation_without_external_calls(tmp_path, backend, aab):
    locks_dir = tmp_path / ".cdt" / "google-play" / "locks"
    locks_dir.mkdir(parents=True)
    held = filelock.FileLock(
        google_play_state.lock_path(tmp_path, PACKAGE), timeout=0, thread_local=False
    )
    held.acquire()
    observed: dict[str, Exception] = {}
    release_guard = threading.Event()

    def run_operation() -> None:
        try:
            make_operation(backend, tmp_path, make_intent(), aab).run()
        except OperationLockedError as exc:
            observed["error"] = exc
        finally:
            release_guard.set()

    worker = threading.Thread(target=run_operation)
    worker.start()
    worker.join(timeout=10)
    held.release()

    assert release_guard.is_set()
    assert isinstance(observed.get("error"), OperationLockedError)
    assert PACKAGE in str(observed["error"])
    assert backend.calls == []


def test_different_packages_do_not_block_each_other(tmp_path, backend, aab):
    locks_dir = tmp_path / ".cdt" / "google-play" / "locks"
    locks_dir.mkdir(parents=True)
    held = filelock.FileLock(
        google_play_state.lock_path(tmp_path, OTHER_PACKAGE), timeout=0, thread_local=False
    )
    held.acquire()
    try:
        outcome = make_operation(backend, tmp_path, make_intent(), aab).run()
        assert outcome.version_code == 40
    finally:
        held.release()


def test_package_lock_is_released_after_run(tmp_path, backend, aab):
    make_operation(backend, tmp_path, make_intent(), aab).run()

    lock = filelock.FileLock(
        google_play_state.lock_path(tmp_path, PACKAGE), timeout=0, thread_local=False
    )
    lock.acquire()
    lock.release()


def test_concurrent_publications_of_same_package_allow_exactly_one(tmp_path, backend, aab):
    started = {"first": threading.Event(), "second": threading.Event()}

    def before_call(label: str) -> None:
        # The lock holder pauses inside its first API call, so the other worker
        # necessarily collides with a held lock instead of racing the release.
        started["first"].wait(timeout=10)
        started["second"].wait(timeout=10)

    backend.before_call = before_call
    results: dict[str, Any] = {}

    def worker(name: str) -> None:
        started[name].set()
        try:
            results[name] = make_operation(backend, tmp_path, make_intent(), aab).run()
        except GooglePlayStateError as exc:
            results[name] = exc

    workers = [threading.Thread(target=worker, args=(name,)) for name in ("first", "second")]
    for thread in workers:
        thread.start()
    for thread in workers:
        thread.join(timeout=30)

    outcomes = list(results.values())
    succeeded = [outcome for outcome in outcomes if isinstance(outcome, google_play_state.PublishOutcome)]
    refused = [outcome for outcome in outcomes if isinstance(outcome, OperationLockedError)]
    assert len(succeeded) == 1
    assert len(refused) == 1
    assert succeeded[0].version_code == 40
    assert PACKAGE in str(refused[0])
    # The refused worker never reached the publishing API; the call log is the
    # winner's ordinary happy path.
    assert backend.calls == [
        "edits.insert",
        "edits.tracks.get",
        "edits.bundles.list",
        "edits.bundles.upload",
        "edits.tracks.update",
        "edits.commit",
    ]
    checkpoints = list((tmp_path / ".cdt" / "google-play" / "operations").glob("*.json"))
    assert len(checkpoints) == 1
    assert json.loads(checkpoints[0].read_text(encoding="utf-8"))["phase"] == PHASE_CONFIRMED


# -- checkpoint hygiene -----------------------------------------------------------------


def test_checkpoint_never_contains_secrets_or_urls(tmp_path, backend, aab):
    intent = make_intent()
    operation = make_operation(backend, tmp_path, intent, aab)
    operation.run()

    path = checkpoint_path(tmp_path, outcome_operation_id(tmp_path, intent, aab))
    raw = path.read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert set(payload) == {
        "schema_version",
        "operation_id",
        "package_name",
        "track",
        "release_status",
        "release_notes",
        "release_name",
        "user_fraction",
        "aab_sha256",
        "phase",
        "edit_id",
        "edit_expiry_seconds",
        "version_code",
        "source_release",
        "result",
        "created_at",
        "updated_at",
    }
    assert "http" not in raw
    assert "token" not in raw


def test_unreadable_aab_stops_before_any_external_call(tmp_path, backend):
    missing = tmp_path / "missing.aab"

    with pytest.raises(GooglePlayStateError) as excinfo:
        make_operation(backend, tmp_path, make_intent(), missing).run()

    assert "cannot read AAB" in str(excinfo.value)
    assert backend.calls == []


def outcome_operation_id(tmp_path: Path, intent: PublishIntent, aab: Path) -> str:
    return compute_operation_id(intent, google_play.compute_file_sha256(aab))
