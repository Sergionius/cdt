"""Per-user CDT settings (never stored in a project's cdt.yaml)."""

from __future__ import annotations

import json
import os
from pathlib import Path

ORCA_STATUS = "experimental.orca-status"
RUN_RETENTION = "experimental.run-retention"

# Public flag names map to their camelCase keys inside the settings file.
EXPERIMENTAL_FLAGS: dict[str, str] = {
    ORCA_STATUS: "orca_status",
    RUN_RETENTION: "run_retention",
}


def settings_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / "cdt" / "settings.json"


def experimental_enabled(name: str) -> bool:
    """Read one experimental flag; missing or invalid settings never enable it."""
    try:
        key = EXPERIMENTAL_FLAGS[name]
    except KeyError:
        raise ValueError(f"Unknown experimental setting: {name}") from None
    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
        experimental = data.get("experimental") if isinstance(data, dict) else None
        return isinstance(experimental, dict) and experimental.get(key) is True
    except (OSError, ValueError, AttributeError, TypeError):
        return False


def orca_status_enabled() -> bool:
    return experimental_enabled(ORCA_STATUS)


def run_retention_enabled() -> bool:
    return experimental_enabled(RUN_RETENTION)


def set_experimental_flag(name: str, enabled: bool) -> Path:
    """Atomically flip one experimental flag, preserving all other settings."""
    try:
        key = EXPERIMENTAL_FLAGS[name]
    except KeyError:
        raise ValueError(f"Unknown experimental setting: {name}") from None
    path = settings_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Invalid CDT settings")
    except FileNotFoundError:
        data = {}
    experimental = data.get("experimental", {})
    if not isinstance(experimental, dict):
        raise ValueError("Invalid CDT experimental settings")
    experimental[key] = enabled
    data["experimental"] = experimental
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def set_orca_status(enabled: bool) -> Path:
    return set_experimental_flag(ORCA_STATUS, enabled)


def set_run_retention(enabled: bool) -> Path:
    return set_experimental_flag(RUN_RETENTION, enabled)
