"""Mocked tests for the narrow Google Play Android Publisher client.

No test uses real credentials, the network or Google Play: ADC handling is
exercised against a locally generated service account file, and the API layer
is replaced by recording fakes.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httplib2
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from google.auth import exceptions as google_auth_exceptions
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, _should_retry_response

from cdt.services import google_play

PUBLISHER_EMAIL = "publisher@test-project.iam.gserviceaccount.com"

READ_LABELS = ("edits.get", "edits.bundles.list", "edits.tracks.get")
MUTATION_LABELS = ("edits.insert", "edits.delete", "edits.tracks.update", "edits.validate", "edits.commit")


def http_error(status: int, reason: str, body: dict[str, Any]) -> HttpError:
    response = httplib2.Response({"status": str(status), "reason": reason})
    return HttpError(response, json.dumps(body).encode("utf-8"))


class Recorder:
    def __init__(self):
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.requests: list[Any] = []

    def calls_for(self, label: str) -> list[dict[str, Any]]:
        return [kwargs for name, kwargs in self.calls if name == label]


class FakeRequest:
    """Stand-in for one googleapiclient request (execute-based operations)."""

    def __init__(self, recorder: Recorder, label: str, response: Any = None, error: Exception | None = None):
        self._recorder = recorder
        self.label = label
        self._response = response
        self._error = error
        self.num_retries: int | None = None

    def execute(self, http: Any = None, num_retries: int = 0) -> Any:
        self.num_retries = num_retries
        self._recorder.requests.append(self)
        if self._error is not None:
            raise self._error
        return self._response


class FakeUploadRequest:
    """Stand-in for the resumable bundles.upload request."""

    def __init__(self, recorder: Recorder, chunks: list[tuple[str, Any]]):
        self._recorder = recorder
        self._chunks = list(chunks)
        self.calls: list[dict[str, Any]] = []
        self.media_body: Any = None

    def next_chunk(self, http: Any = None, num_retries: int = 0) -> tuple[Any, Any]:
        self.calls.append({"http": http, "num_retries": num_retries})
        self._recorder.requests.append(self)
        kind, value = self._chunks.pop(0)
        if kind == "error":
            raise value
        if kind == "progress":
            return object(), None
        return None, value


class _FakeBundles:
    def __init__(self, service: "FakeService"):
        self._service = service

    def list(self, **kwargs: Any) -> FakeRequest:
        return self._service._make("edits.bundles.list", kwargs)

    def upload(self, **kwargs: Any) -> FakeUploadRequest:
        self._service.recorder.calls.append(("edits.bundles.upload", kwargs))
        request = FakeUploadRequest(self._service.recorder, self._service.upload_chunks)
        request.media_body = kwargs.get("media_body")
        self._service.upload_request = request
        return request


class _FakeTracks:
    def __init__(self, service: "FakeService"):
        self._service = service

    def get(self, **kwargs: Any) -> FakeRequest:
        return self._service._make("edits.tracks.get", kwargs)

    def update(self, **kwargs: Any) -> FakeRequest:
        return self._service._make("edits.tracks.update", kwargs)


class _FakeEdits:
    def __init__(self, service: "FakeService"):
        self._service = service

    def bundles(self) -> _FakeBundles:
        return _FakeBundles(self._service)

    def tracks(self) -> _FakeTracks:
        return _FakeTracks(self._service)

    def insert(self, **kwargs: Any) -> FakeRequest:
        return self._service._make("edits.insert", kwargs)

    def get(self, **kwargs: Any) -> FakeRequest:
        return self._service._make("edits.get", kwargs)

    def delete(self, **kwargs: Any) -> FakeRequest:
        return self._service._make("edits.delete", kwargs)

    def validate(self, **kwargs: Any) -> FakeRequest:
        return self._service._make("edits.validate", kwargs)

    def commit(self, **kwargs: Any) -> FakeRequest:
        return self._service._make("edits.commit", kwargs)


class FakeService:
    """Recording fake of the Android Publisher v3 resource."""

    def __init__(self):
        self.recorder = Recorder()
        self.responses: dict[str, Any] = {}
        self.errors: dict[str, Exception] = {}
        self.upload_chunks: list[tuple[str, Any]] = []
        self.upload_request: FakeUploadRequest | None = None

    def plan(self, label: str, response: Any = None, error: Exception | None = None) -> None:
        self.responses[label] = response
        if error is not None:
            self.errors[label] = error

    def _make(self, label: str, kwargs: dict[str, Any]) -> FakeRequest:
        self.recorder.calls.append((label, kwargs))
        error = self.errors.get(label)
        if error is not None:
            return FakeRequest(self.recorder, label, error=error)
        return FakeRequest(self.recorder, label, response=self.responses.get(label))

    def edits(self) -> _FakeEdits:
        return _FakeEdits(self)


class FakeCredentials:
    def __init__(self):
        self.valid = False
        self.refresh_calls = 0
        self.fail_refresh: Exception | None = None

    def refresh(self, request: Any) -> None:
        self.refresh_calls += 1
        if self.fail_refresh is not None:
            raise self.fail_refresh
        self.valid = True


@pytest.fixture
def fake_parts(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Patch client construction points; nothing touches google.auth or httplib2 networking."""
    credentials = FakeCredentials()
    service = FakeService()
    https: list[Any] = []
    loads: list[dict[str, str]] = []

    def fake_load(env: dict[str, str], cwd: Path) -> FakeCredentials:
        loads.append(dict(env))
        return credentials

    def fake_http(_credentials: Any, timeout: float) -> Any:
        http = SimpleNamespace(timeout=timeout)
        https.append(http)
        return http

    monkeypatch.setattr(google_play, "load_credentials", fake_load)
    monkeypatch.setattr(google_play, "_build_authorized_http", fake_http)
    monkeypatch.setattr(google_play, "_build_service", lambda http: service)

    def make_client(env: dict[str, str] | None = None) -> google_play.GooglePlayClient:
        return google_play.GooglePlayClient(env or {}, Path("/project"))

    return SimpleNamespace(
        credentials=credentials,
        service=service,
        https=https,
        loads=loads,
        make_client=make_client,
    )


