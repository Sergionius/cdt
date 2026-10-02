"""HTTP-mock tests for safe ASC queries and App Review operations."""

import pytest

from cdt.services import appstore
from cdt.services import appstore_review as review


def _stub_client(monkeypatch) -> appstore._AscClient:
    monkeypatch.setattr(appstore, "_asc_token", lambda env: "token0")
    return appstore._AscClient({})


class ScriptedAsc:
    """Stand-in for ``appstore._asc_request`` replaying scripted responses.

    Records every call (method, path, payload and the ``retry_ambiguous``
    mode) so tests can assert both wire behaviour and retry safety.
    """

    def __init__(self, monkeypatch, script: list | None = None):
        self.script = list(script or [])
        self.calls: list[dict] = []
        monkeypatch.setattr(appstore, "_asc_request", self)

    def __call__(self, method: str, path: str, client, payload=None, retry_ambiguous=True):
        self.calls.append(
            {"method": method, "path": path, "payload": payload, "retry_ambiguous": retry_ambiguous}
        )
        outcome = self.script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def paths(self, method: str | None = None) -> list[str]:
        return [c["path"] for c in self.calls if method is None or c["method"] == method]

    def calls_for(self, method: str, path: str | None = None) -> list[dict]:
        return [
            c
            for c in self.calls
            if c["method"] == method and (path is None or c["path"] == path)
        ]

    def mutating(self) -> list[dict]:
        return [c for c in self.calls if c["method"] != "GET"]


def _ambiguous_error(path: str, code: int | None = 502) -> appstore.AscAmbiguousResultError:
    category = f"http_{code}" if code is not None else "timeout"
    detail = "lost response" if code is not None else "connection reset"
    return appstore.AscAmbiguousResultError(
        "ambiguous", method="POST", path=path, code=code, category=category, detail=detail
    )


def _list_rsp(data: list, included: list | None = None, next_path: str | None = None) -> dict:
    rsp: dict = {"data": data, "links": {}}
    if included is not None:
        rsp["included"] = included
    if next_path:
        rsp["links"]["next"] = appstore.ASC_API_BASE + next_path
    return rsp


def _app(app_id: str, bundle_id: str) -> dict:
    return {"id": app_id, "type": "apps", "attributes": {"bundleId": bundle_id}}


