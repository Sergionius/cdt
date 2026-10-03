"""Safe App Store Connect queries and App Review operations.

Read-only and mutating primitives needed to submit an already-uploaded build
for App Review: exact lookups of app / iOS App Store version / build with
pagination, validation of the build for review, version read-or-create, build
binding, ``whatsNew`` localizations, release type, phased release and the
current ``reviewSubmissions`` / ``reviewSubmissionItems`` flow.

Every mutating request is issued through
:func:`cdt.services.appstore._asc_request` with ``retry_ambiguous=False`` so a
lost response (transport failure or HTTP 5xx) surfaces as
:class:`cdt.services.appstore.AscAmbiguousResultError` instead of a blind
retry that could submit a version for review twice. Where a lost response can
be reconciled by a safe read-only lookup (create version, add submission item)
the primitive performs that single lookup itself; everything else is left to
the resumable operation layer.

Field names, state values and allowed transitions follow the official Apple
App Store Connect API documentation for ``apps``, ``builds``,
``appStoreVersions``, ``appStoreVersionLocalizations``,
``appStoreVersionPhasedReleases``, ``reviewSubmissions`` and
``reviewSubmissionItems``. The deprecated ``appStoreVersionSubmissions``
flow is intentionally not used.
"""

import urllib.parse

from . import appstore
from .appstore import _AscClient

ASC_MAX_PAGES = 20

ASC_PLATFORM_IOS = "IOS"

BUILD_STATE_VALID = "VALID"

# appStoreVersions.appStoreState value that allows editing / preparing a submission.
VERSION_STATE_PREPARE_FOR_SUBMISSION = "PREPARE_FOR_SUBMISSION"

# releaseType values for manual release and automatic release after approval.
RELEASE_TYPE_MANUAL = "MANUAL"
RELEASE_TYPE_AUTOMATIC = "AUTOMATIC"
RELEASE_MODES = {"manual": RELEASE_TYPE_MANUAL, "automatic": RELEASE_TYPE_AUTOMATIC}

# DRAFT is retained only as a legacy unsubmitted value; ASC uses READY_FOR_REVIEW.
REVIEW_SUBMISSION_STATE_DRAFT = "DRAFT"
REVIEW_SUBMISSION_STATE_READY_FOR_REVIEW = "READY_FOR_REVIEW"
REVIEW_SUBMISSION_STATE_WAITING_FOR_REVIEW = "WAITING_FOR_REVIEW"
REVIEW_SUBMISSION_STATE_IN_REVIEW = "IN_REVIEW"
OPEN_REVIEW_SUBMISSION_STATES = (
    REVIEW_SUBMISSION_STATE_DRAFT,
    REVIEW_SUBMISSION_STATE_READY_FOR_REVIEW,
    REVIEW_SUBMISSION_STATE_WAITING_FOR_REVIEW,
    REVIEW_SUBMISSION_STATE_IN_REVIEW,
    "UNRESOLVED_ISSUES",
    "CANCELING",
    "COMPLETING",
)

# appStoreVersionPhasedReleases.phasedReleaseState: enabled but not started yet.
PHASED_RELEASE_STATE_INACTIVE = "INACTIVE"

# Only documented submitted states prove that Apple accepted the request.
# Unknown values must never be interpreted as success.
UNSUBMITTED_REVIEW_SUBMISSION_STATES = (
    REVIEW_SUBMISSION_STATE_DRAFT,
    REVIEW_SUBMISSION_STATE_READY_FOR_REVIEW,
)


class AppStoreReviewError(Exception):
    """Base class for App Store review preparation failures."""


class ResourceNotFoundError(AppStoreReviewError):
    """An exactly-identified resource does not exist in App Store Connect."""


class AmbiguousResourceError(AppStoreReviewError):
    """A lookup matched more than one resource; refusing to guess."""


class InvalidBuildError(AppStoreReviewError):
    """The build exists but cannot be used for an App Review submission."""


class UnknownLocaleError(AppStoreReviewError):
    """A requested ``whatsNew`` locale has no existing version localization."""


