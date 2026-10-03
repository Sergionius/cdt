"""Service-layer tests for appstore.update_metadata.

All scenarios run on the in-memory ASC double from tests/test_services_appstore_state.py:
no credentials, no network and no real Apple endpoints. Step-adapter and CLI-level
tests live in tests/test_steps_appstore.py and tests/test_agent_first.py.
"""

from __future__ import annotations

import pytest

from cdt.services import appstore, appstore_metadata
from cdt.services import appstore_review as review
from cdt.services.appstore import AscAmbiguousResultError, AscHttpError
from cdt.services.appstore_metadata import (
    InvalidMetadataOptionError,
    UnverifiedMetadataError,
    validate_localizations,
    validate_version,
)
from tests.test_services_appstore_state import FakeAsc, _stub_client

BUNDLE_ID = "com.example.app"


def _client() -> appstore._AscClient:
    return appstore._AscClient({})


def _ambiguous(method: str, path: str) -> AscAmbiguousResultError:
    return AscAmbiguousResultError(
        f"{method} {path} response was lost",
        method=method,
        path=path,
        code=502,
        category="http_502",
        detail="bad gateway",
    )


# -- option validation ----------------------------------------------------------


@pytest.mark.parametrize(
    "version",
    ["", "   ", None, 5, True, ["1.2.3"]],
)
def test_validate_version_rejects_non_strings_and_empty(version):
    with pytest.raises(InvalidMetadataOptionError):
        validate_version(version)


@pytest.mark.parametrize(
    "raw,expected",
    [("1.2.3", "1.2.3"), (" 2.0.1 ", "2.0.1")],
)
def test_validate_version_strips_and_keeps_strings(raw, expected):
    assert validate_version(raw) == expected


@pytest.mark.parametrize(
    "localizations",
    [
        None,
        "ru",
        ["ru"],
        {},
        {"ru": {}},
        {"ru": "Текст"},
        {"ru": None},
        {"": {"description": "Текст"}},
        {"  ": {"description": "Текст"}},
        {None: {"description": "Текст"}},
        {"ru": {"unknown_field": "Текст"}},
        {"ru": {"promotionalText": "Текст"}},  # ASC attribute name, not the option name
        {"ru": {"description": 5}},
        {"ru": {"description": True}},
        {"ru": {"description": False}},
        {"ru": {"description": None}},
        {"ru": {"whats_new": ["Текст"]}},
    ],
)
def test_validate_localizations_rejects_invalid(localizations):
    with pytest.raises(InvalidMetadataOptionError):
        validate_localizations(localizations)


def test_validate_localizations_strips_locales_and_keeps_values_verbatim():
    validated = validate_localizations({"  ru  ": {"description": "  Описание  ", "whats_new": ""}})

    assert validated == {"ru": {"description": "  Описание  ", "whats_new": ""}}


# -- exact version resolution and editable state --------------------------------


def test_unknown_app_fails_without_mutations(monkeypatch):
    asc = FakeAsc(monkeypatch, bundle_id="com.other.app")
    _stub_client(monkeypatch)

    with pytest.raises(review.ResourceNotFoundError, match="com.example.app"):
        appstore_metadata.update_metadata(BUNDLE_ID, "1.2.3", {"ru": {"whats_new": "Текст"}}, _client())

    assert asc.mutating() == []


def test_unknown_version_fails_without_get_or_create_or_mutations(monkeypatch):
    asc = FakeAsc(monkeypatch)
    _stub_client(monkeypatch)
    asc.add_version(version_string="3.0.0")

    with pytest.raises(review.ResourceNotFoundError, match="not found.*1.2.3|1.2.3.*not found"):
        appstore_metadata.update_metadata(BUNDLE_ID, "1.2.3", {"ru": {"whats_new": "Текст"}}, _client())

    assert asc.mutating() == []
    assert all("appStoreVersions" not in call["path"] or call["method"] == "GET" for call in asc.calls)
    assert not any(call["method"] == "POST" and call["path"] == "/v1/appStoreVersions" for call in asc.calls)


