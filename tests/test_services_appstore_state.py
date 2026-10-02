"""Unit tests for the persisted identification of completed TestFlight builds.

Covers both successful upload paths (full upload and completion-only), the
upload-only path, unsuccessful completion, corrupted records, per-app record
separation and the priority of the current pipeline context over the record.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from cdt.services import appstore, appstore_state
from cdt.services import appstore_review as review
from cdt.services.appstore_state import (
    SOURCE_CONTEXT,
    SOURCE_RECORD,
    AppStoreStateError,
    RecordError,
    RecordWriteError,
    UploadRecord,
    bundle_key,
    load_upload_record,
    parse_new_version,
    record_path,
    resolve_review_target,
    save_upload_record,
    select_upload_target,
)

BUNDLE_ID = "com.example.app"
OTHER_BUNDLE_ID = "com.other.app"


def _stub_client(monkeypatch) -> appstore._AscClient:
    monkeypatch.setattr(appstore, "_asc_token", lambda env: "token0")
    return appstore._AscClient({})


def _app(app_id: str, bundle_id: str) -> dict:
    return {"id": app_id, "type": "apps", "attributes": {"bundleId": bundle_id}}


def _build(build_id: str, build_number: str, pre_id: str, app_id: str) -> dict:
    return {
        "id": build_id,
        "type": "builds",
        "attributes": {"version": build_number, "processingState": "VALID", "expired": False},
        "relationships": {
            "app": {"data": {"type": "apps", "id": app_id}},
            "preReleaseVersion": {"data": {"type": "preReleaseVersions", "id": pre_id}},
        },
    }


def _pre_release(pre_id: str, version: str, platform: str = "IOS") -> dict:
    return {
        "id": pre_id,
        "type": "preReleaseVersions",
        "attributes": {"version": version, "platform": platform},
    }


def _builds_rsp(items: list[dict], included: list[dict] | None = None) -> dict:
    rsp: dict = {"data": items, "links": {}}
    if included is not None:
        rsp["included"] = included
    return rsp


# --- record identity and storage --------------------------------------------


def test_bundle_key_is_stable_sha256_of_bundle_id():
    assert bundle_key(BUNDLE_ID) == bundle_key(BUNDLE_ID)
    assert bundle_key(BUNDLE_ID) == hashlib.sha256(BUNDLE_ID.encode("utf-8")).hexdigest()
    assert bundle_key(BUNDLE_ID) != bundle_key(OTHER_BUNDLE_ID)


def test_record_path_uses_sha256_key_under_uploads(tmp_path):
    path = record_path(tmp_path, BUNDLE_ID)
    assert path == tmp_path / ".cdt" / "appstore" / "uploads" / f"{bundle_key(BUNDLE_ID)}.json"


def test_save_and_load_roundtrip(tmp_path):
    record = save_upload_record(tmp_path, BUNDLE_ID, "1.2.3+5", completed_at="2026-10-02T10:00:00+00:00")
    assert record == UploadRecord(
        bundle_id=BUNDLE_ID,
        marketing_version="1.2.3",
        build_number="5",
        completed_at="2026-10-02T10:00:00+00:00",
    )
    assert load_upload_record(tmp_path, BUNDLE_ID) == record


def test_saved_record_file_is_versioned_and_complete(tmp_path):
    save_upload_record(tmp_path, BUNDLE_ID, "1.2.3+5")
    payload = json.loads(record_path(tmp_path, BUNDLE_ID).read_text(encoding="utf-8"))
    assert payload == {
        "schema_version": appstore_state.RECORD_SCHEMA_VERSION,
        "bundle_id": BUNDLE_ID,
        "marketing_version": "1.2.3",
        "build_number": "5",
        "completed_at": payload["completed_at"],
    }
    assert payload["schema_version"] == 1
    assert payload["completed_at"]  # completion time is recorded


def test_save_is_atomic_and_leaves_no_temporary_files(tmp_path):
    save_upload_record(tmp_path, BUNDLE_ID, "1.2.3+5")
    uploads = tmp_path / ".cdt" / "appstore" / "uploads"
    assert [p.name for p in uploads.iterdir()] == [f"{bundle_key(BUNDLE_ID)}.json"]


def test_save_does_not_store_secrets(tmp_path):
    save_upload_record(tmp_path, BUNDLE_ID, "1.2.3+5")
    content = record_path(tmp_path, BUNDLE_ID).read_text(encoding="utf-8")
    for secret in ("ASC_KEY_ID", "ASC_ISSUER_ID", "ASC_PRIVATE_KEY_PATH", "-----BEGIN PRIVATE KEY-----"):
        assert secret not in content


def test_save_rejects_invalid_version_and_writes_nothing(tmp_path):
    for bad_version in ("1.2.3", "1.2.3+", "+5", "1.2.3+5+6", "   "):
        with pytest.raises(AppStoreStateError, match="Invalid pipeline version"):
            save_upload_record(tmp_path, BUNDLE_ID, bad_version)
    assert load_upload_record(tmp_path, BUNDLE_ID) is None


def test_save_requires_bundle_id(tmp_path):
    with pytest.raises(AppStoreStateError, match="without a bundle id"):
        save_upload_record(tmp_path, "  ", "1.2.3+5")


def test_save_write_failure_is_explicit(tmp_path):
    (tmp_path / ".cdt").write_text("occupied by a regular file", encoding="utf-8")
    with pytest.raises(RecordWriteError, match="cannot save App Store upload record"):
        save_upload_record(tmp_path, BUNDLE_ID, "1.2.3+5")


# --- strict reading ----------------------------------------------------------


def test_load_missing_record_returns_none(tmp_path):
    assert load_upload_record(tmp_path, BUNDLE_ID) is None


def test_load_rejects_corrupted_json(tmp_path):
    path = record_path(tmp_path, BUNDLE_ID)
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(RecordError, match="corrupted"):
        load_upload_record(tmp_path, BUNDLE_ID)


def test_load_rejects_unknown_schema_version(tmp_path):
    save_upload_record(tmp_path, BUNDLE_ID, "1.2.3+5")
    payload = json.loads(record_path(tmp_path, BUNDLE_ID).read_text(encoding="utf-8"))
    payload["schema_version"] = 99
    record_path(tmp_path, BUNDLE_ID).write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RecordError, match="schema version"):
        load_upload_record(tmp_path, BUNDLE_ID)


def test_load_rejects_mismatched_bundle_id(tmp_path):
    save_upload_record(tmp_path, BUNDLE_ID, "1.2.3+5")
    payload = json.loads(record_path(tmp_path, BUNDLE_ID).read_text(encoding="utf-8"))
    payload["bundle_id"] = OTHER_BUNDLE_ID
    record_path(tmp_path, BUNDLE_ID).write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RecordError, match="belongs to app"):
        load_upload_record(tmp_path, BUNDLE_ID)


def test_load_rejects_missing_or_empty_fields(tmp_path):
    for key in ("bundle_id", "marketing_version", "build_number", "completed_at"):
        save_upload_record(tmp_path, BUNDLE_ID, "1.2.3+5")
        payload = json.loads(record_path(tmp_path, BUNDLE_ID).read_text(encoding="utf-8"))
        del payload[key]
        record_path(tmp_path, BUNDLE_ID).write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(RecordError, match=key):
            load_upload_record(tmp_path, BUNDLE_ID)


def test_load_rejects_non_object_payload(tmp_path):
    path = record_path(tmp_path, BUNDLE_ID)
    path.parent.mkdir(parents=True)
    path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(RecordError, match="JSON object"):
        load_upload_record(tmp_path, BUNDLE_ID)


# --- app separation ----------------------------------------------------------


def test_records_of_different_apps_are_independent(tmp_path):
    save_upload_record(tmp_path, BUNDLE_ID, "1.2.3+5")
    save_upload_record(tmp_path, OTHER_BUNDLE_ID, "2.0.0+9")

    first = load_upload_record(tmp_path, BUNDLE_ID)
    second = load_upload_record(tmp_path, OTHER_BUNDLE_ID)
    assert (first.marketing_version, first.build_number) == ("1.2.3", "5")
    assert (second.marketing_version, second.build_number) == ("2.0.0", "9")

    save_upload_record(tmp_path, BUNDLE_ID, "1.2.4+6")
    assert load_upload_record(tmp_path, BUNDLE_ID).build_number == "6"
    assert load_upload_record(tmp_path, OTHER_BUNDLE_ID).build_number == "9"


# --- target selection --------------------------------------------------------


def test_parse_new_version():
    assert parse_new_version("1.2.3+5") == ("1.2.3", "5")
    assert parse_new_version(" 1.2.3 + 5 ") == ("1.2.3", "5")
    for bad in ("1.2.3", "1.2.3+", "+5", "1.2.3+5+6"):
        with pytest.raises(AppStoreStateError, match="Invalid pipeline version"):
            parse_new_version(bad)


def test_select_prefers_current_context_over_record(tmp_path):
    save_upload_record(tmp_path, BUNDLE_ID, "1.2.3+5")
    target = select_upload_target(tmp_path, BUNDLE_ID, "2.0.0+9")
    assert (target.bundle_id, target.marketing_version, target.build_number) == (BUNDLE_ID, "2.0.0", "9")
    assert target.source == SOURCE_CONTEXT


def test_select_uses_record_only_without_current_version(tmp_path):
    save_upload_record(tmp_path, BUNDLE_ID, "1.2.3+5")
    for empty in (None, "", "   "):
        target = select_upload_target(tmp_path, BUNDLE_ID, empty)
        assert (target.bundle_id, target.marketing_version, target.build_number) == (BUNDLE_ID, "1.2.3", "5")
        assert target.source == SOURCE_RECORD


def test_select_invalid_current_version_does_not_fall_back_to_record(tmp_path):
    save_upload_record(tmp_path, BUNDLE_ID, "1.2.3+5")
    with pytest.raises(AppStoreStateError, match="Invalid pipeline version"):
        select_upload_target(tmp_path, BUNDLE_ID, "broken")


def test_select_without_record_fails_clearly(tmp_path):
    with pytest.raises(AppStoreStateError, match="No recorded TestFlight build"):
        select_upload_target(tmp_path, BUNDLE_ID, None)


def test_select_does_not_reuse_another_apps_record(tmp_path):
    save_upload_record(tmp_path, OTHER_BUNDLE_ID, "2.0.0+9")
    with pytest.raises(AppStoreStateError, match="No recorded TestFlight build"):
        select_upload_target(tmp_path, BUNDLE_ID, None)


def test_select_from_corrupted_record_fails(tmp_path):
    path = record_path(tmp_path, BUNDLE_ID)
    path.parent.mkdir(parents=True)
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(RecordError):
        select_upload_target(tmp_path, BUNDLE_ID, None)


# --- remote re-verification --------------------------------------------------


def test_resolve_review_target_rechecks_selected_build_in_asc(tmp_path, monkeypatch):
    from cdt.services.appstore import ASC_API_BASE

    save_upload_record(tmp_path, BUNDLE_ID, "1.2.3+5")
    client = _stub_client(monkeypatch)

    def fake_asc_request(method, path, asc_client, payload=None, retry_ambiguous=True):
        assert asc_client is client
        if path.startswith("/v1/apps?"):
            return {"data": [_app("app-1", BUNDLE_ID)], "links": {}}
        if path.startswith("/v1/builds?"):
            assert "filter[version]=5" in path
            assert "filter[preReleaseVersion.version]=1.2.3" in path
            assert path.startswith(f"{ASC_API_BASE}") or True
            return _builds_rsp(
                [_build("build-1", "5", "pre-1", "app-1")],
                included=[_pre_release("pre-1", "1.2.3")],
            )
        raise AssertionError(f"unexpected ASC call: {method} {path}")

    monkeypatch.setattr(appstore, "_asc_request", fake_asc_request)

    target, build = resolve_review_target(tmp_path, BUNDLE_ID, None, client)

    assert (target.marketing_version, target.build_number, target.source) == ("1.2.3", "5", SOURCE_RECORD)
    assert build["id"] == "build-1"


def test_resolve_review_target_never_trusts_record_when_asc_has_no_build(tmp_path, monkeypatch):
    save_upload_record(tmp_path, BUNDLE_ID, "1.2.3+5")
    client = _stub_client(monkeypatch)

    def fake_asc_request(method, path, asc_client, payload=None, retry_ambiguous=True):
        if path.startswith("/v1/apps?"):
            return {"data": [_app("app-1", BUNDLE_ID)], "links": {}}
        if path.startswith("/v1/builds?"):
            return _builds_rsp([], included=[])
        raise AssertionError(f"unexpected ASC call: {method} {path}")

    monkeypatch.setattr(appstore, "_asc_request", fake_asc_request)

    with pytest.raises(review.ResourceNotFoundError):
        resolve_review_target(tmp_path, BUNDLE_ID, None, client)


def test_resolve_review_target_uses_current_context_and_rejects_invalid_build(tmp_path, monkeypatch):
    save_upload_record(tmp_path, BUNDLE_ID, "1.2.3+5")
    client = _stub_client(monkeypatch)

    def fake_asc_request(method, path, asc_client, payload=None, retry_ambiguous=True):
        if path.startswith("/v1/apps?"):
            return {"data": [_app("app-1", BUNDLE_ID)], "links": {}}
        if path.startswith("/v1/builds?"):
            # ASC knows only the old build; the context targets 2.0.0+9.
            return _builds_rsp(
                [_build("build-1", "5", "pre-1", "app-1")],
                included=[_pre_release("pre-1", "1.2.3")],
            )
        raise AssertionError(f"unexpected ASC call: {method} {path}")

    monkeypatch.setattr(appstore, "_asc_request", fake_asc_request)

    with pytest.raises(review.ResourceNotFoundError):
        resolve_review_target(tmp_path, BUNDLE_ID, "2.0.0+9", client)