class VersionStateError(AppStoreReviewError):
    """The App Store version is in a state that must not be prepared or resubmitted."""


class BuildConflictError(AppStoreReviewError):
    """The version already has a different build selected; CDT does not overwrite it."""


class SubmissionConflictError(AppStoreReviewError):
    """A review submission contains foreign items; CDT does not touch it."""


def _attributes(resource: dict) -> dict:
    return resource.get("attributes") or {}


def _related_id(resource: dict, relationship: str) -> str | None:
    data = (resource.get("relationships") or {}).get(relationship, {}).get("data")
    if isinstance(data, dict):
        return data.get("id")
    return None


def _quote(value: str) -> str:
    return urllib.parse.quote(str(value), safe="")


def version_state(version: dict) -> str | None:
    """Return the ``appStoreState`` of an appStoreVersions resource, or None."""
    return _attributes(version).get("appStoreState")


def version_release_type(version: dict) -> str | None:
    """Return the ``releaseType`` of an appStoreVersions resource, or None."""
    return _attributes(version).get("releaseType")


def version_string(version: dict) -> str | None:
    return _attributes(version).get("versionString")


def version_platform(version: dict) -> str | None:
    return _attributes(version).get("platform")


def ensure_version_editable(version: dict) -> str:
    """Allow new submission preparation only for an editable version.

    Returns the observed ``appStoreState`` when it is
    ``PREPARE_FOR_SUBMISSION``. Rejected, already-submitted, awaiting-release
    and unknown states raise :class:`VersionStateError` with an explanation:
    CDT never resubmits or overwrites such versions automatically.
    """
    state = version_state(version)
    if state == VERSION_STATE_PREPARE_FOR_SUBMISSION:
        return state
    if not state:
        raise VersionStateError(
            f"App Store version {version.get('id')} reports no appStoreState; CDT prepares submissions "
            "only for versions in PREPARE_FOR_SUBMISSION - resolve the version in App Store Connect"
        )
    raise VersionStateError(
        f"App Store version {version.get('id')} is in state {state}; new submission preparation requires "
        "PREPARE_FOR_SUBMISSION. Rejected, blocked or already-submitted versions must be resolved in "
        "App Store Connect - CDT does not resubmit or overwrite them automatically"
    )


def get_app_store_version(version_id: str, client: _AscClient) -> dict | None:
    """Return one appStoreVersions resource by id, or None when it is gone."""
    try:
        rsp = appstore._asc_request("GET", f"/v1/appStoreVersions/{_quote(version_id)}", client)
    except appstore.AscHttpError as exc:
        if exc.code == 404:
            return None
        raise
    data = rsp.get("data")
    return data if isinstance(data, dict) else None


def get_review_submission(submission_id: str, client: _AscClient) -> dict | None:
    """Return one reviewSubmissions resource by id, or None when it is gone."""
    try:
        rsp = appstore._asc_request(
            "GET", f"/v1/reviewSubmissions/{_quote(submission_id)}?include=appStoreVersionForReview", client
        )
    except appstore.AscHttpError as exc:
        if exc.code == 404:
            return None
        raise
    data = rsp.get("data")
    return data if isinstance(data, dict) else None


def get_whats_new(version_id: str, client: _AscClient) -> dict[str, str]:
    """Return the current ``whatsNew`` text per existing version localization."""
    texts: dict[str, str] = {}
    for loc in get_version_localizations(version_id, client):
        locale = _attributes(loc).get("locale")
        if isinstance(locale, str):
            value = _attributes(loc).get("whatsNew")
            texts[locale] = value if isinstance(value, str) else ""
    return texts


def submission_state(submission: dict) -> str | None:
    return _attributes(submission).get("state")


def submission_version_id(submission: dict) -> str | None:
    """Return the version the submission was created for, when set."""
    return _related_id(submission, "appStoreVersionForReview")


def item_version_id(item: dict) -> str | None:
    return _related_id(item, "appStoreVersion")


