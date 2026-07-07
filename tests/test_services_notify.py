import json
import urllib.parse

import pytest
import typer

from cdt.services import notify


class FakeHTTPResponse:
    def __init__(self, status: int = 200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_build_notify_text_includes_custom_version_and_ids():
    text = notify._build_notify_text({"NOTIFY_TEXT": "Ready"}, "1.2.3+4", [" APP-1 ", "", "APP-2"])

    assert text == "Ready\n💡 Version: 1.2.3+4\n\nЗадачи:\nhttps://tracker.yandex.ru/APP-1\nhttps://tracker.yandex.ru/APP-2"


def test_notify_success_no_provider_is_noop(monkeypatch):
    monkeypatch.setattr(
        notify.urllib.request, "urlopen", lambda *args, **kwargs: pytest.fail("unexpected network call")
    )

    notify._notify_success({}, "1.0+1")


def test_notify_success_telegram_posts_form(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout):
        calls.append((req, timeout))
        return FakeHTTPResponse()

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)

    notify._notify_success(
        {"NOTIFY_PROVIDER": "telegram", "TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_CHAT_ID": "chat"},
        "1.0+1",
        ["APP-1"],
    )

    req, timeout = calls[0]
    assert timeout == 30
    assert req.full_url == "https://api.telegram.org/bottoken/sendMessage"
    assert req.get_method() == "POST"
    assert req.headers["Content-type"] == "application/x-www-form-urlencoded"
    payload = urllib.parse.parse_qs(req.data.decode("utf-8"))
    assert payload["chat_id"] == ["chat"]
    assert "https://tracker.yandex.ru/APP-1" in payload["text"][0]


def test_notify_success_telegram_requires_credentials():
    with pytest.raises(typer.BadParameter, match="Missing TELEGRAM_BOT_TOKEN"):
        notify._notify_success({"NOTIFY_PROVIDER": "telegram"}, "1.0+1")


def test_notify_success_telegram_reports_http_error(monkeypatch):
    monkeypatch.setattr(notify.urllib.request, "urlopen", lambda req, timeout: FakeHTTPResponse(status=500))

    with pytest.raises(typer.BadParameter, match="Telegram notify failed with status 500"):
        notify._notify_success(
            {"NOTIFY_PROVIDER": "telegram", "TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_CHAT_ID": "chat"},
            "1.0+1",
        )


def test_notify_success_pachca_posts_json(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout):
        calls.append((req, timeout))
        return FakeHTTPResponse()

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)

    notify._notify_success({"NOTIFY_PROVIDER": "pachca", "PACHCA_WEBHOOK_URL": "https://hook"}, "1.0+1")

    req, timeout = calls[0]
    assert timeout == 30
    assert req.full_url == "https://hook"
    assert req.get_method() == "POST"
    assert req.headers["Content-type"] == "application/json"
    assert json.loads(req.data.decode("utf-8"))["message"] == "💡 Version: 1.0+1"


def test_notify_success_pachca_requires_webhook():
    with pytest.raises(typer.BadParameter, match="Missing PACHCA_WEBHOOK_URL"):
        notify._notify_success({"NOTIFY_PROVIDER": "pachca"}, "1.0+1")


def test_notify_success_pachca_reports_http_error(monkeypatch):
    monkeypatch.setattr(notify.urllib.request, "urlopen", lambda req, timeout: FakeHTTPResponse(status=400))

    with pytest.raises(typer.BadParameter, match="Pachca notify failed with status 400"):
        notify._notify_success({"NOTIFY_PROVIDER": "pachca", "PACHCA_WEBHOOK_URL": "https://hook"}, "1.0+1")


def test_notify_success_rejects_unknown_provider():
    with pytest.raises(typer.BadParameter, match="NOTIFY_PROVIDER must be telegram or pachca"):
        notify._notify_success({"NOTIFY_PROVIDER": "email"}, "1.0+1")


def test_notify_prod_user_agent_pachca_posts_message(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout):
        calls.append((req, timeout))
        return FakeHTTPResponse()

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)

    notify._notify_prod_user_agent_pachca(
        {
            "NOTIFY_PROVIDER": "pachca",
            "PACHCA_USER_AGENT_WEBHOOK_URL": "https://ua-hook",
            "UA_APP_NAME": "CDTApp",
            "UA_TITLE": "UA title",
        },
        "2.3.4+56",
    )

    req, _timeout = calls[0]
    body = json.loads(req.data.decode("utf-8"))["message"]
    assert req.full_url == "https://ua-hook"
    assert "UA title for version 2.3.4" in body
    assert "iOS: CDTApp 2.3.4 (56)" in body
    assert "android: CDTApp 2.3.4 (56)" in body


def test_notify_prod_user_agent_ignores_other_provider(monkeypatch):
    monkeypatch.setattr(
        notify.urllib.request, "urlopen", lambda *args, **kwargs: pytest.fail("unexpected network call")
    )

    notify._notify_prod_user_agent_pachca({"NOTIFY_PROVIDER": "telegram"}, "1.0+1")


def test_notify_prod_user_agent_requires_webhook_and_app_name():
    with pytest.raises(typer.BadParameter, match="Missing PACHCA_USER_AGENT_WEBHOOK_URL"):
        notify._notify_prod_user_agent_pachca({"NOTIFY_PROVIDER": "pachca"}, "1.0+1")

    with pytest.raises(typer.BadParameter, match="Missing UA_APP_NAME"):
        notify._notify_prod_user_agent_pachca(
            {"NOTIFY_PROVIDER": "pachca", "PACHCA_USER_AGENT_WEBHOOK_URL": "https://hook"}, "1.0+1"
        )
