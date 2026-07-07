import json
import subprocess
import sys
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from cdt import __version__, self_update
from cdt.self_update import (
    SelfUpdateError,
    _detect_install_method,
    _is_up_to_date,
    _latest_release_tag,
    _manual_update_command,
    _owner_repo_from_url,
    _update_command,
    run_self_update,
)
from tests._helpers import FakeResponse, github_latest_release_json


def test_owner_repo_from_url_parses_https():
    assert _owner_repo_from_url("https://github.com/Sergionius/cdt") == ("Sergionius", "cdt")


def test_owner_repo_from_url_strips_git_suffix():
    assert _owner_repo_from_url("https://github.com/Sergionius/cdt.git") == ("Sergionius", "cdt")


def test_owner_repo_from_url_rejects_invalid():
    with pytest.raises(SelfUpdateError):
        _owner_repo_from_url("https://github.com/Sergionius")


@pytest.mark.parametrize(
    "repo_url",
    [
        "https://github.com/Sergionius/cdt.git@branch",
        "https://github.com/Sergionius/cdt?ref=main",
        "https://github.com/Sergionius/cdt#main",
        "https://github.com/Sergionius/cdt;param",
        "https://github.com/Ser%40gionius/cdt",
        "https://github.com/-Sergionius/cdt",
        "https://github.com/Sergionius/cdt name",
    ],
)
def test_owner_repo_from_url_rejects_suspicious_urls(repo_url):
    with pytest.raises(SelfUpdateError):
        _owner_repo_from_url(repo_url)


def test_latest_release_tag_parses_tag_name():
    response = FakeResponse(github_latest_release_json("v0.4.0"))

    with patch("urllib.request.urlopen", return_value=response) as mock_urlopen:
        tag = _latest_release_tag("Sergionius", "cdt")

    assert tag == "v0.4.0"
    mock_urlopen.assert_called_once()
    request = mock_urlopen.call_args[0][0]
    assert "Sergionius/cdt" in request.full_url
    assert request.headers.get("User-agent", "").startswith("cdt/")


def test_latest_release_tag_strips_whitespace():
    response = FakeResponse(github_latest_release_json("  v0.4.0  "))

    with patch("urllib.request.urlopen", return_value=response):
        tag = _latest_release_tag("Sergionius", "cdt")

    assert tag == "v0.4.0"


def test_latest_release_tag_missing_tag_name_raises():
    response = FakeResponse(json.dumps({}).encode("utf-8"))

    with patch("urllib.request.urlopen", return_value=response):
        with pytest.raises(SelfUpdateError, match="tag_name"):
            _latest_release_tag("Sergionius", "cdt")


def test_latest_release_tag_network_error_raises():
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no route")):
        with pytest.raises(SelfUpdateError, match="Network error"):
            _latest_release_tag("Sergionius", "cdt")


def test_latest_release_tag_http_error_raises():
    error = urllib.error.HTTPError(
        "https://api.github.com/repos/Sergionius/cdt/releases/latest",
        404,
        "Not Found",
        {},
        None,
    )

    with patch("urllib.request.urlopen", side_effect=error):
        with pytest.raises(SelfUpdateError, match="GitHub API error"):
            _latest_release_tag("Sergionius", "cdt")


def test_latest_release_tag_timeout_raises():
    with patch("urllib.request.urlopen", side_effect=TimeoutError):
        with pytest.raises(SelfUpdateError, match="timed out"):
            _latest_release_tag("Sergionius", "cdt")


def test_latest_release_tag_invalid_json_raises():
    response = FakeResponse(b"not json")

    with patch("urllib.request.urlopen", return_value=response):
        with pytest.raises(SelfUpdateError, match="Unable to parse"):
            _latest_release_tag("Sergionius", "cdt")


def test_detect_install_method_detects_pipx_from_executable(monkeypatch):
    monkeypatch.setattr(sys, "executable", "/home/user/.local/pipx/venvs/cdt/bin/python")
    assert _detect_install_method() == ("pipx", False)


