import json
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


def test_asc_request_returns_json_and_builds_authorized_request(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout):
        calls.append((req, timeout))
        return FakeHTTPResponse(b'{"data": [{"id": "1"}]}')

    monkeypatch.setattr(appstore.urllib.request, "urlopen", fake_urlopen)

    result = appstore._asc_request("POST", "/v1/example", "token", {"hello": "world"})

    assert result == {"data": [{"id": "1"}]}
    req, timeout = calls[0]
    assert timeout == 60
    assert req.full_url == appstore.ASC_API_BASE + "/v1/example"
    assert req.get_method() == "POST"
    assert req.headers["Authorization"] == "Bearer token"
    assert json.loads(req.data.decode("utf-8")) == {"hello": "world"}


def test_asc_request_returns_empty_dict_for_empty_body(monkeypatch):
    monkeypatch.setattr(appstore.urllib.request, "urlopen", lambda req, timeout: FakeHTTPResponse())

    assert appstore._asc_request("GET", "/v1/example", "token") == {}


def test_asc_request_wraps_http_error(monkeypatch):
    def fake_urlopen(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, FakeHTTPResponse(b"nope"))

    monkeypatch.setattr(appstore.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(typer.BadParameter, match="App Store Connect API error 401: nope"):
        appstore._asc_request("GET", "/v1/example", "token")


def test_asc_get_app_id_returns_first_app_id(monkeypatch):
    calls = []

    def fake_request(method, path, token, payload=None):
        calls.append((method, path, token, payload))
        return {"data": [{"id": "app-123"}]}

    monkeypatch.setattr(appstore, "_asc_request", fake_request)

    assert appstore._asc_get_app_id("com.example.app", "token") == "app-123"
    assert calls == [("GET", "/v1/apps?filter[bundleId]=com.example.app&limit=1", "token", None)]


def test_asc_get_app_id_reports_missing_app(monkeypatch):
    monkeypatch.setattr(appstore, "_asc_request", lambda method, path, token, payload=None: {"data": []})

    with pytest.raises(typer.BadParameter, match="App not found"):
        appstore._asc_get_app_id("com.example.missing", "token")


def test_asc_set_changelog_patches_existing_localization(monkeypatch):
    calls = []

    def fake_request(method, path, token, payload=None):
        calls.append((method, path, token, payload))
        if method == "GET":
            return {"data": [{"id": "loc-1"}]}
        return {}

    monkeypatch.setattr(appstore, "_asc_request", fake_request)

    appstore._asc_set_changelog("build-1", "Changed", "token")

    assert calls[0][0] == "GET"
    assert calls[1] == (
        "PATCH",
        "/v1/betaBuildLocalizations/loc-1",
        "token",
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

    def fake_request(method, path, token, payload=None):
        calls.append((method, path, token, payload))
        if method == "GET":
            return {"data": []}
        return {}

    monkeypatch.setattr(appstore, "_asc_request", fake_request)

    appstore._asc_set_changelog("build-1", "Changed", "token")

    assert calls[1][0] == "POST"
    assert calls[1][1] == "/v1/betaBuildLocalizations"
    assert calls[1][3]["data"]["relationships"]["build"]["data"] == {"type": "builds", "id": "build-1"}


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
    monkeypatch.setattr(appstore, "_asc_get_app_id", lambda bundle_id, token: "app-1")
    monkeypatch.setattr(
        appstore, "_asc_wait_build", lambda app_id, build_number, token, timeout_sec: ("build-1", "VALID")
    )
    monkeypatch.setattr(
        appstore,
        "_asc_set_changelog",
        lambda build_id, changelog, token: calls.append((build_id, changelog, token)),
    )

    assert appstore._complete_testflight_after_upload(
        {"IOS_BUNDLE_ID": "com.example.app", "ASC_WAIT_TIMEOUT_SEC": "5"}, "Changed", "1.0+7"
    ) == 0
    assert calls == [("build-1", "Changed", "token")]


def test_complete_testflight_after_upload_rejects_failed_processing(monkeypatch):
    monkeypatch.setattr(appstore, "_asc_token", lambda env: "token")
    monkeypatch.setattr(appstore, "_asc_get_app_id", lambda bundle_id, token: "app-1")
    monkeypatch.setattr(
        appstore, "_asc_wait_build", lambda app_id, build_number, token, timeout_sec: ("build-1", "FAILED")
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