def submission_is_submitted(state: str | None) -> bool:
    """True when the submission left the unsubmitted stages (DRAFT / READY_FOR_REVIEW).

    Only such a state proves Apple accepted the submit request; a draft alone
    is never a success.
    """
    return state in {"WAITING_FOR_REVIEW", "IN_REVIEW", "COMPLETING", "COMPLETE"}


def _asc_list_responses(path: str, client: _AscClient) -> list[dict]:
    """GET every page of an ASC list endpoint, following ``links.next``."""
    responses: list[dict] = []
    current = path
    for _ in range(ASC_MAX_PAGES):
        rsp = appstore._asc_request("GET", current, client)
        responses.append(rsp)
        nxt = (rsp.get("links") or {}).get("next")
        if not nxt:
            return responses
        if nxt.startswith(appstore.ASC_API_BASE):
            current = nxt[len(appstore.ASC_API_BASE) :]
        elif nxt.startswith("/"):
            current = nxt
        else:
            raise AppStoreReviewError(f"Unexpected App Store Connect pagination URL: {nxt}")
    raise AppStoreReviewError(f"App Store Connect pagination exceeded {ASC_MAX_PAGES} pages for {path}")


def _asc_list(path: str, client: _AscClient) -> list[dict]:
    items: list[dict] = []
    for rsp in _asc_list_responses(path, client):
        items.extend(rsp.get("data") or [])
    return items


def find_app_id(bundle_id: str, client: _AscClient) -> str:
    """Return the exact App Store Connect app id for *bundle_id*.

    Verifies the bundle id server result client-side (ASC filters can be
    looser than exact equality), follows pagination and rejects multiple
    exact matches instead of picking one arbitrarily.
    """
    items = _asc_list(f"/v1/apps?filter[bundleId]={_quote(bundle_id)}", client)
    matches = [item for item in items if _attributes(item).get("bundleId") == bundle_id]
    if not matches:
        raise ResourceNotFoundError(f"App not found in App Store Connect for bundle id: {bundle_id}")
    if len(matches) > 1:
        raise AmbiguousResourceError(
            f"Bundle id {bundle_id} matches {len(matches)} apps in App Store Connect; "
            "resolve the duplication before submitting for review"
        )
    return matches[0]["id"]


def validate_build_for_review(app_id: str, build: dict) -> None:
    """Reject a build that must not be attached and submitted for review.

    Checks that the build belongs to *app_id*, is processed to ``VALID``,
    has not expired and belongs to the iOS platform (via its
    ``preReleaseVersion``).
    """
    if _related_id(build, "app") != app_id:
        raise InvalidBuildError(f"Build {build.get('id')} belongs to a different app; refusing to use it")

    attrs = _attributes(build)
    state = attrs.get("processingState")
    if attrs.get("expired"):
        raise InvalidBuildError(f"Build {attrs.get('version')} has expired and cannot be submitted for review")
    if state != BUILD_STATE_VALID:
        raise InvalidBuildError(
            f"Build {attrs.get('version')} is not processed (processingState={state}); "
            "wait for TestFlight processing to finish"
        )


def find_build(app_id: str, marketing_version: str, build_number: str, client: _AscClient) -> dict:
    """Return the exact processed iOS build for app + marketing version + build number.

    Uses server-side filters, follows pagination and matches the build number,
    the ``preReleaseVersion`` (marketing version + iOS platform) client-side.
    Raises :class:`ResourceNotFoundError` when nothing matches and
    :class:`AmbiguousResourceError` when several builds match, so callers
    never fall back to an arbitrary latest Apple build.
    """
    path = (
        "/v1/builds"
        f"?filter[app]={_quote(app_id)}"
        f"&filter[version]={_quote(build_number)}"
        f"&filter[preReleaseVersion.version]={_quote(marketing_version)}"
        "&include=preReleaseVersion,app"
    )
    candidates: list[dict] = []
    for rsp in _asc_list_responses(path, client):
        pre_by_id = {
            included["id"]: included
            for included in rsp.get("included") or []
            if included.get("type") == "preReleaseVersions"
        }
        for item in rsp.get("data") or []:
            if str(_attributes(item).get("version")) != str(build_number):
                continue
            pre = pre_by_id.get(_related_id(item, "preReleaseVersion"))
            pre_attrs = _attributes(pre or {})
            if str(pre_attrs.get("version") or "") != str(marketing_version):
                continue
            if str(pre_attrs.get("platform") or "").upper() != ASC_PLATFORM_IOS:
                continue
            candidates.append(item)

    target = f"version {marketing_version} ({build_number})"
    if not candidates:
        raise ResourceNotFoundError(f"Build for {target} not found in App Store Connect for app {app_id}")
    if len(candidates) > 1:
        raise AmbiguousResourceError(
            f"{len(candidates)} iOS builds for {target} found in App Store Connect for app {app_id}; "
            "cannot pick one automatically"
        )

    build = candidates[0]
    validate_build_for_review(app_id, build)
    return build


