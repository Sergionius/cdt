"""Tests for the safe generic webhook service (notify.webhook).

All HTTP traffic is mocked; no test performs real network calls.
"""

import json
import socket
import ssl
import urllib.error
import urllib.parse

import pytest
import typer

from cdt.services import webhook
from cdt.services.webhook import (
    ENV_KEY_RE,
    WebhookError,
    send_webhook,
    validate_env_key_name,
    validate_fail_on_error,
    validate_payload_object,
    validate_timeout_seconds,
)


class FakeResponse:
    def __init__(self, status: int = 200):
        self.status = status
        self.read_calls = 0

    def read(self):
        self.read_calls += 1
        return b"secret provider body"

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def patch_transport(monkeypatch, handler=None):
    """Mock the single-POST transport seam and return the recorded calls."""
    calls = []

    def fake_open(request, timeout):
        calls.append((request, timeout))
        if handler is None:
            return FakeResponse()
        return handler(request, timeout)

    monkeypatch.setattr(webhook, "_open_webhook_response", fake_open)
    return calls


def http_error(status: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://hooks.example/abc",
        status,
        "reason",
        hdrs=None,
        fp=None,  # noqa: S310 - test value
    )


# --- option validation -----------------------------------------------------


def test_env_key_name_must_be_a_plain_name():
    assert validate_env_key_name("MY_WEBHOOK_URL", "url_env") == "MY_WEBHOOK_URL"

    for bad in ("", "   ", None, 5, "MY URL", "MY-URL", "${inputs.hook}", "A.B"):
        with pytest.raises(typer.BadParameter, match="url_env"):
            validate_env_key_name(bad, "url_env")


def test_payload_must_be_a_non_empty_object():
    assert validate_payload_object({"text": "hi"}) == {"text": "hi"}

    for bad in ([], "text", None, 5, {}):
        with pytest.raises(typer.BadParameter, match="payload"):
            validate_payload_object(bad)


def test_timeout_must_be_positive_and_finite():
    assert validate_timeout_seconds(30) == 30.0
    assert validate_timeout_seconds(0.5) == 0.5

    for bad in (0, -1, True, "10", float("inf"), float("nan"), None):
        with pytest.raises(typer.BadParameter, match="timeout_seconds"):
            validate_timeout_seconds(bad)


def test_fail_on_error_must_be_boolean():
    assert validate_fail_on_error(True) is True
    assert validate_fail_on_error(False) is False

    for bad in ("yes", 1, None):
        with pytest.raises(typer.BadParameter, match="fail_on_error"):
            validate_fail_on_error(bad)


def test_env_key_pattern_rejects_interpolation_and_punctuation():
    assert ENV_KEY_RE.fullmatch("SLACK_HOOK_URL")
    assert ENV_KEY_RE.fullmatch("_private2")
    assert not ENV_KEY_RE.fullmatch("9START")
    assert not ENV_KEY_RE.fullmatch("WITH SPACE")
    assert not ENV_KEY_RE.fullmatch("WITH-DASH")
    assert not ENV_KEY_RE.fullmatch("${inputs.hook_env}")


# --- delivery ----------------------------------------------------------


def test_send_webhook_posts_json_once_without_retries(monkeypatch):
    calls = patch_transport(monkeypatch)

    status = send_webhook(
        {"WEBHOOK_URL": "https://hooks.example/abc"},
        url_env="WEBHOOK_URL",
        payload={"text": "hello"},
    )

    assert status == 200
    assert len(calls) == 1  # exactly one POST, no automatic retries
    request, timeout = calls[0]
    assert timeout == webhook.DEFAULT_WEBHOOK_TIMEOUT_SECONDS == 30.0
    assert request.get_method() == "POST"
    assert request.full_url == "https://hooks.example/abc"
    assert request.headers["Content-type"] == "application/json"
    assert "Authorization" not in request.headers
    assert json.loads(request.data.decode("utf-8")) == {"text": "hello"}


def test_send_webhook_uses_explicit_timeout_and_full_authorization_value(monkeypatch):
    calls = patch_transport(monkeypatch)

    send_webhook(
        {"WEBHOOK_URL": "https://hooks.example/abc", "WEBHOOK_AUTH": "Bearer token-value-123"},
        url_env="WEBHOOK_URL",
        payload={"text": "hello"},
        authorization_env="WEBHOOK_AUTH",
        timeout_seconds=7.5,
    )

    request, timeout = calls[0]
    assert timeout == 7.5
    # The full env value becomes the Authorization header; nothing is added.
    assert request.headers["Authorization"] == "Bearer token-value-123"