@pytest.fixture(scope="module")
def service_account_root(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str]:
    """A local service account file backed by a real RSA key (parsed by google-auth)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    payload = {
        "type": "service_account",
        "project_id": "test-project",
        "private_key_id": "kid123",
        "private_key": pem,
        "client_email": PUBLISHER_EMAIL,
        "client_id": "123456789",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "client_x509_cert_url": "https://www.googleapis.com/robot/v1/metadata/x509/publisher",
    }
    return tmp_path_factory.mktemp("adc"), json.dumps(payload)


# -- scope, client construction, timeouts --------------------------------------


def test_androidpublisher_scope_constant():
    assert google_play.ANDROIDPUBLISHER_SCOPE == "https://www.googleapis.com/auth/androidpublisher"


def test_bundled_discovery_supports_changes_in_review_behavior():
    import googleapiclient

    root = Path(googleapiclient.__file__).parent
    doc = json.loads(
        (root / "discovery_cache" / "documents" / "androidpublisher.v3.json").read_text(encoding="utf-8")
    )
    parameter = doc["resources"]["edits"]["methods"]["commit"]["parameters"]["changesInReviewBehavior"]
    assert "ERROR_IF_IN_REVIEW" in parameter["enum"]


def test_build_service_targets_androidpublisher_v3_offline():
    http = google_play._build_authorized_http(FakeCredentials(), google_play.API_HTTP_TIMEOUT_SEC)
    service = google_play._build_service(http)
    assert getattr(service, "_baseUrl", "") == "https://androidpublisher.googleapis.com/"
    assert hasattr(service, "edits")
    assert http.http.timeout == google_play.API_HTTP_TIMEOUT_SEC


def test_client_requests_match_real_offline_discovery(monkeypatch, tmp_path):
    """Construct real SDK requests; replace only execution, never discovery methods."""
    monkeypatch.setattr(google_play, "load_credentials", lambda env, cwd: FakeCredentials())
    client = google_play.GooglePlayClient({}, tmp_path)
    calls = []

    def execute(request, stage, *, mutation):
        calls.append(request)
        return {}

    def upload(request, stage, http):
        calls.append(request)
        return {}

    monkeypatch.setattr(google_play, "_execute", execute)
    monkeypatch.setattr(google_play, "_execute_upload", upload)
    aab = tmp_path / "app.aab"
    aab.write_bytes(b"bundle")
    client.create_edit("com.example.app")
    assert calls[-1].method == "POST"
    assert json.loads(calls[-1].body) == {}
    client.get_edit("com.example.app", "edit-1")
    client.list_bundles("com.example.app", "edit-1")
    client.upload_bundle("com.example.app", "edit-1", aab)
    assert calls[-1].resumable is not None
    client.get_track("com.example.app", "edit-1", "internal")
    releases = [{"status": "draft", "versionCodes": ["42"]}]
    client.update_track("com.example.app", "edit-1", "internal", releases)
    assert json.loads(calls[-1].body) == {"track": "internal", "releases": releases}
    client.validate_edit("com.example.app", "edit-1")
    client.commit_edit("com.example.app", "edit-1")
    assert "changesInReviewBehavior=ERROR_IF_IN_REVIEW" in calls[-1].uri
    client.delete_edit("com.example.app", "edit-1")
    assert len(calls) == 9


def test_authorized_http_uses_finite_timeout():
    http = google_play._build_authorized_http(FakeCredentials(), 42.0)
    assert http.http.timeout == 42.0


def test_client_uses_distinct_timeouts_and_clients_per_call(fake_parts):
    first = fake_parts.make_client()
    second = fake_parts.make_client()

    assert first.api_http is not second.api_http
    assert first.upload_http is not second.upload_http
    assert fake_parts.https[0].timeout == google_play.API_HTTP_TIMEOUT_SEC
    assert fake_parts.https[1].timeout == google_play.UPLOAD_HTTP_TIMEOUT_SEC
    # Credentials are created separately for every call, never cached globally.
    assert len(fake_parts.loads) == 2


def test_client_refreshes_credentials_once_per_call(fake_parts):
    client = fake_parts.make_client()
    assert fake_parts.credentials.refresh_calls == 1
    assert fake_parts.credentials.valid is True
    assert client.service is fake_parts.service


def test_client_skips_refresh_when_credentials_already_valid(fake_parts):
    fake_parts.credentials.valid = True
    fake_parts.make_client()
    assert fake_parts.credentials.refresh_calls == 0


def test_refresh_failure_is_normalized_to_auth_stage_without_leaking_details(fake_parts):
    fake_parts.credentials.fail_refresh = google_auth_exceptions.RefreshError("token endpoint said expired")
    with pytest.raises(google_play.GooglePlayError) as excinfo:
        fake_parts.make_client()
    error = excinfo.value
    assert error.stage == google_play.STAGE_AUTH
    assert "expired" not in error.reason
    assert "RefreshError" in error.reason


# -- ADC loading ----------------------------------------------------------------


def test_load_credentials_uses_adc_file_relative_to_project_root(service_account_root):
    root, payload = service_account_root
    keys = root / "keys"
    keys.mkdir(exist_ok=True)
    (keys / "sa.json").write_text(payload, encoding="utf-8")

    credentials = google_play.load_credentials({"GOOGLE_APPLICATION_CREDENTIALS": "keys/sa.json"}, root)

    assert credentials.service_account_email == PUBLISHER_EMAIL
    assert google_play.ANDROIDPUBLISHER_SCOPE in (credentials.scopes or ())


def test_load_credentials_accepts_absolute_adc_path(service_account_root):
    root, payload = service_account_root
    path = root / "absolute-sa.json"
    path.write_text(payload, encoding="utf-8")

    credentials = google_play.load_credentials({"GOOGLE_APPLICATION_CREDENTIALS": str(path)}, root)

    assert credentials.service_account_email == PUBLISHER_EMAIL


def test_load_credentials_missing_adc_file_raises_normalized_auth_error():
    with pytest.raises(google_play.GooglePlayError) as excinfo:
        google_play.load_credentials({"GOOGLE_APPLICATION_CREDENTIALS": "missing/sa.json"}, Path("/nowhere"))
    assert excinfo.value.stage == google_play.STAGE_AUTH
    assert "missing/sa.json" in str(excinfo.value)


def test_load_credentials_invalid_file_never_leaks_content(service_account_root, tmp_path):
    root, _ = service_account_root
    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps({"type": "service_account", "private_key": "-----BEGIN PRIVATE KEY-----SUPERSECRET-----END-----"}),
        encoding="utf-8",
    )

    with pytest.raises(google_play.GooglePlayError) as excinfo:
        google_play.load_credentials({"GOOGLE_APPLICATION_CREDENTIALS": str(bad)}, tmp_path)

    assert excinfo.value.stage == google_play.STAGE_AUTH
    assert "SUPERSECRET" not in str(excinfo.value)
    assert "SUPERSECRET" not in excinfo.value.reason


def test_load_credentials_falls_back_to_google_auth_default(monkeypatch):
    sentinel = FakeCredentials()

    def fake_default(scopes=None):
        return sentinel

    monkeypatch.setattr(google_play, "_default_credentials", fake_default)

    credentials = google_play.load_credentials({}, Path("/project"))

    assert credentials is sentinel


def test_default_credentials_request_androidpublisher_scope(monkeypatch):
    sentinel = FakeCredentials()
    seen: dict[str, Any] = {}

    def fake_google_default(**kwargs):
        seen.update(kwargs)
        return sentinel, "test-project"

    monkeypatch.setattr(google_play.google.auth, "default", fake_google_default)

    credentials = google_play._default_credentials()

    assert credentials is sentinel
    assert seen["scopes"] == [google_play.ANDROIDPUBLISHER_SCOPE]


def test_load_credentials_reports_missing_default_credentials(monkeypatch):
    def fake_default(scopes=None):
        raise google_auth_exceptions.DefaultCredentialsError("Could not automatically determine credentials")

    monkeypatch.setattr(google_play, "_default_credentials", fake_default)

    with pytest.raises(google_play.GooglePlayError) as excinfo:
        google_play.load_credentials({}, Path("/project"))
    assert excinfo.value.stage == google_play.STAGE_AUTH


def test_client_creation_never_touches_os_environment(monkeypatch, fake_parts):
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    snapshot = dict(os.environ)

    env = {"GOOGLE_APPLICATION_CREDENTIALS": "keys/sa.json"}
    fake_parts.make_client(env)

    assert os.environ == snapshot
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ
    assert fake_parts.loads == [env]


# -- narrow operations and retry policy -----------------------------------------


def test_narrow_operations_call_expected_api_methods(fake_parts):
    client = fake_parts.make_client()
    service = fake_parts.service
    service.plan("edits.insert", response={"id": "edit-1", "expiryTimeSeconds": "3600"})
    service.plan("edits.get", response={"id": "edit-1"})
    service.plan("edits.bundles.list", response={"bundles": [{"versionCode": 7}]})
    service.plan("edits.tracks.get", response={"track": "internal", "releases": []})
    service.plan("edits.tracks.update", response={"track": "internal"})
    service.plan("edits.validate", response={"editId": "edit-1"})

    created = client.create_edit("com.example.app")
    got_edit = client.get_edit("com.example.app", "edit-1")
    bundles = client.list_bundles("com.example.app", "edit-1")
    track = client.get_track("com.example.app", "edit-1", "internal")
    updated = client.update_track("com.example.app", "edit-1", "internal", [{"status": "draft"}])
    client.validate_edit("com.example.app", "edit-1")
    client.delete_edit("com.example.app", "edit-1")

    assert created["id"] == "edit-1"
    assert got_edit["id"] == "edit-1"
    assert bundles == [{"versionCode": 7}]
    assert track["track"] == "internal"
    assert updated["track"] == "internal"

    assert service.recorder.calls == [
        ("edits.insert", {"packageName": "com.example.app", "body": {}}),
        ("edits.get", {"packageName": "com.example.app", "editId": "edit-1"}),
        ("edits.bundles.list", {"packageName": "com.example.app", "editId": "edit-1"}),
        ("edits.tracks.get", {"packageName": "com.example.app", "editId": "edit-1", "track": "internal"}),
        (
            "edits.tracks.update",
            {
                "packageName": "com.example.app",
                "editId": "edit-1",
                "track": "internal",
                "body": {"track": "internal", "releases": [{"status": "draft"}]},
            },
        ),
        ("edits.validate", {"packageName": "com.example.app", "editId": "edit-1"}),
        ("edits.delete", {"packageName": "com.example.app", "editId": "edit-1"}),
    ]


def test_reads_use_limited_retry_budget_and_mutations_none(fake_parts):
    client = fake_parts.make_client()
    service = fake_parts.service
    for label in (*READ_LABELS, *MUTATION_LABELS):
        service.plan(label, response={})

    client.get_edit("com.app", "e1")
    client.list_bundles("com.app", "e1")
    client.get_track("com.app", "e1", "internal")
    client.create_edit("com.app")
    client.delete_edit("com.app", "e1")
    client.update_track("com.app", "e1", "internal", [])
    client.validate_edit("com.app", "e1")
    client.commit_edit("com.app", "e1")

    requests_by_label: dict[str, list[FakeRequest]] = {}
    for (label, _kwargs), request in zip(service.recorder.calls, service.recorder.requests):
        requests_by_label.setdefault(label, []).append(request)

    for label in (*READ_LABELS, *MUTATION_LABELS):
        assert len(requests_by_label[label]) == 1, label

    for label in READ_LABELS:
        assert requests_by_label[label][0].num_retries == google_play.READ_RETRY_MAX, label
    for label in MUTATION_LABELS:
        assert requests_by_label[label][0].num_retries == 0, label


def test_googleapiclient_read_retries_cover_transient_errors_429_and_5xx_only():
    assert _should_retry_response(429, b"") is True
    assert _should_retry_response(500, b"") is True
    assert _should_retry_response(503, b"") is True
    assert _should_retry_response(403, b"") is False
    assert _should_retry_response(401, b"") is False
    assert _should_retry_response(200, b"") is False


def test_failed_mutation_is_never_blindly_retried(fake_parts):
    client = fake_parts.make_client()
    fake_parts.service.plan(
        "edits.commit",
        error=http_error(503, "Backend Error", {"error": {"message": "Backend Error"}}),
    )

    with pytest.raises(google_play.GooglePlayError) as excinfo:
        client.commit_edit("com.example.app", "edit-1")

    assert excinfo.value.http_status == 503
    assert len(fake_parts.service.recorder.requests) == 1
    assert fake_parts.service.recorder.requests[0].num_retries == 0


def test_timeout_on_read_is_normalized_without_http_status(fake_parts):
    client = fake_parts.make_client()
    fake_parts.service.plan("edits.get", error=socket.timeout("timed out"))

    with pytest.raises(google_play.GooglePlayError) as excinfo:
        client.get_edit("com.example.app", "edit-1")

    assert excinfo.value.stage == google_play.STAGE_EDIT_GET
    assert excinfo.value.http_status is None
    assert "timed out" in excinfo.value.reason


# -- commit review-safety flags --------------------------------------------------


def test_commit_always_sets_error_if_in_review(fake_parts):
    client = fake_parts.make_client()
    fake_parts.service.plan("edits.commit", response={"id": "edit-1"})

    client.commit_edit("com.example.app", "edit-1")

    (kwargs,) = fake_parts.service.recorder.calls_for("edits.commit")
    assert kwargs["changesInReviewBehavior"] == google_play.COMMIT_CHANGES_IN_REVIEW_BEHAVIOR
    assert kwargs["changesInReviewBehavior"] == "ERROR_IF_IN_REVIEW"


def test_no_operation_ever_disables_sending_changes_for_review(fake_parts):
    client = fake_parts.make_client()
    service = fake_parts.service
    for label in (*READ_LABELS, *MUTATION_LABELS):
        service.plan(label, response={})

    client.get_edit("com.app", "e1")
    client.list_bundles("com.app", "e1")
    client.get_track("com.app", "e1", "internal")
    client.create_edit("com.app")
    client.delete_edit("com.app", "e1")
    client.update_track("com.app", "e1", "internal", [])
    client.validate_edit("com.app", "e1")
    client.commit_edit("com.app", "e1")

    for _label, kwargs in service.recorder.calls:
        assert "changesNotSentForReview" not in kwargs


# -- error normalization and redaction -------------------------------------------


def test_google_play_error_summary_includes_stage_and_http_status():
    error = google_play.GooglePlayError(google_play.STAGE_EDIT_COMMIT, "Backend Error", http_status=503)
    assert error.summary() == "Google Play edit_commit failed (HTTP 503): Backend Error"
    assert str(error) == error.summary()


def test_access_error_is_normalized_with_http_code_and_safe_reason(fake_parts):
    client = fake_parts.make_client()
    fake_parts.service.plan(
        "edits.tracks.update",
        error=http_error(
            403,
            "Forbidden",
            {
                "error": {
                    "code": 403,
                    "message": "The current user has insufficient permissions to view the app.",
                    "errors": [{"reason": "forbidden"}],
                }
            },
        ),
    )

    with pytest.raises(google_play.GooglePlayError) as excinfo:
        client.update_track("com.example.app", "edit-1", "internal", [])

    error = excinfo.value
    assert error.stage == google_play.STAGE_TRACK_UPDATE
    assert error.http_status == 403
    assert "insufficient permissions" in error.reason
    assert "Authorization" not in str(error)


def test_track_not_found_keeps_http_status_404(fake_parts):
    client = fake_parts.make_client()
    fake_parts.service.plan(
        "edits.tracks.get",
        error=http_error(404, "Not Found", {"error": {"message": "No track found for track name: qa."}}),
    )

    with pytest.raises(google_play.GooglePlayError) as excinfo:
        client.get_track("com.example.app", "edit-1", "qa")

    assert excinfo.value.http_status == 404
    assert "qa" in excinfo.value.reason


def test_sanitize_reason_redacts_tokens_assignments_and_urls():
    redacted = google_play._sanitize_reason(
        "Failure calling endpoint: Bearer ya29.secret-token access_token=ya29.other "
        "authorization: Basic dXNlcjpwYXNz "
        "session https://androidpublisher.googleapis.com/upload/session/v2?upload_id=TOPSECRET failed"
    )
    assert "ya29" not in redacted
    assert "TOPSECRET" not in redacted
    assert "dXNlcjpwYXNz" not in redacted
    assert "[redacted]" in redacted


def test_sanitize_reason_truncates_long_text():
    long_reason = "x" * 2000
    sanitized = google_play._sanitize_reason(long_reason)
    assert len(sanitized) <= google_play._MAX_REASON_LENGTH + 3


def test_sanitize_reason_handles_empty_input():
    assert google_play._sanitize_reason("") == "unknown error"
    assert google_play._sanitize_reason("   ") == "unknown error"


# -- AAB hashing ------------------------------------------------------------------


def test_compute_file_sha256_matches_known_vector(tmp_path):
    path = tmp_path / "blob.bin"
    path.write_bytes(b"abc")
    assert google_play.compute_file_sha256(path) == hashlib.sha256(b"abc").hexdigest()


def test_compute_file_sha256_streams_files_larger_than_one_chunk(tmp_path):
    payload = (b"cdt-aab-payload-" * 100_000)[: google_play.UPLOAD_CHUNK_BYTES + 1]
    path = tmp_path / "big.aab"
    path.write_bytes(payload)
    assert google_play.compute_file_sha256(path) == hashlib.sha256(payload).hexdigest()


def test_compute_file_sha256_missing_file_raises_oserror(tmp_path):
    with pytest.raises(OSError):
        google_play.compute_file_sha256(tmp_path / "missing.aab")


# -- resumable AAB upload ---------------------------------------------------------


def test_upload_bundle_streams_resumable_with_dedicated_timeout(fake_parts, tmp_path):
    client = fake_parts.make_client()
    aab = tmp_path / "app-release.aab"
    aab.write_bytes(b"A" * (google_play.UPLOAD_CHUNK_BYTES + 1))
    fake_parts.service.upload_chunks = [
        ("progress", None),
        ("done", {"versionCode": 42, "sha256": "abc123"}),
    ]

    response = client.upload_bundle("com.example.app", "edit-1", aab)

    assert response == {"versionCode": 42, "sha256": "abc123"}
    upload_request = fake_parts.service.upload_request
    assert upload_request is not None
    # Multi-chunk streaming: one request per chunk, no retries, dedicated upload http.
    assert [call["num_retries"] for call in upload_request.calls] == [0, 0]
    assert all(call["http"] is client.upload_http for call in upload_request.calls)
    media: MediaFileUpload = upload_request.media_body
    assert media.resumable() is True
    assert media.mimetype() == "application/octet-stream"
    assert media.chunksize() == google_play.UPLOAD_CHUNK_BYTES
    assert media.size() == aab.stat().st_size


def test_upload_bundle_failure_is_not_retried(fake_parts, tmp_path):
    client = fake_parts.make_client()
    aab = tmp_path / "app-release.aab"
    aab.write_bytes(b"A" * 1024)
    fake_parts.service.upload_chunks = [
        ("error", http_error(503, "Backend Error", {"error": {"message": "Backend Error"}})),
        ("done", {"versionCode": 42}),
    ]

    with pytest.raises(google_play.GooglePlayError) as excinfo:
        client.upload_bundle("com.example.app", "edit-1", aab)

    assert excinfo.value.http_status == 503
    assert len(fake_parts.service.upload_request.calls) == 1


def test_upload_bundle_error_response_never_leaks_session_url(fake_parts, tmp_path):
    client = fake_parts.make_client()
    aab = tmp_path / "app-release.aab"
    aab.write_bytes(b"A" * 1024)
    fake_parts.service.upload_chunks = [
        (
            "error",
            http_error(
                500,
                "Internal Error",
                {
                    "error": {
                        "message": (
                            "Upload failed, resume at "
                            "https://androidpublisher.googleapis.com/upload/session/v2?upload_id=SECRETSESSIONID"
                        )
                    }
                },
            ),
        ),
    ]

    with pytest.raises(google_play.GooglePlayError) as excinfo:
        client.upload_bundle("com.example.app", "edit-1", aab)

    assert "SECRETSESSIONID" not in str(excinfo.value)
    assert "androidpublisher.googleapis.com/upload" not in str(excinfo.value)


def test_upload_bundle_missing_file_is_normalized_without_api_call(fake_parts, tmp_path):
    client = fake_parts.make_client()

    with pytest.raises(google_play.GooglePlayError) as excinfo:
        client.upload_bundle("com.example.app", "edit-1", tmp_path / "missing.aab")

    assert excinfo.value.stage == google_play.STAGE_BUNDLE_UPLOAD
    assert "cannot read AAB" in excinfo.value.reason
    assert fake_parts.service.recorder.calls == []


def test_auth_failure_during_upload_is_normalized(fake_parts, tmp_path):
    client = fake_parts.make_client()
    aab = tmp_path / "app-release.aab"
    aab.write_bytes(b"A" * 1024)
    fake_parts.service.upload_chunks = [
        ("error", google_auth_exceptions.RefreshError("token expired")),
    ]

    with pytest.raises(google_play.GooglePlayError) as excinfo:
        client.upload_bundle("com.example.app", "edit-1", aab)

    assert excinfo.value.stage == google_play.STAGE_BUNDLE_UPLOAD
    assert "token" not in excinfo.value.reason