def test_detect_install_method_detects_pipx_from_pip_show(monkeypatch):
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3")

    def fake_run(args, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stdout = (
            "Name: cdt\nVersion: 0.3.0\n"
            "Location: /home/user/.local/pipx/venvs/cdt/lib/python3.12/site-packages\n"
        )
        result.stderr = ""
        return result

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert _detect_install_method() == ("pipx", False)


def test_detect_install_method_rejects_editable_install(monkeypatch):
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3")

    def fake_run(args, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stdout = (
            "Name: cdt\nVersion: 0.3.0\n"
            "Location: /usr/lib/python3.12/site-packages\n"
            "Editable project location: /home/user/cdt\n"
        )
        result.stderr = ""
        return result

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert _detect_install_method() is None


def test_detect_install_method_falls_back_to_pip(monkeypatch):
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3")

    def fake_run(args, **kwargs):
        result = MagicMock()
        if args[:3] == [sys.executable, "-m", "pip"]:
            result.returncode = 0
            result.stdout = "Name: cdt\nVersion: 0.3.0\nLocation: /usr/lib/python3.12/site-packages\n"
            result.stderr = ""
        elif args[:2] == ["pipx", "list"]:
            result.returncode = 1
            result.stdout = ""
            result.stderr = ""
        else:
            result.returncode = 1
        return result

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert _detect_install_method() == ("pip", False)


def test_detect_install_method_detects_pipx_from_pipx_list(monkeypatch):
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3")

    def fake_run(args, **kwargs):
        result = MagicMock()
        if args[:3] == [sys.executable, "-m", "pip"]:
            result.returncode = 1
        elif args[:2] == ["pipx", "list"]:
            result.returncode = 0
            result.stdout = json.dumps({"venvs": {"cdt": {"metadata": {"main_package": {"package": "cdt"}}}}})
            result.stderr = ""
        else:
            result.returncode = 1
        return result

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert _detect_install_method() == ("pipx", False)


def test_detect_install_method_detects_pipx_from_main_package(monkeypatch):
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3")

    def fake_run(args, **kwargs):
        result = MagicMock()
        if args[:3] == [sys.executable, "-m", "pip"]:
            result.returncode = 1
        elif args[:2] == ["pipx", "list"]:
            result.returncode = 0
            result.stdout = json.dumps(
                {"venvs": {"custom-env-name": {"metadata": {"main_package": {"package": "cdt"}}}}}
            )
            result.stderr = ""
        else:
            result.returncode = 1
        return result

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert _detect_install_method() == ("pipx", False)


def test_detect_install_method_returns_none_when_unknown(monkeypatch):
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3")

    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        result = MagicMock()
        result.returncode = 1
        result.stdout = ""
        result.stderr = ""
        return result

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert _detect_install_method() is None
    assert calls[0][:3] == [sys.executable, "-m", "pip"]
    assert any(args[:2] == ["pipx", "list"] for args in calls)


def test_detect_install_method_handles_pip_show_failure(monkeypatch):
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3")

    def fake_run(args, **kwargs):
        result = MagicMock()
        if args[:3] == [sys.executable, "-m", "pip"]:
            raise OSError("pip unavailable")
        elif args[:2] == ["pipx", "list"]:
            result.returncode = 1
        else:
            result.returncode = 1
        result.stdout = ""
        result.stderr = ""
        return result

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert _detect_install_method() is None


def test_detect_install_method_handles_pipx_list_invalid_json(monkeypatch):
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3")

    def fake_run(args, **kwargs):
        result = MagicMock()
        if args[:3] == [sys.executable, "-m", "pip"]:
            result.returncode = 1
        elif args[:2] == ["pipx", "list"]:
            result.returncode = 0
            result.stdout = "not json"
        else:
            result.returncode = 1
        result.stderr = ""
        return result

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert _detect_install_method() is None


def test_detect_install_method_handles_malformed_pipx_venv_data(monkeypatch):
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3")

    def fake_run(args, **kwargs):
        result = MagicMock()
        if args[:3] == [sys.executable, "-m", "pip"]:
            result.returncode = 1
        elif args[:2] == ["pipx", "list"]:
            result.returncode = 0
            result.stdout = json.dumps({"venvs": {"not-a-dict": "value"}})
            result.stderr = ""
        else:
            result.returncode = 1
        return result

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert _detect_install_method() is None


def test_update_command_for_pipx():
    cmd = _update_command("v0.4.0", "pipx", owner="Sergionius", repo="cdt")
    assert cmd == [
        "pipx",
        "install",
        "--force",
        "git+https://github.com/Sergionius/cdt.git@v0.4.0",
    ]


def test_update_command_for_pip():
    cmd = _update_command("v0.4.0", "pip", owner="Sergionius", repo="cdt")
    assert cmd[:3] == [sys.executable, "-m", "pip"]
    assert "--force-reinstall" in cmd
    assert "git+https://github.com/Sergionius/cdt.git@v0.4.0" in cmd


def test_update_command_unsupported_method_raises():
    with pytest.raises(SelfUpdateError, match="Unsupported"):
        _update_command("v0.4.0", "uv", owner="Sergionius", repo="cdt")


def test_update_command_rejects_unsafe_tag():
    with pytest.raises(SelfUpdateError, match="Unsafe"):
        _update_command("v0.4.0 evil", "pipx", owner="Sergionius", repo="cdt")


def test_manual_update_command_quotes_url_and_python(monkeypatch):
    monkeypatch.setattr(sys, "executable", "/path with spaces/python3")
    command = _manual_update_command("v0.4.0", owner="Sergionius", repo="cdt")
    assert "'/path with spaces/python3'" in command
    assert "pipx install --force git+https://github.com/Sergionius/cdt.git@v0.4.0" in command
    assert "git+https://github.com/Sergionius/cdt.git@v0.4.0" in command


def test_run_self_update_dry_run_prints_command(monkeypatch, capsys):
    def fake_urlopen(url, **kwargs):
        return FakeResponse(github_latest_release_json("v0.4.0"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(
        self_update,
        "_detect_install_method",
        lambda: ("pipx", False),
    )

    exit_code = run_self_update(repo_url="https://github.com/Sergionius/cdt", dry_run=True)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert f"Current version: {__version__}" in captured.out
    assert "Latest release: v0.4.0" in captured.out
    assert "pipx install --force" in captured.out
    assert "Dry run" in captured.out


def test_run_self_update_uses_shell_safe_command_join(monkeypatch, capsys):
    def fake_urlopen(url, **kwargs):
        return FakeResponse(github_latest_release_json("v0.4.0"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(sys, "executable", "/path with spaces/python3")
    monkeypatch.setattr(
        self_update,
        "_detect_install_method",
        lambda: ("pip", False),
    )

    exit_code = run_self_update(repo_url="https://github.com/Sergionius/cdt", dry_run=True)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "'/path with spaces/python3'" in captured.out
    assert "git+https://github.com/Sergionius/cdt.git@v0.4.0" in captured.out


def test_run_self_update_dry_run_without_detected_method_shows_manual_command(monkeypatch, capsys):
    def fake_urlopen(url, **kwargs):
        return FakeResponse(github_latest_release_json("v0.4.0"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(self_update, "_detect_install_method", lambda: None)

    exit_code = run_self_update(repo_url="https://github.com/Sergionius/cdt", dry_run=True)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Unable to detect" in captured.out
    assert "pipx install --force" in captured.out


def test_run_self_update_already_up_to_date(monkeypatch, capsys):
    def fake_urlopen(url, **kwargs):
        return FakeResponse(github_latest_release_json(f"v{__version__}"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    exit_code = run_self_update(repo_url="https://github.com/Sergionius/cdt", dry_run=False)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Already up to date" in captured.out


def test_run_self_update_already_up_to_date_without_v_prefix(monkeypatch, capsys):
    def fake_urlopen(url, **kwargs):
        return FakeResponse(github_latest_release_json(__version__))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    exit_code = run_self_update(repo_url="https://github.com/Sergionius/cdt", dry_run=False)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Already up to date" in captured.out


def test_run_self_update_unknown_method_exits_with_error(monkeypatch, capsys):
    def fake_urlopen(url, **kwargs):
        return FakeResponse(github_latest_release_json("v9.9.9"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(self_update, "_detect_install_method", lambda: None)

    exit_code = run_self_update(repo_url="https://github.com/Sergionius/cdt", dry_run=False)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Unable to detect" in captured.err


def test_run_self_update_executes_command(monkeypatch, capsys):
    def fake_urlopen(url, **kwargs):
        return FakeResponse(github_latest_release_json("v0.4.0"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(self_update, "_detect_install_method", lambda: ("pipx", False))

    recorded = []

    def fake_run(command, **kwargs):
        recorded.append(command)
        assert kwargs.get("timeout") == 300
        result = MagicMock()
        result.returncode = 0
        return result

    monkeypatch.setattr(subprocess, "run", fake_run)

    exit_code = run_self_update(repo_url="https://github.com/Sergionius/cdt", dry_run=False)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert recorded == [["pipx", "install", "--force", "git+https://github.com/Sergionius/cdt.git@v0.4.0"]]
    assert "Running update" in captured.out


def test_run_self_update_propagates_command_failure(monkeypatch, capsys):
    def fake_urlopen(url, **kwargs):
        return FakeResponse(github_latest_release_json("v0.4.0"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(self_update, "_detect_install_method", lambda: ("pipx", False))

    def fake_run(command, **kwargs):
        result = MagicMock()
        result.returncode = 1
        return result

    monkeypatch.setattr(subprocess, "run", fake_run)

    exit_code = run_self_update(repo_url="https://github.com/Sergionius/cdt", dry_run=False)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Update command failed" in captured.err


def test_run_self_update_handles_missing_executable(monkeypatch, capsys):
    def fake_urlopen(url, **kwargs):
        return FakeResponse(github_latest_release_json("v0.4.0"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(self_update, "_detect_install_method", lambda: ("pipx", False))

    def fake_run(command, **kwargs):
        raise FileNotFoundError("pipx")

    monkeypatch.setattr(subprocess, "run", fake_run)

    exit_code = run_self_update(repo_url="https://github.com/Sergionius/cdt", dry_run=False)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "command not found" in captured.err


def test_run_self_update_handles_os_error(monkeypatch, capsys):
    def fake_urlopen(url, **kwargs):
        return FakeResponse(github_latest_release_json("v0.4.0"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(self_update, "_detect_install_method", lambda: ("pipx", False))

    def fake_run(command, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(subprocess, "run", fake_run)

    exit_code = run_self_update(repo_url="https://github.com/Sergionius/cdt", dry_run=False)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Update failed" in captured.err


def test_run_self_update_handles_timeout(monkeypatch, capsys):
    def fake_urlopen(url, **kwargs):
        return FakeResponse(github_latest_release_json("v0.4.0"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(self_update, "_detect_install_method", lambda: ("pipx", False))

    def fake_run(command, **kwargs):
        raise subprocess.TimeoutExpired("pipx", 300)

    monkeypatch.setattr(subprocess, "run", fake_run)

    exit_code = run_self_update(repo_url="https://github.com/Sergionius/cdt", dry_run=False)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Update timed out" in captured.err


def test_owner_repo_from_url_rejects_non_github():
    with pytest.raises(SelfUpdateError, match="Unsupported repository URL"):
        _owner_repo_from_url("https://gitlab.com/Sergionius/cdt")


def test_owner_repo_from_url_rejects_http():
    with pytest.raises(SelfUpdateError, match="Unsupported repository URL"):
        _owner_repo_from_url("http://github.com/Sergionius/cdt")


def test_owner_repo_from_url_rejects_nonstandard_port():
    with pytest.raises(SelfUpdateError, match="Unsupported repository URL"):
        _owner_repo_from_url("https://github.com:8443/Sergionius/cdt")


def test_latest_release_tag_non_dict_response_raises():
    response = FakeResponse(json.dumps(["not", "a", "dict"]).encode("utf-8"))

    with patch("urllib.request.urlopen", return_value=response):
        with pytest.raises(SelfUpdateError, match="Unexpected GitHub API response format"):
            _latest_release_tag("Sergionius", "cdt")


def test_is_up_to_date_skips_newer_and_equal_versions():
    assert _is_up_to_date("0.4.0", "0.3.0") is True
    assert _is_up_to_date("0.3.0", "0.3.0") is True
    assert _is_up_to_date("v0.3.0", "0.3.0") is True
    assert _is_up_to_date("0.4.0-dev", "0.3.0") is True
    assert _is_up_to_date("0.4.0", "0.4.0-beta") is True
    assert _is_up_to_date("0.3.0", "0.4.0") is False


def test_is_up_to_date_pads_release_segments():
    assert _is_up_to_date("0.3", "0.3.0") is True
    assert _is_up_to_date("0.3", "0.3.0-beta") is True
    assert _is_up_to_date("0.3.0", "0.3.1") is False
    assert _is_up_to_date("0.3.0", "0.3.0-beta") is True


def test_run_self_update_skips_when_current_newer(monkeypatch, capsys):
    def fake_urlopen(url, **kwargs):
        return FakeResponse(github_latest_release_json("v0.2.0"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    exit_code = run_self_update(repo_url="https://github.com/Sergionius/cdt", dry_run=False)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Already up to date" in captured.out


def test_detect_install_method_does_not_misdetect_pipx_from_unrelated_path(monkeypatch):
    monkeypatch.setattr(sys, "executable", "/home/pipx_user/projects/venv/bin/python")

    def fake_run(args, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stdout = (
            "Name: cdt\nVersion: 0.3.0\n"
            "Location: /home/pipx_user/projects/venv/lib/python3.12/site-packages\n"
        )
        result.stderr = ""
        return result

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert _detect_install_method() == ("pip", False)


def test_detect_install_method_detects_pip_user_install(monkeypatch):
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3")
    monkeypatch.setattr(
        self_update.site,
        "getusersitepackages",
        lambda: "/home/user/.local/lib/python3.12/site-packages",
    )

    def fake_run(args, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stdout = (
            "Name: cdt\nVersion: 0.3.0\n"
            "Location: /home/user/.local/lib/python3.12/site-packages/cdt\n"
        )
        result.stderr = ""
        return result

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert _detect_install_method() == ("pip", True)


def test_update_command_for_pip_includes_user_flag_when_requested():
    cmd = _update_command("v0.4.0", "pip", is_user=True, owner="Sergionius", repo="cdt")
    assert cmd[:3] == [sys.executable, "-m", "pip"]
    assert "--force-reinstall" in cmd
    assert "--user" in cmd
    assert "git+https://github.com/Sergionius/cdt.git@v0.4.0" in cmd


def test_detect_install_method_handles_non_dict_pipx_payload(monkeypatch):
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3")

    def fake_run(args, **kwargs):
        result = MagicMock()
        if args[:3] == [sys.executable, "-m", "pip"]:
            result.returncode = 1
        elif args[:2] == ["pipx", "list"]:
            result.returncode = 0
            result.stdout = json.dumps(["not", "a", "dict"])
        else:
            result.returncode = 1
        result.stderr = ""
        return result

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert _detect_install_method() is None


def test_detect_install_method_handles_non_dict_pipx_venvs(monkeypatch):
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3")

    def fake_run(args, **kwargs):
        result = MagicMock()
        if args[:3] == [sys.executable, "-m", "pip"]:
            result.returncode = 1
        elif args[:2] == ["pipx", "list"]:
            result.returncode = 0
            result.stdout = json.dumps({"venvs": ["not", "a", "dict"]})
        else:
            result.returncode = 1
        result.stderr = ""
        return result

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert _detect_install_method() is None