def test_missing_destination_env_is_a_configuration_error(monkeypatch):
    calls = patch_transport(monkeypatch)

    with pytest.raises(typer.BadParameter, match="WEBHOOK_URL"):
        send_webhook({}, url_env="WEBHOOK_URL", payload={"text": "hi"})

    assert calls == []


def test_missing_authorization_env_is_a_configuration_error(monkeypatch):
    calls = patch_transport(monkeypatch)

    with pytest.raises(typer.BadParameter, match="WEBHOOK_AUTH"):
        send_webhook(
            {"WEBHOOK_URL": "https://hooks.example/abc"},
            url_env="WEBHOOK_URL",
            payload={"text": "hi"},
            authorization_env="WEBHOOK_AUTH",
        )

    assert calls == []


@pytest.mark.parametrize(
    "url",
    [
        "http://hooks.example/abc",  # not https
        "https:///path-only",  # no host
        "https://user:pass@hooks.example/abc",  # userinfo
        "https://hooks.example/abc#fragment",  # fragment
        "https://hooks.example:bad/abc",  # invalid port
    ],
)
def test_destination_must_be_https_with_host_without_userinfo_or_fragment(monkeypatch, url):
    calls = patch_transport(monkeypatch)

    with pytest.raises(typer.BadParameter, match="destination"):
        send_webhook({"WEBHOOK_URL": url}, url_env="WEBHOOK_URL", payload={"text": "hi"})

    assert calls == []


def test_only_2xx_status_is_successful(monkeypatch):
    patch_transport(monkeypatch, handler=lambda request, timeout: FakeResponse(status=204))
    assert (
        send_webhook({"WEBHOOK_URL": "https://hooks.example/abc"}, url_env="WEBHOOK_URL", payload={"ok": True}) == 204
    )

    for status in (199, 301, 302, 400, 404, 500):
        patch_transport(monkeypatch, handler=lambda request, timeout, code=status: FakeResponse(status=code))
        with pytest.raises(WebhookError, match="http_error") as excinfo:
            send_webhook({"WEBHOOK_URL": "https://hooks.example/abc"}, url_env="WEBHOOK_URL", payload={"t": 1})
        assert excinfo.value.status == status


def test_transport_errors_become_safe_categories(monkeypatch):
    cases = [
        (urllib.error.URLError("getaddrinfo failed"), "network_error"),
        (urllib.error.URLError(socket.timeout()), "timeout"),
        (TimeoutError(), "timeout"),
        (ssl.SSLError("certificate verify failed"), "ssl_error"),
        (ConnectionResetError(), "network_error"),
    ]
    for error, category in cases:

        def handler(request, timeout, _error=error):
            raise _error

        patch_transport(monkeypatch, handler=handler)
        with pytest.raises(WebhookError, match=category) as excinfo:
            send_webhook({"WEBHOOK_URL": "https://hooks.example/abc"}, url_env="WEBHOOK_URL", payload={"t": 1})
        assert excinfo.value.category == category
        assert excinfo.value.status is None


def test_http_error_hides_reason_and_body(monkeypatch):
    def handler(request, timeout):
        raise http_error(418)

    patch_transport(monkeypatch, handler=handler)

    with pytest.raises(WebhookError) as excinfo:
        send_webhook({"WEBHOOK_URL": "https://hooks.example/abc"}, url_env="WEBHOOK_URL", payload={"t": 1})

    assert excinfo.value.status == 418
    assert "teapot" not in str(excinfo.value)
    assert "https://hooks.example" not in str(excinfo.value)


def test_response_body_is_never_read(monkeypatch):
    response = FakeResponse(status=200)
    patch_transport(monkeypatch, handler=lambda request, timeout: response)

    send_webhook({"WEBHOOK_URL": "https://hooks.example/abc"}, url_env="WEBHOOK_URL", payload={"t": 1})

    assert response.read_calls == 0


def test_no_redirect_handler_never_follows_redirects():
    handler = webhook._NoRedirectHandler()
    request = urllib.request.Request("https://hooks.example/abc")  # noqa: S310 - test value

    assert handler.redirect_request(request, None, 302, "Found", {}, "https://elsewhere.example/x") is None


def test_redirect_response_is_reported_as_http_error_not_followed(monkeypatch):
    def handler(request, timeout):
        raise http_error(302)

    calls = patch_transport(monkeypatch, handler=handler)

    with pytest.raises(WebhookError, match="http_error \\(HTTP 302\\)"):
        send_webhook({"WEBHOOK_URL": "https://hooks.example/abc"}, url_env="WEBHOOK_URL", payload={"t": 1})

    assert len(calls) == 1
    assert calls[0][0].full_url == "https://hooks.example/abc"