def test_multiple_matching_versions_fail_without_mutations(monkeypatch):
    asc = FakeAsc(monkeypatch)
    _stub_client(monkeypatch)
    asc.add_version(version_id="v-1", version_string="1.2.3")
    asc.add_version(version_id="v-2", version_string="1.2.3")

    with pytest.raises(review.AmbiguousResourceError):
        appstore_metadata.update_metadata(BUNDLE_ID, "1.2.3", {"ru": {"whats_new": "Текст"}}, _client())

    assert asc.mutating() == []


@pytest.mark.parametrize("state", ["WAITING_FOR_REVIEW", "REJECTED", "RELEASED", "PENDING_DEVELOPER_RELEASE"])
def test_non_editable_version_state_is_rejected_without_mutations(monkeypatch, state):
    asc = FakeAsc(monkeypatch)
    _stub_client(monkeypatch)
    asc.add_version(state=state)

    with pytest.raises(review.VersionStateError, match="PREPARE_FOR_SUBMISSION"):
        appstore_metadata.update_metadata(BUNDLE_ID, "1.2.3", {"ru": {"whats_new": "Текст"}}, _client())

    assert asc.mutating() == []


def test_uploaded_build_is_never_used_and_no_build_is_selected(monkeypatch):
    asc = FakeAsc(monkeypatch)
    _stub_client(monkeypatch)
    version = asc.add_version()

    outcome = appstore_metadata.update_metadata(BUNDLE_ID, "1.2.3", {"ru": {"whats_new": "Текст"}}, _client())

    assert outcome.version_id == "v-1"
    assert not any(call["path"] == "/v1/builds" for call in asc.calls)
    assert version["relationships"]["build"]["data"] is None


# -- pre-mutation validation of the whole request --------------------------------


def test_unknown_locale_stops_before_the_first_mutation(monkeypatch):
    asc = FakeAsc(monkeypatch)
    _stub_client(monkeypatch)
    asc.add_version(localizations=("en-US",))

    with pytest.raises(review.UnknownLocaleError, match="ru"):
        appstore_metadata.update_metadata(
            BUNDLE_ID,
            "1.2.3",
            {"en-US": {"whats_new": "News"}, "ru": {"whats_new": "Текст"}},
            _client(),
        )

    assert asc.mutating() == []


def test_localizations_must_not_be_empty(monkeypatch):
    with pytest.raises(InvalidMetadataOptionError):
        appstore_metadata.update_localizations("v-1", {}, _client())


# -- minimal patches, field mapping and ordering ---------------------------------


def test_fields_are_mapped_to_asc_attributes(monkeypatch):
    asc = FakeAsc(monkeypatch)
    _stub_client(monkeypatch)
    asc.add_version()

    appstore_metadata.update_metadata(
        BUNDLE_ID,
        "1.2.3",
        {"ru": {"description": "Описание", "keywords": "ключ,слово", "promotional_text": "Промо"}},
        _client(),
    )

    patch = next(call for call in asc.mutating() if call["method"] == "PATCH")
    assert patch["payload"]["data"]["attributes"] == {
        "description": "Описание",
        "keywords": "ключ,слово",
        "promotionalText": "Промо",
    }


def test_patch_contains_only_differing_requested_fields(monkeypatch):
    asc = FakeAsc(monkeypatch)
    _stub_client(monkeypatch)
    version = asc.add_version()
    ru = next(loc for loc in asc.localizations[version["id"]] if loc["attributes"]["locale"] == "ru")
    ru["attributes"]["description"] = "Уже сохранено"

    appstore_metadata.update_metadata(
        BUNDLE_ID,
        "1.2.3",
        {"ru": {"description": "Уже сохранено", "whats_new": "Новости"}},
        _client(),
    )

    patch = next(call for call in asc.mutating() if call["method"] == "PATCH")
    assert patch["payload"]["data"]["attributes"] == {"whatsNew": "Новости"}


