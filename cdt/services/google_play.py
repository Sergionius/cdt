"""Narrow Google Play Android Publisher v3 client with ADC.

The client intentionally exposes only the operations needed to publish one AAB
to one explicitly chosen track: edits (create/get/delete/validate/commit),
bundles (list/upload) and tracks (get/update). There is no Ruby tooling and no
external publishing CLI involved.

Security and safety properties:

- Credentials are created per call from the final pipeline ``env`` (never by
  mutating the global ``os.environ``): an explicit ADC file from
  ``GOOGLE_APPLICATION_CREDENTIALS`` (relative paths resolved against the
  project root) or ``google.auth.default`` otherwise.
- HTTP requests use finite per-request timeouts; AAB uploads stream from disk
  through a resumable upload with a dedicated 120-second timeout.
- Mutations are never retried automatically (a blind retry could duplicate an
  upload or re-commit changes Google rejected). Read-only requests allow a
  small number of automatic retries, which googleapiclient applies only to
  transient network errors, HTTP 429 and 5xx responses.
- Errors are normalized to a stage, an HTTP code and a sanitized reason that
  never contains credential JSON, access tokens, Authorization headers or
  upload-session URLs.
"""

from __future__ import annotations

import hashlib
import json
import re
import socket
from pathlib import Path
from typing import Any

import google.auth
import google.auth.exceptions
import google.oauth2.service_account
import google_auth_httplib2
import httplib2
from googleapiclient import discovery, errors
from googleapiclient.http import MediaFileUpload

ANDROIDPUBLISHER_SCOPE = "https://www.googleapis.com/auth/androidpublisher"
ANDROIDPUBLISHER_API = "androidpublisher"
ANDROIDPUBLISHER_VERSION = "v3"

# Finite per-request HTTP timeouts. Uploads get their own, longer budget.
API_HTTP_TIMEOUT_SEC = 60.0
UPLOAD_HTTP_TIMEOUT_SEC = 120.0

# Resumable upload chunk size; must be a multiple of 256 KiB. The AAB is
# streamed from disk in chunks, never read fully into memory.
UPLOAD_CHUNK_BYTES = 8 * 1024 * 1024

# Automatic retries are limited to read-only requests. googleapiclient applies
# these retries only to transient socket errors and HTTP 429/5xx responses.
READ_RETRY_MAX = 3

# commit must never silently cancel or take over an in-progress review.
COMMIT_CHANGES_IN_REVIEW_BEHAVIOR = "ERROR_IF_IN_REVIEW"

# Error stages (stable identifiers used by steps, state handling and tests).
STAGE_AUTH = "auth"
STAGE_EDIT_CREATE = "edit_create"
STAGE_EDIT_GET = "edit_get"
STAGE_EDIT_DELETE = "edit_delete"
STAGE_BUNDLES_LIST = "bundles_list"
STAGE_BUNDLE_UPLOAD = "bundle_upload"
STAGE_TRACK_GET = "track_get"
STAGE_TRACK_UPDATE = "track_update"
STAGE_EDIT_VALIDATE = "edit_validate"
STAGE_EDIT_COMMIT = "edit_commit"

_MAX_REASON_LENGTH = 400
_REDACTED = "[redacted]"

# Secrets-free diagnostics: never echo bearer tokens, token assignments, Basic
# auth values or any URL (upload-session URLs carry unguessable upload IDs and
# are never shown).
_SECRET_PATTERNS = (
    re.compile(r"(?i)bearer\s+[A-Za-z0-9\-._~+/]+=*"),
    re.compile(r"(?i)(access_token|refresh_token|id_token|assertion)\s*[=:]\s*\S+"),
    re.compile(r"(?i)authorization\s*[=:]\s*.*"),
    re.compile(r"https?://\S+"),
)


