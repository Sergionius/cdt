import subprocess
from pathlib import Path

import typer


def _play_named_sound(
    env: dict[str, str],
    cwd: Path,
    *,
    mode_key: str,
    file_key: str,
    default_file: str,
    label: str,
) -> None:
    mode = env.get(mode_key, "").strip().lower()
    if not mode:
        return

    if mode != "macos":
        typer.echo(f"⚠️ Unknown {mode_key} mode: {mode}. Supported: macos", err=True)
        return

    sound_file_raw = env.get(file_key, "").strip()
    command: list[str]

    volume_raw = env.get("SOUND_VOLUME", "0.3").strip() or "0.3"
    try:
        volume = float(volume_raw)
    except ValueError:
        typer.echo(f"⚠️ Invalid SOUND_VOLUME value: {volume_raw}. Using default 0.3", err=True)
        volume = 0.3
    volume = max(0.0, min(1.0, volume))

    if sound_file_raw:
        sound_file = Path(sound_file_raw).expanduser()
        if not sound_file.is_absolute():
            sound_file = cwd / sound_file
        if not sound_file.exists():
            typer.echo(f"⚠️ {file_key} not found: {sound_file}", err=True)
            return
        command = ["afplay", "-v", str(volume), str(sound_file)]
    else:
        command = ["afplay", "-v", str(volume), default_file]

    try:
        proc = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
    except FileNotFoundError:
        typer.echo(f"⚠️ afplay not found. {mode_key}=macos works only on macOS with afplay", err=True)
        return

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        typer.echo(f"⚠️ Failed to play {label} sound: {err[:300]}", err=True)


def _play_success_sound(env: dict[str, str], cwd: Path) -> None:
    _play_named_sound(
        env,
        cwd,
        mode_key="SUCCESS_SOUND",
        file_key="SUCCESS_SOUND_FILE",
        default_file="/System/Library/Sounds/Glass.aiff",
        label="success",
    )


def _play_fail_sound(env: dict[str, str], cwd: Path) -> None:
    _play_named_sound(
        env,
        cwd,
        mode_key="FAIL_SOUND",
        file_key="FAIL_SOUND_FILE",
        default_file="/System/Library/Sounds/Basso.aiff",
        label="fail",
    )
