import json
import urllib.error

import pytest

from cdt.services.pypi import PYPI_ATTEMPTS, PyPIRelease, PyPIUnavailableError, fetch_release_files

API_URL = "https://pypi.org/pypi/cdt-release/0.5.2/json"
WHEEL = "cdt_release-0.5.2-py3-none-any.whl"
SDIST = "cdt_release-0.5.2.tar.gz"


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _release_payload(files=(WHEEL, SDIST)):
    return json.dumps(
        {
            "info": {"version": "0.5.2"},
            "urls": [{"filename": name, "url": f"https://files.pythonhosted.org/{name}"} for name in files],
        }
    ).encode("utf-8")


def _not_found():
    return urllib.error.HTTPError(API_URL, 404, "Not Found", None, None)


def _install_urlopen(monkeypatch, outcomes):
    """Scripted urlopen outcomes: each call pops the next one; the last outcome repeats."""
    calls: list[str] = []
    outcomes = list(outcomes)

    def fake_urlopen(request, timeout=None):
        calls.append(request.full_url)
        outcome = outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return _FakeResponse(outcome)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    sleeps: list[float] = []
    monkeypatch.setattr("cdt.services.pypi.time.sleep", lambda seconds: sleeps.append(seconds))
    return calls, sleeps


def test_fetch_release_files_returns_files_and_project_url(monkeypatch):
    calls, sleeps = _install_urlopen(monkeypatch, [_release_payload(files=(SDIST, WHEEL))])

    release = fetch_release_files("cdt-release", "0.5.2")

    assert isinstance(release, PyPIRelease)
    assert release.package == "cdt-release"
    assert release.version == "0.5.2"
    assert release.files == (WHEEL, SDIST)
    assert release.wheels == (WHEEL,)
    assert release.sdists == (SDIST,)
    assert release.url == "https://pypi.org/project/cdt-release/0.5.2/"
    assert calls == [API_URL]
    assert sleeps == []


def test_fetch_release_files_tolerates_delayed_indexing(monkeypatch):
    calls, sleeps = _install_urlopen(monkeypatch, [_not_found(), _release_payload()])

    release = fetch_release_files("cdt-release", "0.5.2")

    assert release.wheels == (WHEEL,)
    assert len(calls) == 2
    assert sleeps == [1.0]


def test_fetch_release_files_retries_transient_network_failures(monkeypatch):
    calls, sleeps = _install_urlopen(
        monkeypatch,
        [urllib.error.URLError("connection reset"), _release_payload()],
    )

    release = fetch_release_files("cdt-release", "0.5.2")

    assert release.files == (WHEEL, SDIST)
    assert sleeps == [1.0]
    assert len(calls) == 2


def test_fetch_release_files_retries_transient_http_failures(monkeypatch):
    error = urllib.error.HTTPError(API_URL, 503, "Service Unavailable", None, None)
    calls, sleeps = _install_urlopen(monkeypatch, [error, _release_payload()])

    release = fetch_release_files("cdt-release", "0.5.2")

    assert release.files == (WHEEL, SDIST)
    assert sleeps == [1.0]


def test_fetch_release_files_fails_after_exhausted_retries_on_missing_version(monkeypatch):
    calls, sleeps = _install_urlopen(monkeypatch, [_not_found()])

    with pytest.raises(PyPIUnavailableError, match="after 5 attempts.*not indexed yet"):
        fetch_release_files("cdt-release", "0.5.2")

    assert len(calls) == PYPI_ATTEMPTS
    assert sleeps == [1.0, 2.0, 4.0, 8.0]


def test_fetch_release_files_reports_persistent_http_errors(monkeypatch):
    error = urllib.error.HTTPError(API_URL, 500, "Internal Server Error", None, None)
    _calls, _sleeps = _install_urlopen(monkeypatch, [error])

    with pytest.raises(PyPIUnavailableError, match="HTTP 500"):
        fetch_release_files("cdt-release", "0.5.2")


def test_fetch_release_files_rejects_malformed_payloads_after_retries(monkeypatch):
    calls, sleeps = _install_urlopen(monkeypatch, [b"<html>not json</html>"])

    with pytest.raises(PyPIUnavailableError, match="after 2 attempts"):
        fetch_release_files("cdt-release", "0.5.2", attempts=2)

    assert len(calls) == 2
    assert sleeps == [1.0]


def test_fetch_release_files_respects_custom_attempts_and_delay(monkeypatch):
    calls, sleeps = _install_urlopen(monkeypatch, [_not_found()])

    with pytest.raises(PyPIUnavailableError):
        fetch_release_files("cdt-release", "0.5.2", attempts=3, base_delay=0.25)

    assert len(calls) == 3
    assert sleeps == [0.25, 0.5]


def test_fetch_release_files_requires_at_least_one_attempt():
    with pytest.raises(ValueError, match="attempts must be at least 1"):
        fetch_release_files("cdt-release", "0.5.2", attempts=0)
