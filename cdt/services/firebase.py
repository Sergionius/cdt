import subprocess
from pathlib import Path

import typer

from ..runner import _run
from .tracker import _build_tracker_release_notes


def _ensure_firebase_cli_available() -> None:
    try:
        check = subprocess.run(["firebase", "--version"], capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise typer.BadParameter(
            "firebase CLI is not available. Install it and verify with: firebase --version"
        ) from exc
    if check.returncode != 0:
        raise typer.BadParameter("firebase CLI check failed. Verify with: firebase --version")


def _build_firebase_app_distribution_command(aab_path: Path, env: dict[str, str], ids: list[str]) -> list[str]:
    app_id = env.get("FIREBASE_APP_ID_ANDROID", "").strip()
    token = env.get("FIREBASE_TOKEN", "").strip()
    credentials = env.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    groups = env.get("FIREBASE_GROUPS", "main").strip() or "main"

    if not app_id:
        raise typer.BadParameter("Missing FIREBASE_APP_ID_ANDROID in project .env")
    if not token and not credentials:
        raise typer.BadParameter(
            "Missing Firebase authentication: set FIREBASE_TOKEN or GOOGLE_APPLICATION_CREDENTIALS"
        )

    notes = _build_tracker_release_notes(ids)
    command = [
        "firebase",
        "appdistribution:distribute",
        str(aab_path),
        "--app",
        app_id,
        "--groups",
        groups,
        "--release-notes",
        notes,
    ]
    if token:
        command.extend(["--token", token])
    return command


def _build_android_apptester_command(aab_path: Path, env: dict[str, str], ids: list[str]) -> list[str]:
    _ensure_firebase_cli_available()
    return _build_firebase_app_distribution_command(aab_path, env, ids)


def _upload_android_apptester(aab_path: Path, env: dict[str, str], ids: list[str]) -> int:
    command = _build_android_apptester_command(aab_path, env, ids)
    return _run(command, cwd=Path.cwd())
