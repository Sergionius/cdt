"""Localized App Store metadata text updates for an existing iOS version.

Read-and-update primitives for ``appstore.update_metadata``: they change only
``description``, ``keywords``, ``promotionalText`` and ``whatsNew`` of existing
``appStoreVersionLocalizations`` of an exactly identified, still editable iOS
App Store version. Nothing is created (no version, no localization, no review
submission), no build is selected and nothing is sent for review.

The primitives reuse the shared ASC client (:class:`cdt.services.appstore._AscClient`),
the exact paginated lookups from :mod:`cdt.services.appstore_review` and its
locale/state validation. Every mutation is issued through
:func:`cdt.services.appstore._asc_request` with ``retry_ambiguous=False`` and is
verified by a read-back; a lost or unverifiable PATCH result fails the update
without repeating the PATCH in the same run.
"""

from dataclasses import dataclass, field

from . import appstore
from . import appstore_review as review
from .appstore import _AscClient
from .appstore_review import ResourceNotFoundError


class MetadataUpdateError(Exception):
    """Base class for App Store metadata update failures."""


class InvalidMetadataOptionError(MetadataUpdateError):
    """The requested version or localizations mapping is not usable."""


class UnverifiedMetadataError(MetadataUpdateError):
    """A metadata PATCH outcome could not be verified by reading it back."""


# YAML option field names -> ASC appStoreVersionLocalizations attribute names.
# These four fields are the complete supported set.
METADATA_FIELDS: dict[str, str] = {
    "description": "description",
    "keywords": "keywords",
    "promotional_text": "promotionalText",
    "whats_new": "whatsNew",
}


@dataclass(frozen=True)
class MetadataOutcome:
    """Safe summary of one metadata update: identifiers and locale names only.

    It never contains credentials or the metadata texts themselves.
    """

    bundle_id: str
    version_id: str
    version_string: str
    app_store_state: str
    updated: dict[str, list[str]] = field(default_factory=dict)
    unchanged: list[str] = field(default_factory=list)


def validate_version(version: object) -> str:
    """Require a non-empty version string; no implicit conversions."""
    if not isinstance(version, str) or not version.strip():
        raise InvalidMetadataOptionError(
            f"appstore.update_metadata option 'version' must be a non-empty string, got {version!r}"
        )
    return version.strip()


def validate_localizations(localizations: object) -> dict[str, dict[str, str]]:
    """Validate the ``localizations`` option without mutating the texts.

    Requires a non-empty mapping from non-empty locale names to non-empty
    mappings of known field names to string values. Numbers, booleans and
    ``None`` are rejected instead of being converted to text; an empty string
    is kept as an explicit clear request.
    """
    if not isinstance(localizations, dict):
        raise InvalidMetadataOptionError(
            "appstore.update_metadata option 'localizations' must map locales to field/text mappings, "
            f"got {type(localizations).__name__}"
        )
    if not localizations:
        raise InvalidMetadataOptionError(
            "appstore.update_metadata option 'localizations' must not be empty; provide at least one locale"
        )
    validated: dict[str, dict[str, str]] = {}
    for locale, fields in localizations.items():
        if not isinstance(locale, str) or not locale.strip():
            raise InvalidMetadataOptionError(
                f"appstore.update_metadata option 'localizations' has an empty or non-string locale: {locale!r}"
            )
        if not isinstance(fields, dict):
            raise InvalidMetadataOptionError(
                f"appstore.update_metadata localizations[{locale!r}] must map field names to strings, "
                f"got {type(fields).__name__}"
            )
        if not fields:
            raise InvalidMetadataOptionError(
                f"appstore.update_metadata localizations[{locale!r}] must contain at least one field; "
                f"supported fields: {', '.join(sorted(METADATA_FIELDS))}"
            )
        clean: dict[str, str] = {}
        for name, value in fields.items():
            if name not in METADATA_FIELDS:
                raise InvalidMetadataOptionError(
                    f"appstore.update_metadata localizations[{locale!r}] has unknown field {name!r}; "
                    f"supported fields: {', '.join(sorted(METADATA_FIELDS))}"
                )
            if not isinstance(value, str):
                raise InvalidMetadataOptionError(
                    f"appstore.update_metadata localizations[{locale!r}][{name!r}] must be a string, "
                    f"got {type(value).__name__}; no implicit conversion is performed"
                )
            clean[name] = value
        validated[locale.strip()] = clean
    return validated


def _attribute(resource: dict, name: str) -> str | None:
    value = (resource.get("attributes") or {}).get(name)
    return value if isinstance(value, str) else None


