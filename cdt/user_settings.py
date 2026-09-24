"""Per-user CDT settings (never stored in a project's cdt.yaml)."""

from __future__ import annotations

import json
import os
from pathlib import Path

ORCA_STATUS = "experimental.orca-status"


def settings_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / "cdt" / "settings.json"


def orca_status_enabled() -> bool:
    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
        return data.get("experimental", {}).get("orca_status") is True
    except (OSError, ValueError, AttributeError, TypeError):
        return False


def set_orca_status(enabled: bool) -> Path:
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
    experimental["orca_status"] = enabled
    data["experimental"] = experimental
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path