class GooglePlayError(Exception):
    """Normalized Google Play failure.

    Carries the operation ``stage``, an optional ``http_status`` and a
    ``reason`` sanitized from credential JSON, access tokens, Authorization
    headers and upload-session URLs.
    """

    def __init__(self, stage: str, reason: str, http_status: int | None = None):
        self.stage = stage
        self.http_status = http_status
        self.reason = _sanitize_reason(reason)
        super().__init__(self.summary())

    def summary(self) -> str:
        status = f" (HTTP {self.http_status})" if self.http_status is not None else ""
        return f"Google Play {self.stage} failed{status}: {self.reason}"


def _sanitize_reason(reason: str) -> str:
    text = (reason or "").strip()
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(_REDACTED, text)
    if len(text) > _MAX_REASON_LENGTH:
        text = text[:_MAX_REASON_LENGTH].rstrip() + "..."
    return text or "unknown error"


def _extract_http_reason(exc: errors.HttpError) -> str:
    content = getattr(exc, "content", b"") or b""
    try:
        payload = json.loads(content.decode("utf-8"))
        message = payload.get("error", {}).get("message")
        if isinstance(message, str) and message:
            return message
    except (AttributeError, UnicodeDecodeError, ValueError):
        pass
    resp = getattr(exc, "resp", None)
    reason = getattr(resp, "reason", "") if resp is not None else ""
    return reason or type(exc).__name__