def test_locales_are_processed_in_stable_sorted_order(monkeypatch):
    asc = FakeAsc(monkeypatch)
    _stub_client(monkeypatch)
    version = asc.add_version()

    appstore_metadata.update_metadata(
        BUNDLE_ID,
        "1.2.3",
        {"ru": {"whats_new": "Текст"}, "en-US": {"whats_new": "Text"}},
        _client(),
    )

    locale_by_id = {loc["id"]: loc["attributes"]["locale"] for loc in asc.localizations[version["id"]]}
    patched_locales = [
        locale_by_id[call["payload"]["data"]["id"]] for call in asc.mutating() if call["method"] == "PATCH"
    ]
    assert patched_locales == ["en-US", "ru"]


def test_every_patch_disables_ambiguous_retries(monkeypatch):
    asc = FakeAsc(monkeypatch)
    _stub_client(monkeypatch)
    asc.add_version()

    appstore_metadata.update_metadata(BUNDLE_ID, "1.2.3", {"ru": {"whats_new": "Текст"}}, _client())

    patches = [call for call in asc.mutating() if call["method"] == "PATCH"]
    assert patches
    assert all(call["retry_ambiguous"] is False for call in patches)


def test_empty_string_clears_a_field_explicitly(monkeypatch):
    asc = FakeAsc(monkeypatch)
    _stub_client(monkeypatch)
    version = asc.add_version()
    ru = next(loc for loc in asc.localizations[version["id"]] if loc["attributes"]["locale"] == "ru")
    ru["attributes"]["promotionalText"] = "Старый промо-текст"

    appstore_metadata.update_metadata(BUNDLE_ID, "1.2.3", {"ru": {"promotional_text": ""}}, _client())

    patch = next(call for call in asc.mutating() if call["method"] == "PATCH")
    assert patch["payload"]["data"]["attributes"] == {"promotionalText": ""}
    assert ru["attributes"]["promotionalText"] == ""


# -- reruns read first and skip matching fields ----------------------------------


def test_rerun_reads_state_and_skips_matching_fields(monkeypatch):
    asc = FakeAsc(monkeypatch)
    _stub_client(monkeypatch)
    asc.add_version()
    request = {"ru": {"whats_new": "Текст"}, "en-US": {"description": "Description"}}

    first = appstore_metadata.update_metadata(BUNDLE_ID, "1.2.3", request, _client())
    second = appstore_metadata.update_metadata(BUNDLE_ID, "1.2.3", request, _client())

    assert first.updated == {"en-US": ["description"], "ru": ["whats_new"]}
    assert first.unchanged == []
    assert second.updated == {}
    assert second.unchanged == ["en-US", "ru"]
    assert len([call for call in asc.mutating() if call["method"] == "PATCH"]) == 2


# -- read-back verification and ambiguous results --------------------------------


def test_ambiguous_patch_is_accepted_after_successful_read_back(monkeypatch):
    asc = FakeAsc(monkeypatch)
    _stub_client(monkeypatch)
    asc.add_version(localizations=("ru",))
    asc.fail_on("PATCH", "/v1/appStoreVersionLocalizations", _ambiguous("PATCH", "loc"), apply=True)

    outcome = appstore_metadata.update_metadata(BUNDLE_ID, "1.2.3", {"ru": {"whats_new": "Текст"}}, _client())

    assert outcome.updated == {"ru": ["whats_new"]}
    # Exactly one PATCH: the ambiguity was resolved by reading, not by repeating.
    assert len([call for call in asc.mutating() if call["method"] == "PATCH"]) == 1