def test_opener_disables_redirects_and_verifies_tls(monkeypatch):
    """The real transport seam builds an opener with TLS verification and no redirects."""
    recorded = {}
    real_create_default_context = ssl.create_default_context

    def fake_create_default_context(*args, **kwargs):
        context = real_create_default_context(*args, **kwargs)
        recorded["context"] = context
        return context

    class FakeOpener:
        def open(self, request, timeout):
            return FakeResponse()

    def fake_build_opener(*handlers):
        recorded["handlers"] = handlers
        return FakeOpener()

    monkeypatch.setattr(ssl, "create_default_context", fake_create_default_context)
    monkeypatch.setattr(webhook.urllib.request, "build_opener", fake_build_opener)

    response = webhook._open_webhook_response(
        urllib.request.Request("https://hooks.example/abc"),  # noqa: S310 - test value
        timeout=5,
    )

    assert isinstance(response, FakeResponse)
    assert any(isinstance(handler, webhook._NoRedirectHandler) for handler in recorded["handlers"])
    context = recorded["context"]
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


# --- payload secret scan ------------------------------------------------


def test_payload_containing_destination_url_is_rejected_before_sending(monkeypatch):
    calls = patch_transport(monkeypatch)
    env = {"WEBHOOK_URL": "https://hooks.example/abc123"}

    with pytest.raises(typer.BadParameter, match="destination URL"):
        send_webhook(env, url_env="WEBHOOK_URL", payload={"text": "see https://hooks.example/abc123"})

    assert calls == []  # rejected before any request


def test_payload_containing_context_secret_is_rejected_before_sending(monkeypatch):
    calls = patch_transport(monkeypatch)
    env = {
        "WEBHOOK_URL": "https://hooks.example/abc",
        "NOTIFY_TOKEN": "super-secret-value",
    }

    with pytest.raises(typer.BadParameter, match="known context secret"):
        send_webhook(env, url_env="WEBHOOK_URL", payload={"text": "token is super-secret-value"})

    assert calls == []


def test_payload_containing_authorization_value_is_rejected_before_sending(monkeypatch):
    calls = patch_transport(monkeypatch)
    env = {
        "WEBHOOK_URL": "https://hooks.example/abc",
        "WEBHOOK_AUTH": "Bearer top-secret-token",
    }

    with pytest.raises(typer.BadParameter, match="authorization value"):
        send_webhook(
            env,
            url_env="WEBHOOK_URL",
            payload={"text": "Bearer top-secret-token"},
            authorization_env="WEBHOOK_AUTH",
        )

    assert calls == []


def test_secret_scan_message_does_not_echo_values(monkeypatch):
    patch_transport(monkeypatch)
    env = {
        "WEBHOOK_URL": "https://hooks.example/abc",
        "NOTIFY_TOKEN": "super-secret-value",
    }

    with pytest.raises(typer.BadParameter) as excinfo:
        send_webhook(env, url_env="WEBHOOK_URL", payload={"text": "super-secret-value"})

    assert "super-secret-value" not in str(excinfo.value)
    assert "https://hooks.example" not in str(excinfo.value)


def test_non_serializable_payload_is_a_configuration_error(monkeypatch):
    calls = patch_transport(monkeypatch)

    with pytest.raises(typer.BadParameter, match="JSON-serializable"):
        send_webhook(
            {"WEBHOOK_URL": "https://hooks.example/abc"},
            url_env="WEBHOOK_URL",
            payload={"text": object()},
        )

    assert calls == []


def test_url_extraction_ignores_surrounding_whitespace(monkeypatch):
    calls = patch_transport(monkeypatch)

    status = send_webhook(
        {"WEBHOOK_URL": "  https://hooks.example/abc  "},
        url_env="WEBHOOK_URL",
        payload={"text": "hi"},
    )

    assert status == 200
    assert calls[0][0].full_url == "https://hooks.example/abc"


def test_payload_query_part_is_preserved_exactly(monkeypatch):
    calls = patch_transport(monkeypatch)

    send_webhook(
        {"WEBHOOK_URL": "https://hooks.example/abc"},
        url_env="WEBHOOK_URL",
        payload={"text": "a=b&c", "nested": {"list": [1, "two", None]}},
    )

    body = calls[0][0].data.decode("utf-8")
    assert json.loads(body) == {"text": "a=b&c", "nested": {"list": [1, "two", None]}}
    # A bare '&' or '=' never triggers the destination check.
    assert urllib.parse.unquote_plus(body) is not None