def _current_text(resource: dict, attribute: str) -> str:
    """Current stored text; a missing/null attribute counts as empty."""
    return _attribute(resource, attribute) or ""


def _localizations_by_locale(version_id: str, client: _AscClient) -> dict[str, dict]:
    resources: dict[str, dict] = {}
    for loc in review.get_version_localizations(version_id, client):
        locale = _attribute(loc, "locale")
        if isinstance(locale, str):
            resources[locale] = loc
    return resources


def _verified(version_id: str, locale: str, fields: dict[str, str], client: _AscClient) -> bool:
    """True when a fresh read shows every requested field with its requested value."""
    resource = _localizations_by_locale(version_id, client).get(locale)
    if resource is None:
        return False
    for name, requested in fields.items():
        if _current_text(resource, METADATA_FIELDS[name]) != requested:
            return False
    return True


def update_localizations(
    version_id: str,
    localizations: dict[str, dict[str, str]],
    client: _AscClient,
) -> tuple[dict[str, list[str]], list[str]]:
    """Update existing version localizations and return ``(updated, unchanged)``.

    ``updated`` maps each changed locale to the field names that differed and
    were written; ``unchanged`` lists locales whose requested fields already
    matched. All requested locales are verified to exist before the first
    mutation, locales are processed in stable sorted order and every PATCH is
    issued with ``retry_ambiguous=False`` and verified by a read-back.
    """
    if not localizations:
        raise InvalidMetadataOptionError("localizations must contain at least one locale")

    existing = _localizations_by_locale(version_id, client)
    unknown = sorted(set(localizations) - set(existing))
    if unknown:
        raise review.UnknownLocaleError(
            "No App Store version localization for locale(s): "
            + ", ".join(unknown)
            + f". Prepare them in App Store Connect for version {version_id} first; "
            "CDT does not create partial app cards"
        )

    updated: dict[str, list[str]] = {}
    unchanged: list[str] = []
    for locale in sorted(localizations):
        fields = localizations[locale]
        resource = existing[locale]
        changed = [name for name in fields if _current_text(resource, METADATA_FIELDS[name]) != fields[name]]
        if not changed:
            unchanged.append(locale)
            continue

        attributes = {METADATA_FIELDS[name]: fields[name] for name in changed}
        payload = {
            "data": {
                "type": "appStoreVersionLocalizations",
                "id": resource["id"],
                "attributes": attributes,
            }
        }
        try:
            appstore._asc_request(
                "PATCH",
                f"/v1/appStoreVersionLocalizations/{resource['id']}",
                client,
                payload,
                retry_ambiguous=False,
            )
        except appstore.AscAmbiguousResultError as exc:
            # The PATCH may or may not have been applied: decide by reading,
            # never by repeating it in this run.
            if _verified(version_id, locale, fields, client):
                updated[locale] = changed
                continue
            raise UnverifiedMetadataError(
                f"The metadata PATCH for locale {locale!r} of version {version_id} failed ambiguously "
                f"and the read-back does not confirm the requested values; the result is unverified "
                f"and no further PATCH is sent in this run - verify the localization in App Store Connect"
            ) from exc
        if not _verified(version_id, locale, fields, client):
            raise UnverifiedMetadataError(
                f"The metadata PATCH for locale {locale!r} of version {version_id} was accepted, but the "
                f"read-back does not match the requested values; the result is unverified and no further "
                f"PATCH is sent in this run - verify the localization in App Store Connect"
            )
        updated[locale] = changed
    return updated, unchanged


def update_metadata(
    bundle_id: str,
    version: str,
    localizations: dict[str, dict[str, str]],
    client: _AscClient,
) -> MetadataOutcome:
    """Resolve the exact existing iOS App Store version and update its texts.

    The app is looked up by bundle id, the version by its exact version string
    (paginated, client-side verification). A missing version is an error —
    CDT never creates versions here — and the version must be in
    ``PREPARE_FOR_SUBMISSION`` before any mutation is attempted.
    """
    app_id = review.find_app_id(bundle_id, client)
    found = review.find_app_store_version(app_id, version, client)
    if found is None:
        raise ResourceNotFoundError(
            f"App Store version {version} not found for app {bundle_id}; CDT updates metadata of an "
            "existing version only and does not create versions"
        )
    review.ensure_version_editable(found)
    updated, unchanged = update_localizations(str(found["id"]), localizations, client)
    return MetadataOutcome(
        bundle_id=bundle_id,
        version_id=str(found.get("id")),
        version_string=review.version_string(found) or version,
        app_store_state=review.version_state(found) or "",
        updated=updated,
        unchanged=unchanged,
    )
