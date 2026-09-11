import email.utils
import errno
import http.client
import json
import socket
import ssl
import urllib.error
from pathlib import Path

import pytest
import typer

from cdt.services import appstore


class FakeHTTPResponse:
    def __init__(self, body: bytes = b""):
        self._body = body

    def read(self):
        return self._body

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _http_error(code: int, body: bytes = b"", headers: dict | None = None) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://api.example.test", code, "error", headers or {}, FakeHTTPResponse(body))


def _stub_client(monkeypatch) -> appstore._AscClient:
    """Client with a stubbed JWT generator producing token0, token1, ..."""
    generated = []

    def fake_token(env):
        generated.append(1)
        return f"token{len(generated) - 1}"

    monkeypatch.setattr(appstore, "_asc_token", fake_token)
    return appstore._AscClient({})


def _urlopen_with_outcomes(monkeypatch, outcomes: list, calls: list) -> None:
    """Stub urlopen that replays ``outcomes``: exceptions are raised, others returned."""

    def fake_urlopen(req, timeout):
        calls.append(req)
        outcome = outcomes.pop(0) if outcomes else FakeHTTPResponse(b"{}")
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(appstore.urllib.request, "urlopen", fake_urlopen)


def test_asc_token_requires_all_credentials():
    with pytest.raises(typer.BadParameter, match="Missing ASC credentials"):
        appstore._asc_token({})


def test_asc_token_encodes_expected_claims(tmp_path, monkeypatch):
    key = tmp_path / "AuthKey.p8"
    key.write_text("private", encoding="utf-8")
    captured = {}

    def fake_encode(claims, private_key, **kwargs):
        captured.update({"claims": claims, "private_key": private_key, **kwargs})
        return "token"

    monkeypatch.setattr(appstore.jwt, "encode", fake_encode)
    monkeypatch.setattr(appstore.time, "time", lambda: 100)

    token = appstore._asc_token(
        {"ASC_KEY_ID": "key", "ASC_ISSUER_ID": "issuer", "ASC_PRIVATE_KEY_PATH": str(key)}
    )

    assert token == "token"
    assert captured["claims"] == {"iss": "issuer", "aud": "appstoreconnect-v1", "exp": 1300}
    assert captured["algorithm"] == "ES256"
    assert captured["headers"] == {"kid": "key", "typ": "JWT"}