def find_app_store_version(
    app_id: str,
    version_string: str,
    client: _AscClient,
    platform: str = ASC_PLATFORM_IOS,
) -> dict | None:
    """Return the exact iOS App Store version for *version_string*, or None."""
    path = (
        f"/v1/apps/{_quote(app_id)}/appStoreVersions"
        f"?filter[platform]={_quote(platform)}&filter[versionString]={_quote(version_string)}"
    )
    items = _asc_list(path, client)
    matches = [
        item
        for item in items
        if str(_attributes(item).get("versionString") or "") == version_string
        and str(_attributes(item).get("platform") or "").upper() == platform.upper()
    ]
    if len(matches) > 1:
        raise AmbiguousResourceError(
            f"{len(matches)} {platform} App Store versions {version_string} found for app {app_id}; "
            "cannot pick one automatically"
        )
    return matches[0] if matches else None


def get_or_create_app_store_version(
    app_id: str,
    version_string: str,
    client: _AscClient,
    platform: str = ASC_PLATFORM_IOS,
) -> dict:
    """Return the existing iOS App Store version, creating it when absent.

    A new version starts in ``PREPARE_FOR_SUBMISSION``. If the creation
    response is lost (ambiguous transport error or 5xx), the version is
    re-read once; the creation is only repeated implicitly through that
    read, never by a blind retry.
    """
    existing = find_app_store_version(app_id, version_string, client, platform)
    if existing is not None:
        return existing

    payload = {
        "data": {
            "type": "appStoreVersions",
            "attributes": {"platform": platform, "versionString": version_string},
            "relationships": {"app": {"data": {"type": "apps", "id": app_id}}},
        }
    }
    try:
        rsp = appstore._asc_request(
            "POST", "/v1/appStoreVersions", client, payload, retry_ambiguous=False
        )
    except appstore.AscAmbiguousResultError:
        # The version may have been created; confirm via a safe read-only lookup.
        existing = find_app_store_version(app_id, version_string, client, platform)
        if existing is not None:
            return existing
        raise
    return rsp["data"]


def get_version_build_id(version_id: str, client: _AscClient) -> str | None:
    """Return the id of the build currently bound to the version, or None."""
    try:
        rsp = appstore._asc_request("GET", f"/v1/appStoreVersions/{version_id}/build", client)
    except appstore.AscHttpError as exc:
        if exc.code == 404:
            return None
        raise
    data = rsp.get("data")
    if isinstance(data, dict):
        return data.get("id")
    return None


def set_version_build(version_id: str, build_id: str, client: _AscClient) -> None:
    """Bind *build_id* to the App Store version (overwrites any current binding)."""
    payload = {
        "data": {
            "type": "appStoreVersions",
            "id": version_id,
            "relationships": {"build": {"data": {"type": "builds", "id": build_id}}},
        }
    }
    appstore._asc_request(
        "PATCH", f"/v1/appStoreVersions/{version_id}", client, payload, retry_ambiguous=False
    )


