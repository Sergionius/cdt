"""Unit tests for the persisted identification of completed TestFlight builds.

Covers both successful upload paths (full upload and completion-only), the
upload-only path, unsuccessful completion, corrupted records, per-app record
separation and the priority of the current pipeline context over the record.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import filelock
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


# --- resumable App Review submission operation -----------------------------------


class RunInterrupted(Exception):
    """Raised by FakeAsc to simulate a crash right after a change was applied."""


def _ambiguous(method: str, path: str, code: int | None = 502) -> appstore.AscAmbiguousResultError:
    category = f"http_{code}" if code is not None else "timeout"
    detail = "lost response" if code is not None else "connection reset"
    return appstore.AscAmbiguousResultError(
        "ambiguous", method=method, path=path, code=code, category=category, detail=detail
    )


def _http_404(method: str, path: str) -> appstore.AscHttpError:
    return appstore.AscHttpError("not found", code=404, method=method, path=path, body="not found")


class FakeAsc:
    """Minimal in-memory App Store Connect double with fault injection.

    Remote state (versions, localizations, phased releases, submissions and
    items) persists across operations on the same instance, so a second run
    resumes against the remote state left by the first one — exactly like a
    resume against Apple.
    """

    def __init__(self, monkeypatch, *, app_id: str = "app-1", bundle_id: str = BUNDLE_ID):
        monkeypatch.setattr(appstore, "_asc_request", self)
        self.app_id = app_id
        self.bundle_id = bundle_id
        self.build = {
            "id": "build-5",
            "type": "builds",
            "attributes": {"version": "5", "processingState": "VALID", "expired": False},
            "relationships": {
                "app": {"data": {"type": "apps", "id": app_id}},
                "preReleaseVersion": {"data": {"type": "preReleaseVersions", "id": "pre-1"}},
            },
        }
        self.pre = {"id": "pre-1", "type": "preReleaseVersions", "attributes": {"version": "1.2.3", "platform": "IOS"}}
        self.versions: dict[str, dict] = {}
        self.localizations: dict[str, list[dict]] = {}
        self.phased: dict[str, dict | None] = {}
        self.submissions: dict[str, dict] = {}
        self.items: dict[str, list[dict]] = {}
        self.calls: list[dict] = []
        self.faults: list[dict] = []
        self.stop_after_mutations: int | None = None
        self._mutation_count = 0
        self._next_id = 1

    # -- configuration

    def fail_on(
        self,
        method: str,
        path: str,
        exc: Exception,
        *,
        apply: bool = True,
        attributes: dict | None = None,
        relationships: bool = False,
    ) -> None:
        """Make the next matching call raise *exc* (after applying, or not)."""
        self.faults.append(
            {
                "method": method,
                "path": path,
                "exc": exc,
                "apply": apply,
                "attributes": attributes,
                "relationships": relationships,
            }
        )

    def add_version(
        self,
        version_id: str = "v-1",
        *,
        state: str | None = "PREPARE_FOR_SUBMISSION",
        version_string: str = "1.2.3",
        build_id: str | None = None,
        release_type: str | None = None,
        localizations: tuple[str, ...] = ("ru", "en-US"),
    ) -> dict:
        attributes: dict = {"versionString": version_string, "platform": "IOS"}
        if state is not None:
            attributes["appStoreState"] = state
        if release_type is not None:
            attributes["releaseType"] = release_type
        version = {
            "id": version_id,
            "type": "appStoreVersions",
            "attributes": attributes,
            "relationships": {
                "app": {"data": {"type": "apps", "id": self.app_id}},
                "build": {"data": {"type": "builds", "id": build_id} if build_id else None},
            },
        }
        self.versions[version_id] = version
        self.localizations[version_id] = [
            {
                "id": f"loc-{version_id}-{locale}",
                "type": "appStoreVersionLocalizations",
                "attributes": {"locale": locale, "whatsNew": None},
            }
            for locale in localizations
        ]
        self.phased[version_id] = None
        return version

    def add_submission(
        self,
        submission_id: str,
        *,
        state: str = "DRAFT",
        version_id: str | None = None,
        item_version_ids: tuple[str, ...] = (),
    ) -> dict:
        submission = {
            "id": submission_id,
            "type": "reviewSubmissions",
            "attributes": {"state": state, "platform": "IOS"},
            "relationships": {
                "app": {"data": {"type": "apps", "id": self.app_id}},
                "appStoreVersionForReview": {
                    "data": {"type": "appStoreVersions", "id": version_id} if version_id else None
                },
            },
        }
        self.submissions[submission_id] = submission
        self.items[submission_id] = [
            {
                "id": f"item-{submission_id}-{target}",
                "type": "reviewSubmissionItems",
                "relationships": {
                    "reviewSubmission": {"data": {"type": "reviewSubmissions", "id": submission_id}},
                    "appStoreVersion": {"data": {"type": "appStoreVersions", "id": target}},
                },
            }
            for target in item_version_ids
        ]
        return submission

    # -- request handling

    def __call__(self, method: str, path: str, client, payload=None, retry_ambiguous=True):
        base = path.split("?")[0]
        if base == "/v1/builds":
            assert "include=preReleaseVersion,app" in path
        if base.endswith("/items"):
            assert "include=appStoreVersion" in path
        self.calls.append(
            {
                "method": method,
                "path": base,
                "payload": payload,
                "retry_ambiguous": retry_ambiguous,
            }
        )
        fault = next(
            (
                f
                for f in self.faults
                if f["method"] == method and base.startswith(f["path"]) and self._fault_matches(f, payload)
            ),
            None,
        )
        if fault is not None:
            self.faults.remove(fault)
            if not fault["apply"]:
                raise fault["exc"]
        response = self._route(method, base, payload)
        if fault is not None:
            raise fault["exc"]
        if method != "GET":
            self._mutation_count += 1
        if self.stop_after_mutations is not None and self._mutation_count >= self.stop_after_mutations:
            self.stop_after_mutations = None
            raise RunInterrupted(f"stopped after {self._mutation_count} mutations")
        return response

    @staticmethod
    def _fault_matches(fault: dict, payload: dict | None) -> bool:
        data = (payload or {}).get("data") or {}
        if fault["attributes"] is not None:
            attrs = data.get("attributes") or {}
            if not all(attrs.get(key) == value for key, value in fault["attributes"].items()):
                return False
        if fault["relationships"] and "relationships" not in data:
            return False
        return True

    def _route(self, method: str, base: str, payload: dict | None) -> dict:
        if base == "/v1/apps" and method == "GET":
            return {
                "data": [{"id": self.app_id, "type": "apps", "attributes": {"bundleId": self.bundle_id}}],
                "links": {},
            }
        if base == "/v1/builds" and method == "GET":
            return {"data": [self.build], "included": [self.pre], "links": {}}
        if re.fullmatch(r"/v1/apps/([^/]+)/appStoreVersions", base) and method == "GET":
            return {"data": list(self.versions.values()), "links": {}}
        if base == "/v1/appStoreVersions" and method == "POST":
            attrs = ((payload or {}).get("data") or {}).get("attributes") or {}
            version_id = f"v-new-{self._next_id}"
            self._next_id += 1
            version = {
                "id": version_id,
                "type": "appStoreVersions",
                "attributes": {
                    "versionString": attrs.get("versionString"),
                    "platform": attrs.get("platform"),
                    "appStoreState": "PREPARE_FOR_SUBMISSION",
                },
                "relationships": {
                    "app": {"data": {"type": "apps", "id": self.app_id}},
                    "build": {"data": None},
                },
            }
            self.versions[version_id] = version
            self.localizations[version_id] = [
                {
                    "id": f"loc-{version_id}-{locale}",
                    "type": "appStoreVersionLocalizations",
                    "attributes": {"locale": locale, "whatsNew": None},
                }
                for locale in ("ru", "en-US")
            ]
            self.phased[version_id] = None
            return {"data": version}
        m = re.fullmatch(r"/v1/appStoreVersions/([^/]+)", base)
        if m:
            version = self.versions.get(m.group(1))
            if version is None:
                raise _http_404(method, base)
            if method == "GET":
                return {"data": version}
            if method == "PATCH":
                data = (payload or {}).get("data") or {}
                version["attributes"].update(data.get("attributes") or {})
                relationship = (data.get("relationships") or {}).get("build")
                if relationship is not None:
                    version["relationships"]["build"] = relationship
                return {"data": version}
        m = re.fullmatch(r"/v1/appStoreVersions/([^/]+)/build", base)
        if m and method == "GET":
            version = self.versions.get(m.group(1))
            if version is None:
                raise _http_404(method, base)
            return {"data": version["relationships"]["build"]["data"]}
        m = re.fullmatch(r"/v1/appStoreVersions/([^/]+)/appStoreVersionLocalizations", base)
        if m and method == "GET":
            return {"data": self.localizations.get(m.group(1), []), "links": {}}
        m = re.fullmatch(r"/v1/appStoreVersionLocalizations/([^/]+)", base)
        if m and method == "PATCH":
            for localizations in self.localizations.values():
                for loc in localizations:
                    if loc["id"] == m.group(1):
                        loc["attributes"].update(((payload or {}).get("data") or {}).get("attributes") or {})
                        return {"data": loc}
            raise _http_404(method, base)
        m = re.fullmatch(r"/v1/appStoreVersions/([^/]+)/appStoreVersionPhasedRelease", base)
        if m and method == "GET":
            if m.group(1) not in self.versions:
                raise _http_404(method, base)
            return {"data": self.phased.get(m.group(1))}
        if base == "/v1/appStoreVersionPhasedReleases" and method == "POST":
            data = payload["data"]
            assert set(data) == {"type", "attributes", "relationships"}
            assert data["type"] == "appStoreVersionPhasedReleases"
            assert set(data["relationships"]) == {"appStoreVersion"}
            version_id = data["relationships"]["appStoreVersion"]["data"]["id"]
            assert version_id in self.versions
            phased = {
                "id": f"ph-{self._next_id}",
                "type": "appStoreVersionPhasedReleases",
                "attributes": {"phasedReleaseState": "INACTIVE"},
            }
            self._next_id += 1
            self.phased[version_id] = phased
            return {"data": phased}
        m = re.fullmatch(r"/v1/appStoreVersionPhasedReleases/([^/]+)", base)
        if m and method == "DELETE":
            for version_id, phased in self.phased.items():
                if phased and phased["id"] == m.group(1):
                    self.phased[version_id] = None
                    return {}
            raise _http_404(method, base)
        if base == "/v1/reviewSubmissions" and method == "GET":
            return {"data": list(self.submissions.values()), "links": {}}
        if base == "/v1/reviewSubmissions" and method == "POST":
            data = (payload or {}).get("data") or {}
            assert set(data) == {"type", "attributes", "relationships"}
            assert data["relationships"] == {"app": {"data": {"type": "apps", "id": self.app_id}}}
            submission_id = f"rs-{self._next_id}"
            self._next_id += 1
            submission = {
                "id": submission_id,
                "type": "reviewSubmissions",
                "attributes": {"state": "READY_FOR_REVIEW", "platform": "IOS"},
                "relationships": {
                    "app": {"data": {"type": "apps", "id": self.app_id}},
                    "appStoreVersionForReview": {"data": None},
                },
            }
            self.submissions[submission_id] = submission
            self.items[submission_id] = []
            return {"data": submission}
        m = re.fullmatch(r"/v1/reviewSubmissions/([^/]+)", base)
        if m:
            submission = self.submissions.get(m.group(1))
            if submission is None:
                raise _http_404(method, base)
            if method == "GET":
                return {"data": submission}
            if method == "PATCH":
                assert payload["data"] == {
                    "type": "reviewSubmissions", "id": m.group(1), "attributes": {"submitted": True},
                }
                assert len(self.items[m.group(1)]) == 1
                submission["attributes"]["state"] = "WAITING_FOR_REVIEW"
                for item in self.items[m.group(1)]:
                    version_id = item["relationships"]["appStoreVersion"]["data"]["id"]
                    self.versions[version_id]["attributes"]["appStoreState"] = "WAITING_FOR_REVIEW"
                return {"data": submission}
        m = re.fullmatch(r"/v1/reviewSubmissions/([^/]+)/items", base)
        if m and method == "GET":
            return {"data": self.items.get(m.group(1), []), "links": {}}
        if base == "/v1/reviewSubmissionItems" and method == "POST":
            data = (payload or {}).get("data") or {}
            target_submission = data["relationships"]["reviewSubmission"]["data"]["id"]
            target_version = data["relationships"]["appStoreVersion"]["data"]["id"]
            item = {
                "id": f"item-{self._next_id}",
                "type": "reviewSubmissionItems",
                "relationships": {
                    "reviewSubmission": {"data": {"type": "reviewSubmissions", "id": target_submission}},
                    "appStoreVersion": {"data": {"type": "appStoreVersions", "id": target_version}},
                },
            }
            self._next_id += 1
            self.items.setdefault(target_submission, []).append(item)
            self.submissions[target_submission]["relationships"]["appStoreVersionForReview"] = {
                "data": {"type": "appStoreVersions", "id": target_version}
            }
            return {"data": item}
        raise AssertionError(f"FakeAsc: unexpected call {method} {base}")

    # -- introspection

    def mutating(self) -> list[dict]:
        return [c for c in self.calls if c["method"] != "GET"]


def _intent(**overrides) -> appstore_state.ReviewIntent:
    params = dict(
        bundle_id=BUNDLE_ID,
        marketing_version="1.2.3",
        build_number="5",
        whats_new={"ru": "Исправления и улучшения"},
        release_mode="manual",
        phased_release=True,
    )
    params.update(overrides)
    return appstore_state.ReviewIntent(**params)


def _operation(tmp_path, monkeypatch, intent=None) -> appstore_state.AppStoreReviewOperation:
    client = _stub_client(monkeypatch)
    return appstore_state.AppStoreReviewOperation(client, tmp_path, intent or _intent())


def _confirmed_checkpoint(tmp_path, outcome_or_id) -> appstore_state.ReviewCheckpoint:
    operation_id = getattr(outcome_or_id, "operation_id", outcome_or_id)
    return appstore_state.load_review_checkpoint(appstore_state.checkpoint_path(tmp_path, operation_id))


# --- intent, operation identity and checkpoint storage ----------------------------


def test_intent_rejects_invalid_parameters():
    with pytest.raises(AppStoreStateError):
        _intent(bundle_id="  ")
    with pytest.raises(AppStoreStateError):
        _intent(marketing_version="")
    with pytest.raises(AppStoreStateError):
        _intent(build_number=" ")
    with pytest.raises(AppStoreStateError, match="whats_new"):
        _intent(whats_new={})
    with pytest.raises(AppStoreStateError, match="whats_new"):
        _intent(whats_new={"ru": "   "})
    with pytest.raises(AppStoreStateError, match="release mode"):
        _intent(release_mode="whenever")
    with pytest.raises(AppStoreStateError, match="boolean"):
        _intent(phased_release=1)
    with pytest.raises(AppStoreStateError, match="boolean"):
        _intent(phased_release="true")


def test_operation_id_is_stable_and_parameter_sensitive():
    base = appstore_state.compute_operation_id(_intent())
    assert base == appstore_state.compute_operation_id(_intent())
    for changed in (
        _intent(marketing_version="1.2.4"),
        _intent(build_number="6"),
        _intent(whats_new={"ru": "Другой текст"}),
        _intent(whats_new={"ru": "Текст", "en-US": "Text"}),
        _intent(release_mode="automatic"),
        _intent(phased_release=False),
        _intent(bundle_id=OTHER_BUNDLE_ID),
    ):
        assert appstore_state.compute_operation_id(changed) != base


def test_checkpoint_and_lock_paths_are_under_appstore_state_dir():
    assert appstore_state.checkpoint_path(Path("root"), "opid") == (
        Path("root") / ".cdt" / "appstore" / "operations" / "opid.json"
    )
    assert appstore_state.review_lock_path(Path("root"), "com.example.app") == (
        Path("root") / ".cdt" / "appstore" / "locks" / "com.example.app.lock"
    )
    # bundle ids are sanitized so odd characters cannot escape the locks dir
    assert "\\" not in appstore_state.review_lock_path(Path("root"), "com.example/app").name
    assert appstore_state.review_lock_path(Path("root"), "com.example/app").name.endswith("com.example_app.lock")


def test_review_checkpoint_roundtrip_and_strict_reading(tmp_path):
    checkpoint = appstore_state.ReviewCheckpoint(
        operation_id="op",
        bundle_id=BUNDLE_ID,
        platform="IOS",
        marketing_version="1.2.3",
        build_number="5",
        whats_new={"ru": "Текст"},
        release_mode="manual",
        phased_release=True,
    )
    path = tmp_path / "op.json"
    appstore_state.save_review_checkpoint(path, checkpoint)
    assert appstore_state.load_review_checkpoint(path) == checkpoint

    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(appstore_state.CheckpointError, match="corrupted"):
        appstore_state.load_review_checkpoint(path)

    path.write_text(json.dumps({"schema_version": 99}), encoding="utf-8")
    with pytest.raises(appstore_state.CheckpointError, match="schema version"):
        appstore_state.load_review_checkpoint(path)

    path.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
    with pytest.raises(appstore_state.CheckpointError, match="missing"):
        appstore_state.load_review_checkpoint(path)

    confirmed = appstore_state.ReviewCheckpoint(
        operation_id="op",
        bundle_id=BUNDLE_ID,
        platform="IOS",
        marketing_version="1.2.3",
        build_number="5",
        whats_new={"ru": "Текст"},
        release_mode="manual",
        phased_release=False,
        phase=appstore_state.PHASE_CONFIRMED,
    )
    path.write_text(json.dumps(confirmed.to_json()), encoding="utf-8")
    with pytest.raises(appstore_state.CheckpointError, match="requires a recorded result"):
        appstore_state.load_review_checkpoint(path)


def test_review_checkpoint_write_failure_is_explicit(tmp_path):
    (tmp_path / ".cdt").write_text("occupied by a regular file", encoding="utf-8")
    checkpoint = appstore_state.ReviewCheckpoint(
        operation_id="op",
        bundle_id=BUNDLE_ID,
        platform="IOS",
        marketing_version="1.2.3",
        build_number="5",
        whats_new={"ru": "Текст"},
        release_mode="manual",
        phased_release=True,
    )
    path = tmp_path / ".cdt" / "appstore" / "operations" / "op.json"
    with pytest.raises(appstore_state.CheckpointWriteError, match="cannot save"):
        appstore_state.save_review_checkpoint(path, checkpoint)


# --- fresh run and resumability ---------------------------------------------------


def test_intent_is_saved_before_any_external_call(tmp_path, monkeypatch):
    path = appstore_state.checkpoint_path(tmp_path, appstore_state.compute_operation_id(_intent()))

    def fake_asc_request(method, request_path, client, payload=None, retry_ambiguous=True):
        assert path.exists(), "the intent checkpoint must be durable before the first external call"
        raise AssertionError("unexpected external call")

    monkeypatch.setattr(appstore, "_asc_request", fake_asc_request)
    client = _stub_client(monkeypatch)

    with pytest.raises(AssertionError, match="unexpected external call"):
        appstore_state.AppStoreReviewOperation(client, tmp_path, _intent()).run()

    checkpoint = appstore_state.load_review_checkpoint(path)
    assert checkpoint.phase == appstore_state.PHASE_INTENT
    assert checkpoint.whats_new == {"ru": "Исправления и улучшения"}


def test_fresh_run_creates_version_prepares_and_submits(tmp_path, monkeypatch):
    asc = FakeAsc(monkeypatch)

    outcome = _operation(tmp_path, monkeypatch).run()

    assert outcome.resumed is False
    assert (outcome.marketing_version, outcome.build_number) == ("1.2.3", "5")
    assert outcome.submission_state == "WAITING_FOR_REVIEW"  # beyond the unsubmitted stages
    assert outcome.release_mode == "manual"
    assert outcome.release_type == "MANUAL"
    assert outcome.phased_release is True

    version = asc.versions[outcome.version_id]
    assert version["attributes"]["versionString"] == "1.2.3"
    assert version["relationships"]["build"]["data"]["id"] == "build-5"
    assert version["attributes"]["releaseType"] == "MANUAL"
    assert any(
        loc["attributes"]["whatsNew"] == "Исправления и улучшения"
        for loc in asc.localizations[outcome.version_id]
    )
    assert asc.phased[outcome.version_id] is not None

    submission = asc.submissions[outcome.submission_id]
    assert submission["attributes"]["state"] == "WAITING_FOR_REVIEW"
    assert submission["relationships"]["appStoreVersionForReview"]["data"]["id"] == outcome.version_id
    items = asc.items[outcome.submission_id]
    assert len(items) == 1
    assert items[0]["relationships"]["appStoreVersion"]["data"]["id"] == outcome.version_id

    checkpoint = _confirmed_checkpoint(tmp_path, outcome)
    assert checkpoint.phase == appstore_state.PHASE_CONFIRMED
    assert checkpoint.result["submission_state"] == "WAITING_FOR_REVIEW"
    assert checkpoint.result["phased_release"] is True
    assert checkpoint.blocked is None
    content = appstore_state.checkpoint_path(tmp_path, outcome.operation_id).read_text(encoding="utf-8")
    for secret in ("ASC_KEY_ID", "ASC_ISSUER_ID", "ASC_PRIVATE_KEY_PATH", "-----BEGIN PRIVATE KEY-----"):
        assert secret not in content

    # eight external changes, each issued exactly once and never blindly retried
    mutating = asc.mutating()
    assert len(mutating) == 8
    assert all(c["retry_ambiguous"] is False for c in mutating)
    assert len([c for c in mutating if c["path"] == "/v1/appStoreVersions" and c["method"] == "POST"]) == 1
    assert len([c for c in mutating if c["path"].startswith("/v1/reviewSubmissions") and c["method"] == "POST"]) == 1
    assert len([c for c in mutating if c["path"] == "/v1/reviewSubmissionItems"]) == 1
    assert len([c for c in mutating if c["path"] == "/v1/appStoreVersionPhasedReleases"]) == 1


@pytest.mark.parametrize("stop_after", [1, 2, 3, 4, 5, 6, 7, 8])
def test_resume_after_every_external_change_without_duplicates(tmp_path, monkeypatch, stop_after):
    asc = FakeAsc(monkeypatch)
    asc.stop_after_mutations = stop_after
    with pytest.raises(RunInterrupted):
        _operation(tmp_path, monkeypatch).run()

    outcome = _operation(tmp_path, monkeypatch).run()  # same remote state, fresh process

    assert outcome.submission_state == "WAITING_FOR_REVIEW"
    mutating = asc.mutating()
    assert len(mutating) == 8  # every change applied exactly once across both runs
    assert len([c for c in mutating if c["path"] == "/v1/appStoreVersions" and c["method"] == "POST"]) == 1
    assert len([c for c in mutating if c["path"].startswith("/v1/reviewSubmissions") and c["method"] == "POST"]) == 1
    assert len([c for c in mutating if c["path"] == "/v1/reviewSubmissionItems"]) == 1
    submit_patches = [
        c for c in mutating if c["method"] == "PATCH" and re.fullmatch(r"/v1/reviewSubmissions/rs-\d+", c["path"])
    ]
    assert len(submit_patches) == 1
    checkpoint = _confirmed_checkpoint(tmp_path, outcome)
    assert checkpoint.phase == appstore_state.PHASE_CONFIRMED
    assert checkpoint.result is not None


def test_repeat_after_completion_only_verifies_and_returns_result(tmp_path, monkeypatch):
    asc = FakeAsc(monkeypatch)
    first = _operation(tmp_path, monkeypatch).run()
    assert first.resumed is False

    asc.calls.clear()
    second = _operation(tmp_path, monkeypatch).run()

    assert second.resumed is True
    assert second.submission_id == first.submission_id
    assert second.submission_state == "WAITING_FOR_REVIEW"
    assert all(c["method"] == "GET" for c in asc.calls)  # re-verified, nothing submitted twice


def test_confirmed_checkpoint_with_missing_submission_is_rejected(tmp_path, monkeypatch):
    asc = FakeAsc(monkeypatch)
    outcome = _operation(tmp_path, monkeypatch).run()
    del asc.submissions[outcome.submission_id]

    asc.calls.clear()
    with pytest.raises(appstore_state.UnknownResultError, match="cannot be found"):
        _operation(tmp_path, monkeypatch).run()


def test_confirmed_checkpoint_with_unsubmitted_stage_is_rejected(tmp_path, monkeypatch):
    asc = FakeAsc(monkeypatch)
    outcome = _operation(tmp_path, monkeypatch).run()
    asc.submissions[outcome.submission_id]["attributes"]["state"] = "DRAFT"

    with pytest.raises(appstore_state.UnknownResultError, match="state 'DRAFT'"):
        _operation(tmp_path, monkeypatch).run()


# --- version state and build binding conflicts -------------------------------------


@pytest.mark.parametrize(
    "state",
    ["REJECTED", "METADATA_REJECTED", "WAITING_FOR_REVIEW", "PENDING_DEVELOPER_RELEASE", None],
)
def test_existing_version_in_non_editable_state_stops_before_changes(tmp_path, monkeypatch, state):
    asc = FakeAsc(monkeypatch)
    asc.add_version("v-1", state=state)

    with pytest.raises(review.VersionStateError, match="PREPARE_FOR_SUBMISSION"):
        _operation(tmp_path, monkeypatch).run()

    assert asc.mutating() == []  # nothing is prepared or submitted for such versions


def test_version_with_other_build_selected_is_not_overwritten(tmp_path, monkeypatch):
    asc = FakeAsc(monkeypatch)
    asc.add_version("v-1", build_id="build-other")

    with pytest.raises(review.BuildConflictError, match="already has build build-other"):
        _operation(tmp_path, monkeypatch).run()

    assert not any(
        c["method"] == "PATCH" and c["path"] == "/v1/appStoreVersions/v-1" for c in asc.calls
    )  # neither the binding nor anything else was touched


def test_matching_existing_binding_is_reused_without_patch(tmp_path, monkeypatch):
    asc = FakeAsc(monkeypatch)
    asc.add_version("v-1", build_id="build-5")

    outcome = _operation(tmp_path, monkeypatch).run()

    assert outcome.version_id == "v-1"
    version_patches = [
        c for c in asc.calls if c["method"] == "PATCH" and c["path"] == "/v1/appStoreVersions/v-1"
    ]
    # only the releaseType PATCH happened; the build binding was reused as-is
    assert [((c["payload"] or {}).get("data") or {}).get("attributes") for c in version_patches] == [
        {"releaseType": "MANUAL"}
    ]


# --- submission targeting ------------------------------------------------------------


def test_open_submission_with_foreign_items_is_never_reused_or_submitted(tmp_path, monkeypatch):
    asc = FakeAsc(monkeypatch)
    asc.add_version("v-1")
    asc.add_submission("rs-user", state="DRAFT", version_id=None, item_version_ids=("v-other",))

    with pytest.raises(review.SubmissionConflictError, match="other versions"):
        _operation(tmp_path, monkeypatch).run()

    assert not any(c["path"] == "/v1/reviewSubmissionItems" for c in asc.calls)
    assert asc.submissions["rs-user"]["attributes"]["state"] == "DRAFT"


def test_empty_open_draft_is_adopted_and_filled(tmp_path, monkeypatch):
    asc = FakeAsc(monkeypatch)
    asc.add_version("v-1")
    asc.add_submission("rs-user", state="DRAFT", version_id=None)

    outcome = _operation(tmp_path, monkeypatch).run()

    assert outcome.submission_id == "rs-user"
    assert len(asc.items["rs-user"]) == 1
    assert asc.submissions["rs-user"]["attributes"]["state"] == "WAITING_FOR_REVIEW"
    assert not any(
        c["path"] == "/v1/reviewSubmissions" and c["method"] == "POST" for c in asc.calls
    )  # the existing draft was reused, not duplicated


def test_already_submitted_open_submission_completes_without_resubmitting(tmp_path, monkeypatch):
    asc = FakeAsc(monkeypatch)
    asc.add_version("v-1")
    asc.add_submission("rs-user", state="WAITING_FOR_REVIEW", version_id="v-1", item_version_ids=("v-1",))

    outcome = _operation(tmp_path, monkeypatch).run()

    assert outcome.resumed is True
    assert outcome.submission_state == "WAITING_FOR_REVIEW"
    assert not any(
        c["method"] in ("POST", "PATCH") and c["path"].startswith("/v1/reviewSubmissions") for c in asc.calls
    )
    assert _confirmed_checkpoint(tmp_path, outcome).phase == appstore_state.PHASE_CONFIRMED


# --- lost responses -----------------------------------------------------------------


def test_lost_submit_response_is_confirmed_via_get(tmp_path, monkeypatch):
    asc = FakeAsc(monkeypatch)
    asc.fail_on("PATCH", "/v1/reviewSubmissions", _ambiguous("PATCH", "/v1/reviewSubmissions"), apply=True)

    outcome = _operation(tmp_path, monkeypatch).run()

    assert outcome.submission_state == "WAITING_FOR_REVIEW"
    assert outcome.resumed is False
    submit_patch_path = f"/v1/reviewSubmissions/{outcome.submission_id}"
    assert len([c for c in asc.calls if c["method"] == "PATCH" and c["path"] == submit_patch_path]) == 1
    assert _confirmed_checkpoint(tmp_path, outcome).phase == appstore_state.PHASE_CONFIRMED


def test_lost_submit_response_without_remote_change_blocks_and_persists(tmp_path, monkeypatch):
    asc = FakeAsc(monkeypatch)
    asc.fail_on("PATCH", "/v1/reviewSubmissions", _ambiguous("PATCH", "/v1/reviewSubmissions"), apply=False)

    with pytest.raises(appstore_state.UnknownResultError, match="App Store Connect"):
        _operation(tmp_path, monkeypatch).run()

    checkpoint = _confirmed_checkpoint(tmp_path, appstore_state.compute_operation_id(_intent()))
    assert checkpoint.blocked is not None
    assert checkpoint.blocked["category"] == "submit_lost"

    # The blocking state persists: a re-run makes no external calls at all.
    asc.calls.clear()
    with pytest.raises(appstore_state.UnknownResultError, match="blocked"):
        _operation(tmp_path, monkeypatch).run()
    assert asc.calls == []


def test_lost_submission_creation_is_adopted_when_verifiable(tmp_path, monkeypatch):
    asc = FakeAsc(monkeypatch)
    asc.fail_on("POST", "/v1/reviewSubmissions", _ambiguous("POST", "/v1/reviewSubmissions"), apply=True)

    outcome = _operation(tmp_path, monkeypatch).run()

    assert outcome.submission_state == "WAITING_FOR_REVIEW"
    assert len([c for c in asc.calls if c["path"] == "/v1/reviewSubmissions" and c["method"] == "POST"]) == 1


def test_lost_submission_creation_without_remote_change_blocks(tmp_path, monkeypatch):
    asc = FakeAsc(monkeypatch)
    asc.fail_on("POST", "/v1/reviewSubmissions", _ambiguous("POST", "/v1/reviewSubmissions"), apply=False)

    with pytest.raises(appstore_state.UnknownResultError, match="blocked"):
        _operation(tmp_path, monkeypatch).run()

    checkpoint = _confirmed_checkpoint(tmp_path, appstore_state.compute_operation_id(_intent()))
    assert checkpoint.blocked is not None
    assert checkpoint.blocked["category"] == "submission_create_lost"


def test_lost_build_binding_response_is_confirmed_via_get(tmp_path, monkeypatch):
    asc = FakeAsc(monkeypatch)
    asc.add_version("v-1")
    asc.fail_on(
        "PATCH",
        "/v1/appStoreVersions",
        _ambiguous("PATCH", "/v1/appStoreVersions"),
        apply=True,
        relationships=True,
    )

    _operation(tmp_path, monkeypatch).run()

    assert asc.versions["v-1"]["relationships"]["build"]["data"]["id"] == "build-5"
    binding_patches = [
        c
        for c in asc.calls
        if c["method"] == "PATCH" and c["path"] == "/v1/appStoreVersions/v-1"
        and "relationships" in ((c["payload"] or {}).get("data") or {})
    ]
    assert len(binding_patches) == 1  # confirmed via GET, never repeated


def test_lost_build_binding_without_remote_change_blocks(tmp_path, monkeypatch):
    asc = FakeAsc(monkeypatch)
    asc.add_version("v-1")
    asc.fail_on(
        "PATCH",
        "/v1/appStoreVersions",
        _ambiguous("PATCH", "/v1/appStoreVersions"),
        apply=False,
        relationships=True,
    )

    with pytest.raises(appstore_state.UnknownResultError, match="blocked"):
        _operation(tmp_path, monkeypatch).run()

    assert asc.versions["v-1"]["relationships"]["build"]["data"] is None  # never bound blindly
    checkpoint = _confirmed_checkpoint(tmp_path, appstore_state.compute_operation_id(_intent()))
    assert checkpoint.blocked is not None
    assert checkpoint.blocked["category"] == "build_binding_lost"


def test_lost_whats_new_response_is_confirmed_via_get(tmp_path, monkeypatch):
    asc = FakeAsc(monkeypatch)
    asc.fail_on(
        "PATCH",
        "/v1/appStoreVersionLocalizations",
        _ambiguous("PATCH", "/v1/appStoreVersionLocalizations"),
        apply=True,
    )

    outcome = _operation(tmp_path, monkeypatch).run()

    assert any(
        loc["attributes"]["whatsNew"] == "Исправления и улучшения"
        for loc in asc.localizations[outcome.version_id]
    )
    localization_patches = [
        c
        for c in asc.calls
        if c["method"] == "PATCH" and c["path"].startswith("/v1/appStoreVersionLocalizations")
    ]
    assert len(localization_patches) == 1  # confirmed via GET, never repeated


def test_lost_whats_new_without_remote_change_blocks(tmp_path, monkeypatch):
    asc = FakeAsc(monkeypatch)
    asc.fail_on(
        "PATCH",
        "/v1/appStoreVersionLocalizations",
        _ambiguous("PATCH", "/v1/appStoreVersionLocalizations"),
        apply=False,
    )

    with pytest.raises(appstore_state.UnknownResultError, match="blocked"):
        _operation(tmp_path, monkeypatch).run()

    checkpoint = _confirmed_checkpoint(tmp_path, appstore_state.compute_operation_id(_intent()))
    assert checkpoint.blocked is not None
    assert checkpoint.blocked["category"] == "whats_new_lost"


def test_lost_release_type_is_reconciled_via_get(tmp_path, monkeypatch):
    asc = FakeAsc(monkeypatch)
    asc.fail_on(
        "PATCH", "/v1/appStoreVersions", _ambiguous("PATCH", "/v1/appStoreVersions"),
        apply=True, attributes={"releaseType": "MANUAL"},
    )

    outcome = _operation(tmp_path, monkeypatch).run()

    assert outcome.release_type == "MANUAL"
    assert asc.versions[outcome.version_id]["attributes"]["releaseType"] == "MANUAL"


def test_lost_phased_release_delete_blocks_when_not_applied(tmp_path, monkeypatch):
    intent = _intent(phased_release=False)
    asc = FakeAsc(monkeypatch)
    asc.add_version("v-1")
    asc.phased["v-1"] = {
        "id": "ph-1",
        "type": "appStoreVersionPhasedReleases",
        "attributes": {"phasedReleaseState": "INACTIVE"},
    }
    delete_path = "/v1/appStoreVersionPhasedReleases/ph-1"
    asc.fail_on("DELETE", delete_path, _ambiguous("DELETE", delete_path), apply=False)

    with pytest.raises(appstore_state.UnknownResultError, match="blocked"):
        _operation(tmp_path, monkeypatch, intent).run()

    assert asc.phased["v-1"] is not None  # never deleted blindly
    checkpoint = _confirmed_checkpoint(tmp_path, appstore_state.compute_operation_id(intent))
    assert checkpoint.blocked is not None
    assert checkpoint.blocked["category"] == "phased_release_lost"


def test_phased_release_disabled_is_a_noop_when_absent(tmp_path, monkeypatch):
    asc = FakeAsc(monkeypatch)

    outcome = _operation(tmp_path, monkeypatch, _intent(phased_release=False)).run()

    assert outcome.phased_release is False
    assert not any(c["path"].startswith("/v1/appStoreVersionPhasedReleases") for c in asc.mutating())
    assert asc.phased[outcome.version_id] is None


@pytest.mark.parametrize("state", ["UNRESOLVED_ISSUES", "CANCELING", "UNKNOWN"])
def test_unsafe_submission_state_never_submits_or_creates_duplicate(tmp_path, monkeypatch, state):
    asc = FakeAsc(monkeypatch)
    asc.add_version("v-1")
    asc.add_submission("rs-user", state=state, version_id="v-1", item_version_ids=("v-1",))
    with pytest.raises(review.SubmissionConflictError, match="state"):
        _operation(tmp_path, monkeypatch).run()
    assert not any(c["path"].startswith("/v1/reviewSubmission") for c in asc.mutating())


@pytest.mark.parametrize("change", ["build", "empty_items", "duplicate_items", "unknown_state"])
def test_confirmed_operation_requires_exact_remote_target(tmp_path, monkeypatch, change):
    asc = FakeAsc(monkeypatch)
    outcome = _operation(tmp_path, monkeypatch).run()
    if change == "build":
        asc.versions[outcome.version_id]["relationships"]["build"]["data"]["id"] = "other-build"
    elif change == "empty_items":
        asc.items[outcome.submission_id].clear()
    elif change == "duplicate_items":
        asc.items[outcome.submission_id] *= 2
    else:
        asc.submissions[outcome.submission_id]["attributes"]["state"] = "UNKNOWN"
    asc.calls.clear()
    with pytest.raises((appstore_state.UnknownResultError, review.SubmissionConflictError)):
        _operation(tmp_path, monkeypatch).run()
    assert asc.mutating() == []


@pytest.mark.parametrize("endpoint,category", [
    ("/v1/appStoreVersions", "version_create_lost"),
    ("/v1/reviewSubmissionItems", "item_create_lost"),
])
def test_unconfirmed_creation_is_blocked_across_runs(tmp_path, monkeypatch, endpoint, category):
    asc = FakeAsc(monkeypatch)
    asc.fail_on("POST", endpoint, _ambiguous("POST", endpoint), apply=False)
    with pytest.raises(appstore_state.UnknownResultError, match="blocked"):
        _operation(tmp_path, monkeypatch).run()
    checkpoint = _confirmed_checkpoint(tmp_path, appstore_state.compute_operation_id(_intent()))
    assert checkpoint.blocked["category"] == category
    asc.calls.clear()
    with pytest.raises(appstore_state.UnknownResultError, match="blocked"):
        _operation(tmp_path, monkeypatch).run()
    assert asc.calls == []


# --- checkpoint bookkeeping, conflicts and coordination -------------------------------


def _save_other_checkpoint(tmp_path, **overrides) -> Path:
    params = dict(
        operation_id="other-operation",
        bundle_id=BUNDLE_ID,
        platform="IOS",
        marketing_version="1.2.3",
        build_number="5",
        whats_new={"ru": "Текст"},
        release_mode="manual",
        phased_release=True,
    )
    params.update(overrides)
    checkpoint = appstore_state.ReviewCheckpoint(**params)
    appstore_state.save_review_checkpoint(
        tmp_path / ".cdt" / "appstore" / "operations" / f"{params['operation_id']}.json", checkpoint
    )
    return appstore_state.checkpoint_path(tmp_path, params["operation_id"])


def test_unfinished_operation_with_other_parameters_blocks(tmp_path, monkeypatch):
    asc = FakeAsc(monkeypatch)
    _save_other_checkpoint(tmp_path, phase=appstore_state.PHASE_SUBMISSION_READY)

    with pytest.raises(appstore_state.ConflictingOperationError, match="changed parameters"):
        _operation(tmp_path, monkeypatch).run()

    assert asc.calls == []


def test_confirmed_and_foreign_app_operations_do_not_block(tmp_path, monkeypatch):
    FakeAsc(monkeypatch)
    _save_other_checkpoint(
        tmp_path,
        phase=appstore_state.PHASE_CONFIRMED,
        result={"submission_id": "rs-old", "submission_state": "WAITING_FOR_REVIEW"},
    )
    _save_other_checkpoint(tmp_path, bundle_id=OTHER_BUNDLE_ID, phase=appstore_state.PHASE_INTENT)

    outcome = _operation(tmp_path, monkeypatch).run()

    assert outcome.submission_state == "WAITING_FOR_REVIEW"


def test_concurrent_local_run_is_rejected_by_the_app_lock(tmp_path, monkeypatch):
    asc = FakeAsc(monkeypatch)
    lock_file = appstore_state.review_lock_path(tmp_path, BUNDLE_ID)
    lock_file.parent.mkdir(parents=True, exist_ok=True)

    with filelock.FileLock(lock_file, timeout=0, thread_local=False):
        with pytest.raises(appstore_state.OperationLockedError, match="already running"):
            _operation(tmp_path, monkeypatch).run()

    assert asc.calls == []


def test_checkpoint_write_failure_prevents_any_external_call(tmp_path, monkeypatch):
    asc = FakeAsc(monkeypatch)

    def failing_save(path, checkpoint):
        raise appstore_state.CheckpointWriteError("cannot save App Store review checkpoint: disk full")

    monkeypatch.setattr(appstore_state, "save_review_checkpoint", failing_save)

    with pytest.raises(appstore_state.CheckpointWriteError, match="cannot save"):
        _operation(tmp_path, monkeypatch).run()

    assert asc.calls == []  # the intent is durable before anything touches Apple


def test_corrupted_checkpoint_stops_without_external_calls(tmp_path, monkeypatch):
    asc = FakeAsc(monkeypatch)
    path = appstore_state.checkpoint_path(tmp_path, appstore_state.compute_operation_id(_intent()))
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")

    with pytest.raises(appstore_state.CheckpointError, match="corrupted"):
        _operation(tmp_path, monkeypatch).run()

    assert asc.calls == []