def test_asc_token_requires_existing_private_key(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    with pytest.raises(typer.BadParameter, match="ASC private key not found"):
        appstore._asc_token(
            {"ASC_KEY_ID": "key", "ASC_ISSUER_ID": "issuer", "ASC_PRIVATE_KEY_PATH": "AuthKey.p8"}
        )


def test_asc_client_caches_generated_token(monkeypatch):
    client = _stub_client(monkeypatch)

    assert client.token() == "token0"
    assert client.token() == "token0"


def test_asc_client_refreshes_token_before_expiry(monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(appstore.time, "time", lambda: clock["now"])
    generated = []

    def fake_token(env):
        generated.append(clock["now"])
        return f"token{len(generated)}"

    monkeypatch.setattr(appstore, "_asc_token", fake_token)
    client = appstore._AscClient({})

    assert client.token() == "token1"
    clock["now"] = 1000 + appstore.ASC_JWT_TTL_SEC - appstore.ASC_JWT_REFRESH_MARGIN_SEC - 1
    assert client.token() == "token1"  # still inside the refresh margin
    clock["now"] = 1000 + appstore.ASC_JWT_TTL_SEC - appstore.ASC_JWT_REFRESH_MARGIN_SEC
    assert client.token() == "token2"  # refreshed before the 20 minute expiry
    assert generated == [1000.0, clock["now"]]


def test_asc_client_force_token_refresh_generates_new_token(monkeypatch):
    client = _stub_client(monkeypatch)

    assert client.token() == "token0"
    client.force_token_refresh()
    assert client.token() == "token1"


def test_asc_request_returns_json_and_builds_authorized_request(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout):
        calls.append((req, timeout))
        return FakeHTTPResponse(b'{"data": [{"id": "1"}]}')

    monkeypatch.setattr(appstore.urllib.request, "urlopen", fake_urlopen)
    client = _stub_client(monkeypatch)

    result = appstore._asc_request("POST", "/v1/example", client, {"hello": "world"})

    assert result == {"data": [{"id": "1"}]}
    req, timeout = calls[0]
    assert timeout == appstore.ASC_REQUEST_TIMEOUT_SEC
    assert req.full_url == appstore.ASC_API_BASE + "/v1/example"
    assert req.get_method() == "POST"
    assert req.headers["Authorization"] == "Bearer token0"
    assert json.loads(req.data.decode("utf-8")) == {"hello": "world"}


def test_asc_request_returns_empty_dict_for_empty_body(monkeypatch):
    _urlopen_with_outcomes(monkeypatch, [FakeHTTPResponse()], [])

    assert appstore._asc_request("GET", "/v1/example", _stub_client(monkeypatch)) == {}


def test_asc_request_forces_single_jwt_refresh_on_401(monkeypatch):
    requests = []
    outcomes = [_http_error(401, b"unauthorized"), FakeHTTPResponse(b'{"data": []}')]

    def fake_urlopen(req, timeout):
        requests.append(req.headers["Authorization"])
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(appstore.urllib.request, "urlopen", fake_urlopen)
    client = _stub_client(monkeypatch)

    assert appstore._asc_request("GET", "/v1/example", client) == {"data": []}
    assert requests == ["Bearer token0", "Bearer token1"]


def test_asc_request_second_401_fails_without_new_refresh(monkeypatch):
    requests = []
    token_calls = []

    def fake_token(env):
        token_calls.append(1)
        return f"token{len(token_calls)}"

    monkeypatch.setattr(appstore, "_asc_token", fake_token)

    def fake_urlopen(req, timeout):
        requests.append(req)
        raise _http_error(401, b"denied")

    monkeypatch.setattr(appstore.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(typer.BadParameter, match="API error 401: denied"):
        appstore._asc_request("GET", "/v1/example", appstore._AscClient({}))

    assert len(requests) == 2  # initial request + one retry after forced refresh
    assert len(token_calls) == 2  # initial JWT + one forced refresh, nothing more


def test_asc_request_does_not_retry_permanent_4xx(monkeypatch):
    calls = []
    _urlopen_with_outcomes(monkeypatch, [_http_error(404, b"not found")], calls)

    with pytest.raises(typer.BadParameter, match="API error 404: not found"):
        appstore._asc_request("GET", "/v1/example", _stub_client(monkeypatch))

    assert len(calls) == 1


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("timed out"),
        socket.timeout("timed out"),
        ssl.SSLEOFError("EOF occurred in violation of protocol"),
        ssl.SSLError("bad handshake"),
        ConnectionResetError(54, "Connection reset by peer"),
        ConnectionAbortedError(53, "Software caused connection abort"),
        http.client.RemoteDisconnected("Remote end closed connection without response"),
        urllib.error.URLError(OSError(errno.ECONNRESET, "Connection reset by peer")),
        urllib.error.URLError("[Errno 8] nodename nor servname provided"),
        OSError(errno.ECONNRESET, "Connection reset by peer"),
        OSError(errno.ETIMEDOUT, "Operation timed out"),
    ],
)
def test_asc_request_retries_transient_failures(monkeypatch, failure):
    calls = []
    sleeps = []
    _urlopen_with_outcomes(monkeypatch, [failure, FakeHTTPResponse(b'{"ok": true}')], calls)
    monkeypatch.setattr(appstore.time, "sleep", sleeps.append)

    result = appstore._asc_request("GET", "/v1/example", _stub_client(monkeypatch))

    assert result == {"ok": True}
    assert len(calls) == 2
    assert len(sleeps) == 1
    assert appstore.ASC_RETRY_BASE_DELAY_SEC <= sleeps[0] <= appstore.ASC_RETRY_MAX_DELAY_SEC


def test_asc_request_fails_fast_for_non_transient_os_error(monkeypatch):
    calls = []
    _urlopen_with_outcomes(monkeypatch, [OSError(errno.EACCES, "Permission denied")], calls)

    with pytest.raises(typer.BadParameter, match="request failed: PermissionError"):
        appstore._asc_request("GET", "/v1/example", _stub_client(monkeypatch))

    assert len(calls) == 1


def test_asc_request_raises_exhaustion_after_retry_budget(monkeypatch):
    calls = []
    sleeps = []
    _urlopen_with_outcomes(monkeypatch, [TimeoutError("timed out")] * 10, calls)
    monkeypatch.setattr(appstore.time, "sleep", sleeps.append)

    with pytest.raises(typer.BadParameter) as excinfo:
        appstore._asc_request("GET", "/v1/example", _stub_client(monkeypatch))

    message = str(excinfo.value)
    assert f"failed after {appstore.ASC_RETRY_MAX_ATTEMPTS} attempts" in message
    assert "timeout: timed out" in message
    assert len(calls) == appstore.ASC_RETRY_MAX_ATTEMPTS
    assert len(sleeps) == appstore.ASC_RETRY_MAX_ATTEMPTS - 1


def test_asc_request_logs_retry_diagnostics_without_secrets(monkeypatch, capsys):
    calls = []
    _urlopen_with_outcomes(monkeypatch, [TimeoutError("timed out"), FakeHTTPResponse(b"{}")], calls)
    monkeypatch.setattr(appstore.time, "sleep", lambda seconds: None)

    appstore._asc_request("GET", "/v1/example", _stub_client(monkeypatch))

    output = capsys.readouterr().out
    assert "attempt 1/4" in output
    assert "timeout" in output
    assert "Bearer" not in output
    assert "token0" not in output


def test_asc_request_retries_429_and_5xx(monkeypatch):
    calls = []
    sleeps = []
    _urlopen_with_outcomes(
        monkeypatch,
        [_http_error(429, b"slow down"), _http_error(503, b"unavailable"), FakeHTTPResponse(b'{"ok": 1}')],
        calls,
    )
    monkeypatch.setattr(appstore.time, "sleep", sleeps.append)

    result = appstore._asc_request("GET", "/v1/example", _stub_client(monkeypatch))

    assert result == {"ok": 1}
    assert len(calls) == 3
    assert len(sleeps) == 2


def test_asc_request_caps_retry_after_seconds_delay(monkeypatch):
    calls = []
    sleeps = []
    _urlopen_with_outcomes(
        monkeypatch,
        [_http_error(429, b"slow down", headers={"Retry-After": "120"}), FakeHTTPResponse(b"{}")],
        calls,
    )
    monkeypatch.setattr(appstore.time, "sleep", sleeps.append)

    appstore._asc_request("GET", "/v1/example", _stub_client(monkeypatch))

    assert sleeps == [appstore.ASC_RETRY_MAX_DELAY_SEC]


def test_asc_request_honors_retry_after_http_date(monkeypatch):
    now = 1_700_000_000.0
    monkeypatch.setattr(appstore.time, "time", lambda: now)
    calls = []
    sleeps = []
    _urlopen_with_outcomes(
        monkeypatch,
        [
            _http_error(503, b"later", headers={"Retry-After": email.utils.formatdate(now + 3, usegmt=True)}),
            FakeHTTPResponse(b"{}"),
        ],
        calls,
    )
    monkeypatch.setattr(appstore.time, "sleep", sleeps.append)

    appstore._asc_request("GET", "/v1/example", _stub_client(monkeypatch))

    assert sleeps == [3.0]


def test_asc_request_uses_backoff_when_retry_after_missing(monkeypatch):
    calls = []
    sleeps = []
    _urlopen_with_outcomes(monkeypatch, [_http_error(429, b"slow down"), FakeHTTPResponse(b"{}")], calls)
    monkeypatch.setattr(appstore.time, "sleep", sleeps.append)

    appstore._asc_request("GET", "/v1/example", _stub_client(monkeypatch))

    assert (
        appstore.ASC_RETRY_BASE_DELAY_SEC
        <= sleeps[0]
        <= appstore.ASC_RETRY_BASE_DELAY_SEC + appstore.ASC_RETRY_JITTER_SEC
    )


def test_asc_request_retry_does_not_sleep_past_deadline(monkeypatch):
    calls = []
    sleeps = []
    _urlopen_with_outcomes(monkeypatch, [urllib.error.URLError(OSError(errno.ECONNRESET, "reset"))], calls)
    monkeypatch.setattr(appstore.time, "time", lambda: 2000.0)
    monkeypatch.setattr(appstore.time, "sleep", sleeps.append)
    client = _stub_client(monkeypatch)
    client.deadline = 1000.0  # already in the past

    appstore._asc_request("GET", "/v1/example", client)

    assert sleeps == [0.0]


def test_asc_retry_after_seconds_formats():
    now = 1_700_000_000.0
    assert appstore._asc_retry_after_seconds(None) is None
    assert appstore._asc_retry_after_seconds("") is None
    assert appstore._asc_retry_after_seconds("  7 ") == 7.0
    assert appstore._asc_retry_after_seconds("garbage") is None
    assert appstore._asc_retry_after_seconds(email.utils.formatdate(now - 30, usegmt=True), now=now) == 0.0
    assert appstore._asc_retry_after_seconds(email.utils.formatdate(now + 90, usegmt=True), now=now) == 90.0


def test_asc_retry_delay_exponential_backoff_with_zero_jitter(monkeypatch):
    monkeypatch.setattr(appstore.random, "uniform", lambda low, high: 0.0)

    assert appstore._asc_retry_delay(1) == 1.0
    assert appstore._asc_retry_delay(2) == 2.0
    assert appstore._asc_retry_delay(3) == 4.0
    assert appstore._asc_retry_delay(4) == 8.0
    assert appstore._asc_retry_delay(10) == appstore.ASC_RETRY_MAX_DELAY_SEC  # upper bound


def test_asc_retry_delay_bounds_and_retry_after_handling():
    for attempt in range(1, 8):
        delay = appstore._asc_retry_delay(attempt)
        assert 0.0 < delay <= appstore.ASC_RETRY_MAX_DELAY_SEC
    # Retry-After is respected but capped by the same upper bound.
    assert appstore._asc_retry_delay(1, 120.0) == appstore.ASC_RETRY_MAX_DELAY_SEC
    assert (
        appstore.ASC_RETRY_BASE_DELAY_SEC
        <= appstore._asc_retry_delay(1, 0.2)
        <= appstore.ASC_RETRY_BASE_DELAY_SEC + appstore.ASC_RETRY_JITTER_SEC
    )


def test_asc_retry_wait_clamps_to_deadline():
    assert appstore._asc_retry_wait(10.0, None) == 10.0
    assert appstore._asc_retry_wait(10.0, 1000.0, now=995.0) == 5.0
    assert appstore._asc_retry_wait(2.0, 1000.0, now=995.0) == 2.0
    assert appstore._asc_retry_wait(10.0, 1000.0, now=1000.0) == 0.0
    assert appstore._asc_retry_wait(10.0, 1000.0, now=1001.0) == 0.0


def test_asc_get_app_id_returns_first_app_id(monkeypatch):
    calls = []

    def fake_request(method, path, client, payload=None):
        calls.append((method, path, payload))
        return {"data": [{"id": "app-123"}]}

    monkeypatch.setattr(appstore, "_asc_request", fake_request)

    assert appstore._asc_get_app_id("com.example.app", _stub_client(monkeypatch)) == "app-123"
    assert calls == [("GET", "/v1/apps?filter[bundleId]=com.example.app&limit=1", None)]


def test_asc_get_app_id_reports_missing_app(monkeypatch):
    monkeypatch.setattr(appstore, "_asc_request", lambda method, path, client, payload=None: {"data": []})

    with pytest.raises(typer.BadParameter, match="App not found"):
        appstore._asc_get_app_id("com.example.missing", _stub_client(monkeypatch))


def test_asc_wait_build_sets_client_deadline(monkeypatch):
    seen = {}

    def fake_request(method, path, client, payload=None):
        seen["deadline"] = client.deadline
        return {"data": [{"id": "build-1", "attributes": {"processingState": "VALID"}}]}

    monkeypatch.setattr(appstore, "_asc_request", fake_request)
    client = appstore._AscClient({})

    assert appstore._asc_wait_build("app-1", "42", client, timeout_sec=5) == ("build-1", "VALID")
    assert seen["deadline"] is not None
    assert seen["deadline"] - appstore.time.time() <= 5


def test_asc_set_changelog_patches_existing_localization(monkeypatch):
    calls = []

    def fake_request(method, path, client, payload=None):
        calls.append((method, path, payload))
        if method == "GET":
            return {"data": [{"id": "loc-1"}]}
        return {}

    monkeypatch.setattr(appstore, "_asc_request", fake_request)

    appstore._asc_set_changelog("build-1", "Changed", _stub_client(monkeypatch))

    assert calls[0][0] == "GET"
    assert calls[1] == (
        "PATCH",
        "/v1/betaBuildLocalizations/loc-1",
        {
            "data": {
                "type": "betaBuildLocalizations",
                "id": "loc-1",
                "attributes": {"whatsNew": "Changed"},
            }
        },
    )


def test_asc_set_changelog_creates_localization(monkeypatch):
    calls = []

    def fake_request(method, path, client, payload=None):
        calls.append((method, path, payload))
        if method == "GET":
            return {"data": []}
        return {}

    monkeypatch.setattr(appstore, "_asc_request", fake_request)

    appstore._asc_set_changelog("build-1", "Changed", _stub_client(monkeypatch))

    assert calls[1][0] == "POST"
    assert calls[1][1] == "/v1/betaBuildLocalizations"
    assert calls[1][2]["data"]["relationships"]["build"]["data"] == {"type": "builds", "id": "build-1"}


def test_complete_testflight_after_upload_requires_bundle_id(monkeypatch):
    monkeypatch.setattr(appstore, "_asc_token", lambda env: "token")

    with pytest.raises(typer.BadParameter, match="Missing IOS_BUNDLE_ID"):
        appstore._complete_testflight_after_upload({}, "log", "1.0+1")


def test_complete_testflight_after_upload_rejects_invalid_timeout(monkeypatch):
    monkeypatch.setattr(appstore, "_asc_token", lambda env: "token")

    with pytest.raises(typer.BadParameter, match="ASC_WAIT_TIMEOUT_SEC must be an integer"):
        appstore._complete_testflight_after_upload(
            {"IOS_BUNDLE_ID": "com.example.app", "ASC_WAIT_TIMEOUT_SEC": "soon"}, "log", "1.0+1"
        )


def test_complete_testflight_after_upload_sets_changelog_for_valid_build(monkeypatch):
    calls = []
    monkeypatch.setattr(appstore, "_asc_token", lambda env: "token")
    monkeypatch.setattr(appstore, "_asc_get_app_id", lambda bundle_id, client: "app-1")
    monkeypatch.setattr(
        appstore, "_asc_wait_build", lambda app_id, build_number, client, timeout_sec: ("build-1", "VALID")
    )
    monkeypatch.setattr(
        appstore,
        "_asc_set_changelog",
        lambda build_id, changelog, client: calls.append((build_id, changelog, client)),
    )

    assert appstore._complete_testflight_after_upload(
        {"IOS_BUNDLE_ID": "com.example.app", "ASC_WAIT_TIMEOUT_SEC": "5"}, "Changed", "1.0+7"
    ) == 0
    assert calls[0][0] == "build-1"
    assert calls[0][1] == "Changed"
    assert isinstance(calls[0][2], appstore._AscClient)


def test_complete_testflight_after_upload_rejects_failed_processing(monkeypatch):
    monkeypatch.setattr(appstore, "_asc_token", lambda env: "token")
    monkeypatch.setattr(appstore, "_asc_get_app_id", lambda bundle_id, client: "app-1")
    monkeypatch.setattr(
        appstore, "_asc_wait_build", lambda app_id, build_number, client, timeout_sec: ("build-1", "FAILED")
    )

    with pytest.raises(typer.BadParameter, match="Build processing ended with state: FAILED"):
        appstore._complete_testflight_after_upload({"IOS_BUNDLE_ID": "com.example.app"}, "Changed", "1.0+7")


def test_ensure_transporter_reports_missing_xcrun(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(appstore.subprocess, "run", missing)

    with pytest.raises(typer.BadParameter, match="xcrun is not available"):
        appstore._ensure_itmstransporter_available()


def test_upload_testflight_does_not_poll_after_transporter_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(appstore, "_build_testflight_transporter_command", lambda path, env: ["upload"])
    monkeypatch.setattr(appstore, "_run", lambda command, cwd: 7)
    monkeypatch.setattr(
        appstore,
        "_complete_testflight_after_upload",
        lambda *args: (_ for _ in ()).throw(AssertionError("must not poll")),
    )

    assert appstore._upload_testflight(tmp_path / "app.ipa", {}, "notes", "1.0+1") == 7


def test_build_testflight_transporter_command_uses_key_directory(tmp_path, monkeypatch):
    key = tmp_path / "keys" / "AuthKey.p8"
    key.parent.mkdir()
    key.write_text("key", encoding="utf-8")
    monkeypatch.setattr(appstore, "_ensure_itmstransporter_available", lambda: None)

    command = appstore._build_testflight_transporter_command(
        Path("build/app.ipa"),
        {"ASC_KEY_ID": "key-id", "ASC_ISSUER_ID": "issuer", "ASC_PRIVATE_KEY_PATH": str(key)},
    )

    assert command == [
        "xcrun",
        "iTMSTransporter",
        "-m",
        "upload",
        "-assetFile",
        "build/app.ipa",
        "-apiKey",
        "key-id",
        "-apiIssuer",
        "issuer",
        "-apiKeyPath",
        str(key.parent),
    ]