def _http_status_of(exc: errors.HttpError) -> int | None:
    status = getattr(exc, "status_code", None)
    if status is None:
        resp = getattr(exc, "resp", None)
        status = getattr(resp, "status", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _normalize_http_error(stage: str, exc: errors.HttpError) -> GooglePlayError:
    return GooglePlayError(stage, _extract_http_reason(exc), http_status=_http_status_of(exc))


def _wrap_transport_failure(stage: str, exc: BaseException) -> GooglePlayError:
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return GooglePlayError(stage, "request timed out")
    if isinstance(exc, httplib2.HttpLib2Error):
        return GooglePlayError(stage, f"connection failed ({type(exc).__name__})")
    return GooglePlayError(stage, f"network failure ({type(exc).__name__})")


def _execute(request: Any, stage: str, *, mutation: bool) -> Any:
    """Execute one API request according to the retry policy.

    Mutations run with ``num_retries=0``: googleapiclient would otherwise
    blindly repeat POSTs, which could duplicate an AAB upload or re-commit
    changes Google already rejected. Read-only requests run with a limited
    retry budget; googleapiclient retries them only on transient network
    errors and 429/5xx responses.
    """
    try:
        return request.execute(num_retries=0 if mutation else READ_RETRY_MAX)
    except errors.HttpError as exc:
        raise _normalize_http_error(stage, exc) from exc
    except google.auth.exceptions.GoogleAuthError as exc:
        raise GooglePlayError(stage, f"authentication failed ({type(exc).__name__})") from exc
    except (httplib2.HttpLib2Error, OSError) as exc:
        raise _wrap_transport_failure(stage, exc) from exc


def _execute_upload(request: Any, stage: str, http: Any) -> dict[str, Any]:
    """Drive a resumable upload to completion with no automatic retries."""
    try:
        response: dict[str, Any] | None = None
        while response is None:
            _progress, response = request.next_chunk(http=http, num_retries=0)
        return response
    except errors.HttpError as exc:
        raise _normalize_http_error(stage, exc) from exc
    except google.auth.exceptions.GoogleAuthError as exc:
        raise GooglePlayError(stage, f"authentication failed ({type(exc).__name__})") from exc
    except (httplib2.HttpLib2Error, OSError) as exc:
        raise _wrap_transport_failure(stage, exc) from exc


def compute_file_sha256(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Compute the SHA-256 of a file by streaming it in chunks.

    Like the resumable upload, this never reads the whole AAB into memory.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_adc_path(env: dict[str, str], cwd: Path) -> Path | None:
    """Return the ADC file path named by ``GOOGLE_APPLICATION_CREDENTIALS``.

    Relative values are resolved against ``cwd`` (the pipeline project root).
    Only the passed-in ``env`` is inspected; the global ``os.environ`` is never
    read or modified here.
    """
    raw = (env.get("GOOGLE_APPLICATION_CREDENTIALS") or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = cwd / path
    return path


def _default_credentials() -> google.auth.credentials.Credentials:
    credentials, _project = google.auth.default(scopes=[ANDROIDPUBLISHER_SCOPE])
    return credentials


def load_credentials(env: dict[str, str], cwd: Path) -> google.auth.credentials.Credentials:
    """Load Android Publisher credentials for a single call.

    Prefers the ADC file named by ``GOOGLE_APPLICATION_CREDENTIALS`` in the
    final pipeline ``env`` and otherwise falls back to ``google.auth.default``.
    Never mutates ``os.environ``.
    """
    adc_path = resolve_adc_path(env, cwd)
    if adc_path is not None:
        if not adc_path.is_file():
            raise GooglePlayError(STAGE_AUTH, f"GOOGLE_APPLICATION_CREDENTIALS file not found: {adc_path}")
        try:
            return google.oauth2.service_account.Credentials.from_service_account_file(
                str(adc_path), scopes=[ANDROIDPUBLISHER_SCOPE]
            )
        except Exception as exc:
            raise GooglePlayError(
                STAGE_AUTH, f"invalid GOOGLE_APPLICATION_CREDENTIALS file ({type(exc).__name__})"
            ) from exc
    try:
        return _default_credentials()
    except google.auth.exceptions.DefaultCredentialsError as exc:
        raise GooglePlayError(
            STAGE_AUTH,
            "no Application Default Credentials found; set GOOGLE_APPLICATION_CREDENTIALS "
            "or configure ADC for the environment",
        ) from exc


def refresh_credentials(credentials: google.auth.credentials.Credentials) -> None:
    """Refresh credentials eagerly so auth failures surface at the auth stage."""
    if credentials.valid:
        return
    request = google_auth_httplib2.Request(httplib2.Http(timeout=API_HTTP_TIMEOUT_SEC))
    try:
        credentials.refresh(request)
    except google.auth.exceptions.GoogleAuthError as exc:
        raise GooglePlayError(STAGE_AUTH, f"credential refresh failed ({type(exc).__name__})") from exc
    except (httplib2.HttpLib2Error, OSError) as exc:
        raise _wrap_transport_failure(STAGE_AUTH, exc) from exc


def _build_authorized_http(credentials: google.auth.credentials.Credentials, timeout: float) -> Any:
    return google_auth_httplib2.AuthorizedHttp(credentials, http=httplib2.Http(timeout=timeout))


def _build_service(http: Any) -> Any:
    """Build the Android Publisher v3 resource from the bundled discovery document (offline)."""
    return discovery.build(
        ANDROIDPUBLISHER_API,
        ANDROIDPUBLISHER_VERSION,
        http=http,
        cache_discovery=False,
        static_discovery=True,
    )


class GooglePlayClient:
    """Per-call Android Publisher client with narrow operations.

    Every instance loads its own credentials and builds its own HTTP clients,
    so parallel pipeline branches never share authorization state and the
    global ``os.environ`` stays untouched.
    """

    def __init__(
        self,
        env: dict[str, str],
        cwd: Path,
        *,
        api_timeout: float = API_HTTP_TIMEOUT_SEC,
        upload_timeout: float = UPLOAD_HTTP_TIMEOUT_SEC,
    ):
        self._credentials = load_credentials(env, cwd)
        refresh_credentials(self._credentials)
        self._api_http = _build_authorized_http(self._credentials, api_timeout)
        self._upload_http = _build_authorized_http(self._credentials, upload_timeout)
        self._service = _build_service(self._api_http)

    @property
    def service(self) -> Any:
        return self._service

    @property
    def api_http(self) -> Any:
        return self._api_http

    @property
    def upload_http(self) -> Any:
        return self._upload_http

    # -- edits ---------------------------------------------------------------

    def create_edit(self, package_name: str) -> dict[str, Any]:
        """Open a new edit (mutation, no automatic retries)."""
        request = self._service.edits().insert(packageName=package_name, body={})
        return _execute(request, STAGE_EDIT_CREATE, mutation=True)

    def get_edit(self, package_name: str, edit_id: str) -> dict[str, Any]:
        request = self._service.edits().get(packageName=package_name, editId=edit_id)
        return _execute(request, STAGE_EDIT_GET, mutation=False)

    def delete_edit(self, package_name: str, edit_id: str) -> None:
        """Delete the edit (mutation, no automatic retries)."""
        request = self._service.edits().delete(packageName=package_name, editId=edit_id)
        _execute(request, STAGE_EDIT_DELETE, mutation=True)

    def validate_edit(self, package_name: str, edit_id: str) -> dict[str, Any]:
        """Validate the edit without changing anything (POST, so no retries)."""
        request = self._service.edits().validate(packageName=package_name, editId=edit_id)
        return _execute(request, STAGE_EDIT_VALIDATE, mutation=True)

    def commit_edit(self, package_name: str, edit_id: str) -> dict[str, Any]:
        """Commit the edit for the standard Google Play review/publication flow.

        ``changesInReviewBehavior=ERROR_IF_IN_REVIEW`` always makes Google fail
        the commit while the app is currently under review instead of silently
        cancelling that review. ``changesNotSentForReview`` is never set, and
        flags are never changed after a Google rejection.
        """
        request = self._service.edits().commit(
            packageName=package_name,
            editId=edit_id,
            changesInReviewBehavior=COMMIT_CHANGES_IN_REVIEW_BEHAVIOR,
        )
        return _execute(request, STAGE_EDIT_COMMIT, mutation=True)

    # -- bundles ---------------------------------------------------------

    def list_bundles(self, package_name: str, edit_id: str) -> list[dict[str, Any]]:
        request = self._service.edits().bundles().list(packageName=package_name, editId=edit_id)
        response = _execute(request, STAGE_BUNDLES_LIST, mutation=False)
        return list(response.get("bundles", []))

    def upload_bundle(self, package_name: str, edit_id: str, aab_path: Path) -> dict[str, Any]:
        """Upload one AAB via resumable upload, streaming it from disk.

        The file is never read fully into memory; each chunk request uses the
        dedicated upload HTTP client with a 120-second per-request timeout.
        Automatic retries stay disabled: a repeated upload could create a
        duplicate bundle, and a lost response is resolved by the state layer.
        """
        try:
            media = MediaFileUpload(
                str(aab_path),
                mimetype="application/octet-stream",
                resumable=True,
                chunksize=UPLOAD_CHUNK_BYTES,
            )
        except OSError as exc:
            raise GooglePlayError(STAGE_BUNDLE_UPLOAD, f"cannot read AAB file ({type(exc).__name__})") from exc
        request = self._service.edits().bundles().upload(
            packageName=package_name,
            editId=edit_id,
            media_body=media,
            ackBundleInstallationWarning=False,
        )
        return _execute_upload(request, STAGE_BUNDLE_UPLOAD, self._upload_http)

    # -- tracks ----------------------------------------------------------

    def get_track(self, package_name: str, edit_id: str, track: str) -> dict[str, Any]:
        request = self._service.edits().tracks().get(packageName=package_name, editId=edit_id, track=track)
        return _execute(request, STAGE_TRACK_GET, mutation=False)

    def update_track(
        self, package_name: str, edit_id: str, track: str, releases: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Replace the target track's releases with ``releases`` (mutation)."""
        body = {"track": track, "releases": releases}
        request = self._service.edits().tracks().update(
            packageName=package_name,
            editId=edit_id,
            track=track,
            body=body,
        )
        return _execute(request, STAGE_TRACK_UPDATE, mutation=True)
