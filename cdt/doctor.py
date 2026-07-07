from __future__ import annotations

import json
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import typer

from . import __version__
from .pipeline.config import load_pipeline_config
from .self_update import _detect_install_method


def doctor_payload(cwd: Path) -> dict[str, Any]:
    manager_info = _detect_install_method()
    manager = manager_info[0] if manager_info else "unknown"
    config_path = cwd / "cdt.yaml"
    checks: list[dict[str, Any]] = [
        {"name": "python", "ok": sys.version_info >= (3, 10), "message": sys.version.split()[0]},
        {"name": "cdt", "ok": True, "message": __version__},
        {"name": "install_manager", "ok": manager != "unknown", "message": manager, "critical": False},
        {
            "name": "pipx",
            "ok": shutil.which("pipx") is not None,
            "message": "in PATH" if shutil.which("pipx") else "not found",
            "critical": False,
        },
        _github_check(),
        {"name": "cdt_yaml_exists", "ok": config_path.exists(), "message": str(config_path)},
    ]

    if config_path.exists():
        try:
            load_pipeline_config(cwd)
        except typer.BadParameter as exc:
            checks.append({"name": "cdt_yaml_valid", "ok": False, "message": str(exc), "critical": True})
        else:
            checks.append({"name": "cdt_yaml_valid", "ok": True, "message": "valid", "critical": True})
    else:
        checks.append({"name": "cdt_yaml_valid", "ok": False, "message": "missing cdt.yaml", "critical": True})

    critical_failed = any(not check["ok"] and check.get("critical", True) for check in checks)
    return {"status": "ok" if not critical_failed else "error", "checks": checks}


def run_doctor(cwd: Path, *, json_output: bool = False) -> int:
    payload = doctor_payload(cwd)
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    else:
        typer.echo("CDT doctor")
        for check in payload["checks"]:
            marker = "OK" if check["ok"] else "FAIL"
            typer.echo(f"[{marker}] {check['name']}: {check['message']}")
    return 0 if payload["status"] == "ok" else 1


def _github_check() -> dict[str, Any]:
    try:
        request = urllib.request.Request(
            "https://api.github.com/rate_limit",
            headers={"User-Agent": f"cdt/{__version__}"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            ok = 200 <= getattr(response, "status", 200) < 400
        return {
            "name": "github_api",
            "ok": ok,
            "message": "reachable" if ok else "unexpected response",
            "critical": False,
        }
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"name": "github_api", "ok": False, "message": f"unreachable: {exc}", "critical": False}