def test_ambiguous_patch_without_effect_fails_without_repeating(monkeypatch):
    asc = FakeAsc(monkeypatch)
    _stub_client(monkeypatch)
    asc.add_version(localizations=("ru",))
    asc.fail_on("PATCH", "/v1/appStoreVersionLocalizations", _ambiguous("PATCH", "loc"), apply=False)

    with pytest.raises(UnverifiedMetadataError, match="ru"):
        appstore_metadata.update_metadata(BUNDLE_ID, "1.2.3", {"ru": {"whats_new": "Текст"}}, _client())

    assert len([call for call in asc.mutating() if call["method"] == "PATCH"]) == 1
    ru = next(loc for loc in asc.localizations["v-1"] if loc["attributes"]["locale"] == "ru")
    assert ru["attributes"]["whatsNew"] is None


def test_read_back_mismatch_after_accepted_patch_fails_without_repeating(monkeypatch):
    asc = FakeAsc(monkeypatch)
    _stub_client(monkeypatch)
    asc.add_version(localizations=("ru",))

    original = review.get_version_localizations
    reads = {"count": 0}

    def stale_read_back(version_id, client):
        reads["count"] += 1
        resources = original(version_id, client)
        if reads["count"] >= 2:  # the read-back after the PATCH, not the initial state read
            for loc in resources:
                if loc["attributes"].get("locale") == "ru":
                    loc["attributes"]["whatsNew"] = "Apple stored something else"
        return resources

    monkeypatch.setattr(review, "get_version_localizations", stale_read_back)

    with pytest.raises(UnverifiedMetadataError, match="unverified"):
        appstore_metadata.update_metadata(BUNDLE_ID, "1.2.3", {"ru": {"whats_new": "Текст"}}, _client())

    assert len([call for call in asc.mutating() if call["method"] == "PATCH"]) == 1


def test_partial_success_keeps_completed_locales_without_rollback(monkeypatch):
    asc = FakeAsc(monkeypatch)
    _stub_client(monkeypatch)
    version = asc.add_version()
    ru = next(loc for loc in asc.localizations[version["id"]] if loc["attributes"]["locale"] == "ru")
    asc.fail_on(
        "PATCH",
        "/v1/appStoreVersionLocalizations/loc-v-1-ru",
        AscHttpError("uneditable", code=409, method="PATCH", path="loc", body="uneditable"),
        apply=False,
    )

    with pytest.raises(AscHttpError):
        appstore_metadata.update_metadata(
            BUNDLE_ID,
            "1.2.3",
            {"en-US": {"whats_new": "Text"}, "ru": {"whats_new": "Текст"}},
            _client(),
        )

    # en-US (processed first) kept its confirmed change; nothing was rolled back.
    en = next(loc for loc in asc.localizations[version["id"]] if loc["attributes"]["locale"] == "en-US")
    assert en["attributes"]["whatsNew"] == "Text"
    assert ru["attributes"]["whatsNew"] is None


# -- safe outcome summary ---------------------------------------------------------


def test_outcome_reports_identifiers_and_locale_names_without_texts(monkeypatch):
    asc = FakeAsc(monkeypatch)
    _stub_client(monkeypatch)
    version = asc.add_version()
    en = next(loc for loc in asc.localizations[version["id"]] if loc["attributes"]["locale"] == "en-US")
    en["attributes"]["description"] = "Unchanged"

    outcome = appstore_metadata.update_metadata(
        BUNDLE_ID,
        "1.2.3",
        {"ru": {"whats_new": "Секретный текст"}, "en-US": {"description": "Unchanged"}},
        _client(),
    )

    assert isinstance(outcome, appstore_metadata.MetadataOutcome)
    assert outcome.bundle_id == BUNDLE_ID
    assert outcome.version_id == "v-1"
    assert outcome.version_string == "1.2.3"
    assert outcome.app_store_state == "PREPARE_FOR_SUBMISSION"
    assert outcome.updated == {"ru": ["whats_new"]}
    assert outcome.unchanged == ["en-US"]
    # The outcome carries field names and locale names only, never metadata texts.
    rendered = repr(outcome) + str(outcome)
    assert "Секретный текст" not in rendered
    assert "Unchanged" not in rendered
