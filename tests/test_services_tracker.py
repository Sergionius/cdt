import json

import pytest
import typer

from cdt.services import tracker


class FakeHTTPResponse:
    def __init__(self, status: int = 200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_build_tracker_release_notes_links_ids():
    assert tracker._build_tracker_release_notes(["APP-1", "APP-2"]) == (
        "https://tracker.yandex.ru/APP-1\nhttps://tracker.yandex.ru/APP-2"
    )


def test_build_tracker_comment_text_includes_custom_text_and_version():
    assert tracker._build_tracker_comment_text({"NOTIFY_TEXT": "Done"}, "1.2+3") == "Done\n💡 Версия: 1.2+3"


def test_tracker_comment_requires_token_and_org_id():
    with pytest.raises(typer.BadParameter, match="Missing TRACKER_OAUTH_TOKEN"):
        tracker._tracker_comment({}, "APP-1", "1.0+1")


def test_tracker_comment_posts_json_to_default_base_url(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout):
        calls.append((req, timeout))
        return FakeHTTPResponse()

    monkeypatch.setattr(tracker.urllib.request, "urlopen", fake_urlopen)

    tracker._tracker_comment(
        {"TRACKER_OAUTH_TOKEN": "token", "TRACKER_ORG_ID": "org", "NOTIFY_TEXT": "Released"},
        "APP-1",
        "1.0+1",
    )

    req, timeout = calls[0]
    assert timeout == 30
    assert req.full_url == "https://api.tracker.yandex.net/v2/issues/APP-1/comments"
    assert req.get_method() == "POST"
    assert req.headers["Authorization"] == "OAuth token"
    assert req.headers["X-cloud-org-id"] == "org"
    assert req.headers["Content-type"] == "application/json"
    assert json.loads(req.data.decode("utf-8")) == {"text": "Released\n💡 Версия: 1.0+1"}


def test_tracker_comment_strips_custom_base_url(monkeypatch):
    calls = []
    monkeypatch.setattr(
        tracker.urllib.request,
        "urlopen",
        lambda req, timeout: calls.append(req) or FakeHTTPResponse(),
    )

    tracker._tracker_comment(
        {"TRACKER_OAUTH_TOKEN": "token", "TRACKER_ORG_ID": "org", "TRACKER_BASE_URL": "https://tracker.example/"},
        "APP-2",
        "2.0+2",
    )

    assert calls[0].full_url == "https://tracker.example/v2/issues/APP-2/comments"


def test_tracker_comment_reports_http_error_status(monkeypatch):
    monkeypatch.setattr(tracker.urllib.request, "urlopen", lambda req, timeout: FakeHTTPResponse(status=500))

    with pytest.raises(typer.BadParameter, match="Tracker comment failed for APP-1: status 500"):
        tracker._tracker_comment({"TRACKER_OAUTH_TOKEN": "token", "TRACKER_ORG_ID": "org"}, "APP-1", "1.0+1")
