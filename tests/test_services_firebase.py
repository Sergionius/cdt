import subprocess
from pathlib import Path

import pytest
import typer

from cdt.services import firebase


def test_ensure_firebase_cli_available_reports_missing_executable(monkeypatch):
    def fake_run(command, capture_output, text):
        raise FileNotFoundError

    monkeypatch.setattr(firebase.subprocess, "run", fake_run)

    with pytest.raises(typer.BadParameter, match="firebase CLI is not available"):
        firebase._ensure_firebase_cli_available()


def test_ensure_firebase_cli_available_reports_failed_check(monkeypatch):
    monkeypatch.setattr(
        firebase.subprocess,
        "run",
        lambda command, capture_output, text: subprocess.CompletedProcess(command, 1),
    )

    with pytest.raises(typer.BadParameter, match="firebase CLI check failed"):
        firebase._ensure_firebase_cli_available()


def test_ensure_firebase_cli_available_accepts_zero_exit(monkeypatch):
    monkeypatch.setattr(
        firebase.subprocess,
        "run",
        lambda command, capture_output, text: subprocess.CompletedProcess(command, 0),
    )

    firebase._ensure_firebase_cli_available()


def test_build_firebase_command_requires_app_id_and_token():
    with pytest.raises(typer.BadParameter, match="Missing FIREBASE_APP_ID_ANDROID"):
        firebase._build_firebase_app_distribution_command(Path("app.aab"), {}, [])

    with pytest.raises(typer.BadParameter, match="Missing FIREBASE_TOKEN"):
        firebase._build_firebase_app_distribution_command(Path("app.aab"), {"FIREBASE_APP_ID_ANDROID": "app"}, [])


def test_build_firebase_command_uses_groups_and_tracker_notes():
    command = firebase._build_firebase_app_distribution_command(
        Path("app.aab"),
        {"FIREBASE_APP_ID_ANDROID": "app-id", "FIREBASE_TOKEN": "token", "FIREBASE_GROUPS": "qa"},
        ["APP-1", "APP-2"],
    )

    assert command == [
        "firebase",
        "appdistribution:distribute",
        "app.aab",
        "--app",
        "app-id",
        "--groups",
        "qa",
        "--release-notes",
        "https://tracker.yandex.ru/APP-1\nhttps://tracker.yandex.ru/APP-2",
        "--token",
        "token",
    ]


def test_build_firebase_command_defaults_empty_groups_to_main():
    command = firebase._build_firebase_app_distribution_command(
        Path("app.aab"),
        {"FIREBASE_APP_ID_ANDROID": "app-id", "FIREBASE_TOKEN": "token", "FIREBASE_GROUPS": "   "},
        [],
    )

    assert command[command.index("--groups") + 1] == "main"


def test_upload_android_apptester_runs_built_command(monkeypatch, tmp_path):
    calls = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(firebase, "_build_android_apptester_command", lambda aab_path, env, ids: ["firebase", "upload"])
    monkeypatch.setattr(firebase, "_run", lambda command, cwd: calls.append((command, cwd)) or 5)

    assert firebase._upload_android_apptester(Path("app.aab"), {}, ["APP-1"]) == 5
    assert calls == [(["firebase", "upload"], tmp_path)]