def get_version_localizations(version_id: str, client: _AscClient) -> list[dict]:
    """Return all existing App Store version localizations (paginated)."""
    return _asc_list(f"/v1/appStoreVersions/{version_id}/appStoreVersionLocalizations", client)


def set_whats_new(version_id: str, whats_new: dict[str, str], client: _AscClient) -> list[str]:
    """Set ``whatsNew`` on existing version localizations only.

    Every requested locale must already exist as a version localization;
    unknown locales raise :class:`UnknownLocaleError` explaining that the
    localization must be prepared in App Store Connect first — CDT never
    creates a partial app card. All locales are validated before the first
    write, and only the passed locales are modified.
    """
    if not whats_new:
        raise AppStoreReviewError("whats_new must contain at least one locale")

    existing_ids = {
        str(_attributes(loc).get("locale")): loc["id"]
        for loc in get_version_localizations(version_id, client)
    }
    unknown = [locale for locale in whats_new if locale not in existing_ids]
    if unknown:
        raise UnknownLocaleError(
            "No App Store version localization for locale(s): "
            + ", ".join(sorted(unknown))
            + f". Prepare them in App Store Connect for version {version_id} first; "
            "CDT does not create partial app cards"
        )

    for locale, text in whats_new.items():
        loc_id = existing_ids[locale]
        payload = {
            "data": {
                "type": "appStoreVersionLocalizations",
                "id": loc_id,
                "attributes": {"whatsNew": text},
            }
        }
        appstore._asc_request(
            "PATCH",
            f"/v1/appStoreVersionLocalizations/{loc_id}",
            client,
            payload,
            retry_ambiguous=False,
        )
    return sorted(whats_new)


def set_release_type(version_id: str, release_mode: str, client: _AscClient) -> str:
    """Map ``manual``/``automatic`` to the ASC ``releaseType`` and apply it."""
    try:
        release_type = RELEASE_MODES[release_mode]
    except KeyError:
        raise AppStoreReviewError(
            f"Unknown release mode {release_mode!r}; expected 'manual' or 'automatic'"
        ) from None
    payload = {
        "data": {
            "type": "appStoreVersions",
            "id": version_id,
            "attributes": {"releaseType": release_type},
        }
    }
    appstore._asc_request(
        "PATCH", f"/v1/appStoreVersions/{version_id}", client, payload, retry_ambiguous=False
    )
    return release_type


def get_phased_release(version_id: str, client: _AscClient) -> dict | None:
    """Return the version's phased release resource, or None when absent."""
    try:
        rsp = appstore._asc_request("GET", f"/v1/appStoreVersions/{version_id}/appStoreVersionPhasedRelease", client)
    except appstore.AscHttpError as exc:
        if exc.code == 404:
            return None
        raise
    data = rsp.get("data")
    return data if isinstance(data, dict) else None


def set_phased_release(version_id: str, enabled: bool, client: _AscClient) -> str | None:
    """Ensure the seven-day phased release is enabled or absent for the version.

    Returns the resulting ``phasedReleaseState`` (None when disabled). When
    enabling and the creation response is lost, the resource is re-read once;
    the creation is never blindly retried.
    """
    current = get_phased_release(version_id, client)
    if enabled:
        if current is not None:
            return _attributes(current).get("phasedReleaseState")
        try:
            rsp = appstore._asc_request(
                "POST",
                "/v1/appStoreVersionPhasedReleases",
                client,
                {
                    "data": {
                        "type": "appStoreVersionPhasedReleases",
                        "attributes": {"phasedReleaseState": PHASED_RELEASE_STATE_INACTIVE},
                        "relationships": {
                            "appStoreVersion": {"data": {"type": "appStoreVersions", "id": version_id}}
                        },
                    }
                },
                retry_ambiguous=False,
            )
        except appstore.AscAmbiguousResultError:
            # Confirm via a safe read-only lookup instead of repeating the POST.
            current = get_phased_release(version_id, client)
            if current is not None:
                return _attributes(current).get("phasedReleaseState")
            raise
        return _attributes(rsp["data"]).get("phasedReleaseState")

    if current is None:
        return None
    appstore._asc_request(
        "DELETE", f"/v1/appStoreVersionPhasedReleases/{current['id']}", client, retry_ambiguous=False
    )
    return None


