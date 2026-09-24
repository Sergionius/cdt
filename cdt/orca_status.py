"""Experimental Orca terminal status via its undocumented OSC 9999 sequence."""

from __future__ import annotations

import json
import os
import sys

from .user_settings import orca_status_enabled


def report(state: str, pipeline: str, *, failed: bool = False) -> None:
    """Best-effort status for a direct run in an Orca shell, never for agent panes."""
    if not orca_status_enabled() or not os.environ.get("ORCA_PANE_KEY"):
        return
    if os.environ.get("ORCA_PI_STATUS_OWNED") or os.environ.get("PI_SESSION_ID"):
        return
    try:
        stream = sys.stdout
        if not stream.isatty():
            return
        payload = {"state": state, "agentType": "cdt", "prompt": f"CDT {pipeline}: {'failed' if failed else state}"}
        # Bypass CDT's output recorder: terminal control sequences must never enter output.log.
        os.write(stream.fileno(), b"\x1b]9999;" + json.dumps(payload, ensure_ascii=True).encode("ascii") + b"\x07")
    except (OSError, ValueError, AttributeError):
        pass  # Status is cosmetic and must never change the pipeline outcome.