def _build(
    build_id: str,
    build_number: str,
    pre_id: str,
    app_id: str,
    state: str = "VALID",
    expired: bool = False,
) -> dict:
    return {
        "id": build_id,
        "type": "builds",
        "attributes": {"version": build_number, "processingState": state, "expired": expired},
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


def _version(version_id: str, version_string: str, platform: str = "IOS", state: str | None = None) -> dict:
    attributes: dict = {"versionString": version_string, "platform": platform}
    if state is not None:
        attributes["appStoreState"] = state
    return {"id": version_id, "type": "appStoreVersions", "attributes": attributes}


def _localization(loc_id: str, locale: str) -> dict:
    return {"id": loc_id, "type": "appStoreVersionLocalizations", "attributes": {"locale": locale}}


def _submission(submission_id: str, state: str, platform: str = "IOS") -> dict:
    return {
        "id": submission_id,
        "type": "reviewSubmissions",
        "attributes": {"state": state, "platform": platform},
    }


def _http_404(path: str) -> appstore.AscHttpError:
    return appstore.AscHttpError("not found", code=404, method="GET", path=path, body="not found")


# --- exact app lookup -------------------------------------------------------


def test_find_app_id_follows_pagination_and_matches_bundle_exactly(monkeypatch):
    asc = ScriptedAsc(
        monkeypatch,
        [
            _list_rsp([_app("app-loose", "com.example.app.free")], next_path="/v1/apps?cursor=2"),
            _list_rsp([_app("app-exact", "com.example.app")]),
        ],
    )

    assert review.find_app_id("com.example.app", _stub_client(monkeypatch)) == "app-exact"
    assert len(asc.calls_for("GET")) == 2
    assert "filter[bundleId]=com.example.app" in asc.paths("GET")[0]


def test_find_app_id_rejects_multiple_exact_matches(monkeypatch):
    ScriptedAsc(
        monkeypatch,
        [
            _list_rsp(
                [_app("app-1", "com.example.app"), _app("app-2", "com.example.app")]
            )
        ],
    )

    with pytest.raises(review.AmbiguousResourceError, match="com.example.app"):
        review.find_app_id("com.example.app", _stub_client(monkeypatch))


def test_find_app_id_reports_missing_app(monkeypatch):
    ScriptedAsc(monkeypatch, [_list_rsp([])])

    with pytest.raises(review.ResourceNotFoundError, match="com.example.missing"):
        review.find_app_id("com.example.missing", _stub_client(monkeypatch))


# --- exact build lookup and validation ---------------------------------------


def test_find_build_follows_pagination_and_matches_exact_ios_build(monkeypatch):
    builds = [
        _build("build-44", "44", "pre-1", "app-1"),
        _build("build-mac", "45", "pre-mac", "app-1"),
        _build("build-45", "45", "pre-2", "app-1"),
    ]
    included = [
        _pre_release("pre-1", "1.2.3"),
        _pre_release("pre-mac", "1.2.3", platform="MAC_OS"),
        _pre_release("pre-2", "1.2.3"),
    ]
    asc = ScriptedAsc(
        monkeypatch,
        [
            _list_rsp(builds[:2], included=included, next_path="/v1/builds?cursor=2"),
            _list_rsp(builds[2:], included=[included[2]]),
        ],
    )

    build = review.find_build("app-1", "1.2.3", "45", _stub_client(monkeypatch))

    assert build["id"] == "build-45"
    assert asc.calls_for("GET", asc.paths("GET")[0]) == [asc.calls[0]]
    first_path = asc.paths("GET")[0]
    assert "filter[app]=app-1" in first_path
    assert "filter[version]=45" in first_path
    assert "filter[preReleaseVersion.version]=1.2.3" in first_path


def test_find_build_rejects_multiple_exact_matches(monkeypatch):
    builds = [_build("build-a", "45", "pre-1", "app-1"), _build("build-b", "45", "pre-1", "app-1")]
    included = [_pre_release("pre-1", "1.2.3")]
    ScriptedAsc(monkeypatch, [_list_rsp(builds, included=included)])

    with pytest.raises(review.AmbiguousResourceError, match="cannot pick one"):
        review.find_build("app-1", "1.2.3", "45", _stub_client(monkeypatch))


def test_find_build_reports_missing_build(monkeypatch):
    ScriptedAsc(monkeypatch, [_list_rsp([])])

    with pytest.raises(review.ResourceNotFoundError, match="1.2.3 \\(45\\)"):
        review.find_build("app-1", "1.2.3", "45", _stub_client(monkeypatch))


def test_find_build_rejects_unprocessed_build(monkeypatch):
    builds = [_build("build-45", "45", "pre-1", "app-1", state="PROCESSING")]
    ScriptedAsc(monkeypatch, [_list_rsp(builds, included=[_pre_release("pre-1", "1.2.3")])])

    with pytest.raises(review.InvalidBuildError, match="processingState=PROCESSING"):
        review.find_build("app-1", "1.2.3", "45", _stub_client(monkeypatch))


def test_find_build_rejects_expired_build(monkeypatch):
    builds = [_build("build-45", "45", "pre-1", "app-1", expired=True)]
    ScriptedAsc(monkeypatch, [_list_rsp(builds, included=[_pre_release("pre-1", "1.2.3")])])

    with pytest.raises(review.InvalidBuildError, match="expired"):
        review.find_build("app-1", "1.2.3", "45", _stub_client(monkeypatch))


def test_find_build_rejects_build_of_other_app(monkeypatch):
    builds = [_build("build-45", "45", "pre-1", "app-other")]
    ScriptedAsc(monkeypatch, [_list_rsp(builds, included=[_pre_release("pre-1", "1.2.3")])])

    with pytest.raises(review.InvalidBuildError, match="different app"):
        review.find_build("app-1", "1.2.3", "45", _stub_client(monkeypatch))


def test_validate_build_accepts_valid_current_build():
    build = _build("build-45", "45", "pre-1", "app-1")
    review.validate_build_for_review("app-1", build)  # must not raise


# --- App Store version lookup and creation -----------------------------------


def test_find_app_store_version_returns_none_when_missing(monkeypatch):
    ScriptedAsc(monkeypatch, [_list_rsp([_version("v-9", "1.2.4")])])

    result = review.find_app_store_version("app-1", "1.2.3", _stub_client(monkeypatch))

    assert result is None


def test_find_app_store_version_returns_exact_match(monkeypatch):
    ScriptedAsc(monkeypatch, [_list_rsp([_version("v-1", "1.2.3"), _version("v-2", "1.2.4")])])

    result = review.find_app_store_version("app-1", "1.2.3", _stub_client(monkeypatch))

    assert result is not None
    assert result["id"] == "v-1"


def test_find_app_store_version_rejects_ambiguous(monkeypatch):
    ScriptedAsc(monkeypatch, [_list_rsp([_version("v-1", "1.2.3"), _version("v-2", "1.2.3")])])

    with pytest.raises(review.AmbiguousResourceError):
        review.find_app_store_version("app-1", "1.2.3", _stub_client(monkeypatch))


def test_get_or_create_version_returns_existing_without_post(monkeypatch):
    asc = ScriptedAsc(monkeypatch, [_list_rsp([_version("v-1", "1.2.3")])])

    result = review.get_or_create_app_store_version("app-1", "1.2.3", _stub_client(monkeypatch))

    assert result["id"] == "v-1"
    assert asc.mutating() == []


def test_get_or_create_version_creates_missing_version_in_safe_mode(monkeypatch):
    asc = ScriptedAsc(monkeypatch, [_list_rsp([]), {"data": _version("v-new", "1.2.3")}])

    result = review.get_or_create_app_store_version("app-1", "1.2.3", _stub_client(monkeypatch))

    assert result["id"] == "v-new"
    post_calls = asc.calls_for("POST", "/v1/appStoreVersions")
    assert len(post_calls) == 1
    assert post_calls[0]["retry_ambiguous"] is False
    payload = post_calls[0]["payload"]["data"]
    assert payload["attributes"] == {"platform": "IOS", "versionString": "1.2.3"}
    assert payload["relationships"]["app"]["data"] == {"type": "apps", "id": "app-1"}


def test_get_or_create_version_reconciles_after_ambiguous_creation(monkeypatch):
    asc = ScriptedAsc(
        monkeypatch,
        [
            _list_rsp([]),
            _ambiguous_error("/v1/appStoreVersions"),
            _list_rsp([_version("v-created", "1.2.3")]),
        ],
    )

    result = review.get_or_create_app_store_version("app-1", "1.2.3", _stub_client(monkeypatch))

    assert result["id"] == "v-created"  # confirmed by GET, not by a repeated POST
    assert len(asc.calls_for("POST")) == 1
    assert asc.calls[-1]["method"] == "GET"


def test_get_or_create_version_reraises_when_reconciliation_finds_nothing(monkeypatch):
    asc = ScriptedAsc(
        monkeypatch,
        [
            _list_rsp([]),
            _ambiguous_error("/v1/appStoreVersions"),
            _list_rsp([]),
        ],
    )

    with pytest.raises(appstore.AscAmbiguousResultError):
        review.get_or_create_app_store_version("app-1", "1.2.3", _stub_client(monkeypatch))

    assert len(asc.calls_for("POST")) == 1  # still no blind retry


# --- build binding ------------------------------------------------------------


def test_get_version_build_id_returns_bound_build(monkeypatch):
    ScriptedAsc(monkeypatch, [{"data": {"id": "build-45", "type": "builds", "attributes": {}}}])

    assert review.get_version_build_id("v-1", _stub_client(monkeypatch)) == "build-45"


def test_get_version_build_id_handles_absent_binding(monkeypatch):
    ScriptedAsc(monkeypatch, [{"data": None}, _http_404("/v1/appStoreVersions/v-1/build")])

    assert review.get_version_build_id("v-1", _stub_client(monkeypatch)) is None
    assert review.get_version_build_id("v-1", _stub_client(monkeypatch)) is None


def test_set_version_build_patches_relationship_in_safe_mode(monkeypatch):
    asc = ScriptedAsc(monkeypatch, [{}])

    review.set_version_build("v-1", "build-45", _stub_client(monkeypatch))

    (call,) = asc.calls
    assert call["method"] == "PATCH"
    assert call["path"] == "/v1/appStoreVersions/v-1"
    assert call["retry_ambiguous"] is False
    assert call["payload"]["data"]["relationships"]["build"]["data"] == {"type": "builds", "id": "build-45"}


# --- whatsNew localizations ----------------------------------------------------


def test_set_whats_new_patches_only_requested_existing_localizations(monkeypatch):
    asc = ScriptedAsc(
        monkeypatch,
        [
            _list_rsp(
                [_localization("loc-ru", "ru"), _localization("loc-en", "en-US"), _localization("loc-de", "de-DE")]
            ),
            {},
            {},
        ],
    )

    updated = review.set_whats_new("v-1", {"ru": "Исправления", "en-US": "Bug fixes"}, _stub_client(monkeypatch))

    assert updated == ["en-US", "ru"]
    patches = asc.calls_for("PATCH")
    assert [
        c["path"] for c in patches
    ] == ["/v1/appStoreVersionLocalizations/loc-ru", "/v1/appStoreVersionLocalizations/loc-en"]
    assert all(c["retry_ambiguous"] is False for c in patches)
    assert patches[0]["payload"]["data"]["attributes"] == {"whatsNew": "Исправления"}
    assert patches[1]["payload"]["data"]["attributes"] == {"whatsNew": "Bug fixes"}


def test_set_whats_new_validates_all_locales_before_first_write(monkeypatch):
    asc = ScriptedAsc(monkeypatch, [_list_rsp([_localization("loc-ru", "ru")])])

    with pytest.raises(review.UnknownLocaleError, match="fr-FR") as excinfo:
        review.set_whats_new("v-1", {"ru": "Текст", "fr-FR": "Texte"}, _stub_client(monkeypatch))

    assert "App Store Connect" in str(excinfo.value)
    assert asc.mutating() == []  # nothing written, no partial app card


def test_set_whats_new_rejects_empty_dict(monkeypatch):
    asc = ScriptedAsc(monkeypatch, [])

    with pytest.raises(review.AppStoreReviewError, match="at least one locale"):
        review.set_whats_new("v-1", {}, _stub_client(monkeypatch))

    assert asc.calls == []


# --- release type and phased release -------------------------------------------


def test_set_release_type_maps_manual_and_automatic(monkeypatch):
    asc = ScriptedAsc(monkeypatch, [{}, {}])

    assert review.set_release_type("v-1", "manual", _stub_client(monkeypatch)) == "MANUAL"
    assert review.set_release_type("v-1", "automatic", _stub_client(monkeypatch)) == "AUTOMATIC"
    assert [c["payload"]["data"]["attributes"]["releaseType"] for c in asc.calls_for("PATCH")] == [
        "MANUAL",
        "AUTOMATIC",
    ]
    assert all(c["retry_ambiguous"] is False for c in asc.calls_for("PATCH"))


def test_set_release_type_rejects_unknown_mode(monkeypatch):
    asc = ScriptedAsc(monkeypatch, [])

    with pytest.raises(review.AppStoreReviewError, match="manual"):
        review.set_release_type("v-1", "whenever", _stub_client(monkeypatch))

    assert asc.calls == []


def test_set_phased_release_creates_inactive_when_absent(monkeypatch):
    path = "/v1/appStoreVersions/v-1/phasedRelease"
    asc = ScriptedAsc(
        monkeypatch,
        [
            _http_404(path),
            {
                "data": {
                    "id": "ph-1",
                    "type": "appStoreVersionPhasedReleases",
                    "attributes": {"phasedReleaseState": "INACTIVE"},
                }
            },
        ],
    )

    assert review.set_phased_release("v-1", True, _stub_client(monkeypatch)) == "INACTIVE"
    (post_call,) = asc.calls_for("POST", path)
    assert post_call["retry_ambiguous"] is False
    assert post_call["payload"]["data"]["attributes"] == {"phasedReleaseState": "INACTIVE"}


def test_set_phased_release_keeps_existing_when_enabling(monkeypatch):
    existing = {"id": "ph-1", "type": "appStoreVersionPhasedReleases", "attributes": {"phasedReleaseState": "INACTIVE"}}
    asc = ScriptedAsc(monkeypatch, [{"data": existing}])

    assert review.set_phased_release("v-1", True, _stub_client(monkeypatch)) == "INACTIVE"
    assert asc.mutating() == []


def test_set_phased_release_reconciles_after_ambiguous_creation(monkeypatch):
    path = "/v1/appStoreVersions/v-1/phasedRelease"
    existing = {"id": "ph-1", "type": "appStoreVersionPhasedReleases", "attributes": {"phasedReleaseState": "INACTIVE"}}
    asc = ScriptedAsc(monkeypatch, [_http_404(path), _ambiguous_error(path), {"data": existing}])

    assert review.set_phased_release("v-1", True, _stub_client(monkeypatch)) == "INACTIVE"
    assert len(asc.calls_for("POST", path)) == 1  # confirmed via GET, not repeated


def test_set_phased_release_deletes_when_disabling(monkeypatch):
    path = "/v1/appStoreVersions/v-1/phasedRelease"
    existing = {"id": "ph-1", "type": "appStoreVersionPhasedReleases", "attributes": {"phasedReleaseState": "ACTIVE"}}
    asc = ScriptedAsc(monkeypatch, [{"data": existing}, {}])

    assert review.set_phased_release("v-1", False, _stub_client(monkeypatch)) is None
    (delete_call,) = asc.calls_for("DELETE", path)
    assert delete_call["retry_ambiguous"] is False


def test_set_phased_release_is_noop_when_absent_and_disabling(monkeypatch):
    path = "/v1/appStoreVersions/v-1/phasedRelease"
    asc = ScriptedAsc(monkeypatch, [_http_404(path)])

    assert review.set_phased_release("v-1", False, _stub_client(monkeypatch)) is None
    assert asc.mutating() == []


# --- review submissions ----------------------------------------------------------


def test_find_open_review_submission_returns_draft(monkeypatch):
    items = [
        _submission("rs-done", "COMPLETED"),
        _submission("rs-draft", "DRAFT", platform="IOS"),
    ]
    asc = ScriptedAsc(monkeypatch, [_list_rsp(items)])

    result = review.find_open_review_submission("app-1", _stub_client(monkeypatch))

    assert result is not None
    assert result["id"] == "rs-draft"
    assert "filter[platform]=IOS" in asc.paths("GET")[0]


def test_find_open_review_submission_rejects_multiple_open(monkeypatch):
    items = [
        _submission("rs-a", "DRAFT"),
        _submission("rs-b", "WAITING_FOR_REVIEW"),
    ]
    ScriptedAsc(monkeypatch, [_list_rsp(items)])

    with pytest.raises(review.AmbiguousResourceError, match="resolve them in App Store Connect"):
        review.find_open_review_submission("app-1", _stub_client(monkeypatch))


def test_find_open_review_submission_returns_none_when_only_terminal(monkeypatch):
    ScriptedAsc(monkeypatch, [_list_rsp([_submission("rs-old", "COMPLETED")])])

    assert review.find_open_review_submission("app-1", _stub_client(monkeypatch)) is None


def test_create_review_submission_targets_exact_version(monkeypatch):
    asc = ScriptedAsc(monkeypatch, [{"data": _submission("rs-1", "DRAFT")}])

    result = review.create_review_submission("app-1", "v-1", _stub_client(monkeypatch))

    assert result["id"] == "rs-1"
    (call,) = asc.calls
    assert call["method"] == "POST"
    assert call["path"] == "/v1/reviewSubmissions"
    assert call["retry_ambiguous"] is False
    payload = call["payload"]["data"]
    assert payload["attributes"] == {"platform": "IOS"}
    assert payload["relationships"]["app"]["data"] == {"type": "apps", "id": "app-1"}
    assert payload["relationships"]["appStoreVersionForReview"]["data"] == {
        "type": "appStoreVersions",
        "id": "v-1",
    }


def test_add_review_submission_item_reuses_existing_item(monkeypatch):
    existing_item = {
        "id": "item-1",
        "type": "reviewSubmissionItems",
        "relationships": {"appStoreVersion": {"data": {"type": "appStoreVersions", "id": "v-1"}}},
    }
    asc = ScriptedAsc(monkeypatch, [_list_rsp([existing_item])])

    result = review.add_review_submission_item("rs-1", "v-1", _stub_client(monkeypatch))

    assert result["id"] == "item-1"
    assert asc.mutating() == []


def test_add_review_submission_item_creates_in_safe_mode(monkeypatch):
    created = {
        "id": "item-2",
        "type": "reviewSubmissionItems",
        "relationships": {"appStoreVersion": {"data": {"type": "appStoreVersions", "id": "v-2"}}},
    }
    asc = ScriptedAsc(monkeypatch, [_list_rsp([]), {"data": created}])

    result = review.add_review_submission_item("rs-1", "v-2", _stub_client(monkeypatch))

    assert result["id"] == "item-2"
    (post_call,) = asc.calls_for("POST", "/v1/reviewSubmissionItems")
    assert post_call["retry_ambiguous"] is False
    assert post_call["payload"]["data"]["relationships"] == {
        "reviewSubmission": {"data": {"type": "reviewSubmissions", "id": "rs-1"}},
        "appStoreVersion": {"data": {"type": "appStoreVersions", "id": "v-2"}},
    }


def test_add_review_submission_item_reconciles_after_ambiguous_creation(monkeypatch):
    created = {
        "id": "item-2",
        "type": "reviewSubmissionItems",
        "relationships": {"appStoreVersion": {"data": {"type": "appStoreVersions", "id": "v-2"}}},
    }
    asc = ScriptedAsc(
        monkeypatch,
        [
            _list_rsp([]),
            _ambiguous_error("/v1/reviewSubmissionItems"),
            _list_rsp([created]),
        ],
    )

    result = review.add_review_submission_item("rs-1", "v-2", _stub_client(monkeypatch))

    assert result["id"] == "item-2"
    assert len(asc.calls_for("POST")) == 1  # confirmed via GET, not repeated


def test_add_review_submission_item_reraises_when_reconciliation_finds_nothing(monkeypatch):
    asc = ScriptedAsc(
        monkeypatch,
        [_list_rsp([]), _ambiguous_error("/v1/reviewSubmissionItems"), _list_rsp([])],
    )

    with pytest.raises(appstore.AscAmbiguousResultError):
        review.add_review_submission_item("rs-1", "v-2", _stub_client(monkeypatch))

    assert len(asc.calls_for("POST")) == 1


def test_submit_review_submission_sends_submitted_with_full_items_list(monkeypatch):
    asc = ScriptedAsc(monkeypatch, [{"data": _submission("rs-1", "WAITING_FOR_REVIEW")}])

    result = review.submit_review_submission("rs-1", ["item-1", "item-2"], _stub_client(monkeypatch))

    assert result["attributes"]["state"] == "WAITING_FOR_REVIEW"
    (call,) = asc.calls
    assert call["method"] == "PATCH"
    assert call["path"] == "/v1/reviewSubmissions/rs-1"
    assert call["retry_ambiguous"] is False
    payload = call["payload"]["data"]
    assert payload["attributes"] == {"submitted": True}
    assert payload["relationships"]["items"]["data"] == [
        {"type": "reviewSubmissionItems", "id": "item-1"},
        {"type": "reviewSubmissionItems", "id": "item-2"},
    ]


def test_mutating_requests_never_use_blind_retries_across_flow(monkeypatch):
    """Happy-path flow: every mutation asks for the no-ambiguous-retry mode."""
    builds = [_build("build-45", "45", "pre-1", "app-1")]
    included = [_pre_release("pre-1", "1.2.3")]
    localizations = [_localization("loc-ru", "ru")]
    phased = {"id": "ph-1", "type": "appStoreVersionPhasedReleases", "attributes": {"phasedReleaseState": "INACTIVE"}}
    item = {
        "id": "item-1",
        "type": "reviewSubmissionItems",
        "relationships": {"appStoreVersion": {"data": {"type": "appStoreVersions", "id": "v-1"}}},
    }
    asc = ScriptedAsc(
        monkeypatch,
        [
            _list_rsp([_app("app-1", "com.example.app")]),  # find app
            _list_rsp(builds, included=included),  # find build
            _list_rsp([]),  # version lookup
            {"data": _version("v-1", "1.2.3", state="PREPARE_FOR_SUBMISSION")},  # create version
            {"data": None},  # current build binding
            {},  # set build
            _list_rsp(localizations),  # localizations
            {},  # whatsNew patch
            {},  # release type patch
            _http_404("/v1/appStoreVersions/v-1/phasedRelease"),  # phased read
            {"data": phased},  # phased create
            _list_rsp([]),  # open submissions
            {"data": _submission("rs-1", "DRAFT")},  # create submission
            _list_rsp([]),  # submission items
            {"data": item},  # add item
            {"data": _submission("rs-1", "READY_FOR_REVIEW")},  # submit
        ],
    )
    client = _stub_client(monkeypatch)

    app_id = review.find_app_id("com.example.app", client)
    build = review.find_build(app_id, "1.2.3", "45", client)
    version = review.get_or_create_app_store_version(app_id, "1.2.3", client)
    assert review.get_version_build_id(version["id"], client) is None  # no binding yet
    review.set_version_build(version["id"], build["id"], client)
    review.set_whats_new(version["id"], {"ru": "Текст"}, client)
    review.set_release_type(version["id"], "manual", client)
    review.set_phased_release(version["id"], True, client)
    assert review.find_open_review_submission(app_id, client) is None  # no open submission yet
    submission = review.create_review_submission(app_id, version["id"], client)
    item_added = review.add_review_submission_item(submission["id"], version["id"], client)
    review.submit_review_submission(submission["id"], [item_added["id"]], client)

    mutations = asc.mutating()
    assert len(mutations) == 8  # version, build, whatsNew, releaseType, phased, submission, item, submit
    assert all(c["retry_ambiguous"] is False for c in mutations)
    gets = asc.calls_for("GET")
    assert len(gets) == 8
    assert all(c["retry_ambiguous"] is True for c in gets)  # reads keep the default safe mode