def find_open_review_submission(
    app_id: str,
    client: _AscClient,
    platform: str = ASC_PLATFORM_IOS,
) -> dict | None:
    """Return the single open review submission for the app, or None.

    More than one open submission is an ambiguity that must be resolved in
    App Store Connect; CDT never picks one automatically.
    """
    path = (
        f"/v1/reviewSubmissions?filter[app]={_quote(app_id)}&filter[platform]={_quote(platform)}"
        "&include=appStoreVersionForReview"
    )
    matches = [
        item
        for item in _asc_list(path, client)
        if _attributes(item).get("state") != "COMPLETE"
        and str(_attributes(item).get("platform") or "").upper() == platform.upper()
    ]
    if len(matches) > 1:
        states = sorted(_attributes(item).get("state", "?") for item in matches)
        raise AmbiguousResourceError(
            f"{len(matches)} open review submissions found for app {app_id} (states: {', '.join(states)}); "
            "resolve them in App Store Connect before submitting"
        )
    return matches[0] if matches else None


def create_review_submission(
    app_id: str,
    client: _AscClient,
    platform: str = ASC_PLATFORM_IOS,
) -> dict:
    """Create an app-level draft; bind the version through a submission item."""
    payload = {
        "data": {
            "type": "reviewSubmissions",
            "attributes": {"platform": platform},
            "relationships": {
                "app": {"data": {"type": "apps", "id": app_id}},
            },
        }
    }
    rsp = appstore._asc_request(
        "POST", "/v1/reviewSubmissions", client, payload, retry_ambiguous=False
    )
    return rsp["data"]


def get_review_submission_items(submission_id: str, client: _AscClient) -> list[dict]:
    """Return the items of a review submission (paginated)."""
    return _asc_list(f"/v1/reviewSubmissions/{submission_id}/items?include=appStoreVersion", client)


def find_version_item(items: list[dict], version_id: str) -> dict | None:
    """Return the submission item bound to *version_id*, or None."""
    for item in items:
        if _related_id(item, "appStoreVersion") == version_id:
            return item
    return None


def add_review_submission_item(submission_id: str, version_id: str, client: _AscClient) -> dict:
    """Add the version to the draft submission, reusing an existing item.

    A lost creation response is reconciled by re-reading the items once; the
    POST is never blindly retried.
    """
    existing = find_version_item(get_review_submission_items(submission_id, client), version_id)
    if existing is not None:
        return existing

    payload = {
        "data": {
            "type": "reviewSubmissionItems",
            "relationships": {
                "reviewSubmission": {"data": {"type": "reviewSubmissions", "id": submission_id}},
                "appStoreVersion": {"data": {"type": "appStoreVersions", "id": version_id}},
            },
        }
    }
    try:
        rsp = appstore._asc_request(
            "POST", "/v1/reviewSubmissionItems", client, payload, retry_ambiguous=False
        )
    except appstore.AscAmbiguousResultError:
        # Confirm via a safe read-only lookup instead of repeating the POST.
        item = find_version_item(get_review_submission_items(submission_id, client), version_id)
        if item is not None:
            return item
        raise
    return rsp["data"]


def submit_review_submission(submission_id: str, client: _AscClient) -> dict:
    """Submit the review submission whose items were verified by the caller.

    Losing the submit response is genuinely ambiguous (Apple may have accepted
    the submission), so it always raises
    :class:`cdt.services.appstore.AscAmbiguousResultError` and must be
    reconciled by reading the submission state — never by repeating the PATCH.
    """
    payload = {
        "data": {
            "type": "reviewSubmissions",
            "id": submission_id,
            "attributes": {"submitted": True},
        }
    }
    rsp = appstore._asc_request(
        "PATCH", f"/v1/reviewSubmissions/{submission_id}", client, payload, retry_ambiguous=False
    )
    return rsp["data"]
