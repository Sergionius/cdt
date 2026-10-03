from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from ..services.webhook import ENV_KEY_RE
from .config import ParallelSpec, PipelineConfig, PipelineItemSpec, SequenceSpec
from .registry import get_step_metadata
from .validation import pipeline_names, validate_pipeline


def _webhook_env_keys(options: dict[str, Any]) -> set[str]:
    """Dynamically selected env keys of a ``notify.webhook`` leaf.

    Only literal names are statically checkable: interpolated env key names
    are validated and resolved when the step runs. Names only - the values are
    never read here and no request is sent.
    """
    keys: set[str] = set()
    for option in ("url_env", "authorization_env"):
        value = options.get(option)
        if isinstance(value, str) and ENV_KEY_RE.fullmatch(value.strip()) is not None:
            keys.add(value.strip())
    return keys


def preflight_payload(
    config: PipelineConfig, name: str, env: dict[str, str], *, cwd: Path | None = None
) -> dict[str, Any]:
    errors = validate_pipeline(config, name)
    pipeline = config.pipelines.get(name)
    tools: set[str] = set()
    env_keys: set[str] = set()
    firebase_auth_required = False
    google_play_adc: str | None = None
    if pipeline is not None and not errors:
        # Static preflight has no input set: inspect every declared leaf,
        # including conditional ones. Conditions never waive risk validation.
        for step in _iter_steps(pipeline.steps):
            metadata = get_step_metadata(step.name)
            tools.update(metadata.external_tools)
            if step.name != "notify.prod_user_agent" or env.get("NOTIFY_PROVIDER", "").strip().lower() == "pachca":
                env_keys.update(metadata.requires_env)
            if step.name == "notify.webhook":
                env_keys.update(_webhook_env_keys(step.options))
            if step.name == "firebase.upload_app_distribution":
                firebase_auth_required = True
            if step.name == "google_play.upload_aab":
                google_play_adc = (env.get("GOOGLE_APPLICATION_CREDENTIALS") or "").strip()

    tool_checks = [{"name": tool, "available": shutil.which(tool) is not None} for tool in sorted(tools)]
    env_checks = [{"name": key, "present": bool(env.get(key, "").strip())} for key in sorted(env_keys)]
    if firebase_auth_required:
        env_checks.append(
            {
                "name": "FIREBASE_TOKEN or GOOGLE_APPLICATION_CREDENTIALS",
                "present": bool(
                    env.get("FIREBASE_TOKEN", "").strip() or env.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
                ),
            }
        )
    # Google Play: an explicitly configured ADC file is checked read-only. An
    # unset variable is not an error (ADC may come from the CI environment);
    # credentials alone never prove Google Play Console permissions.
    if google_play_adc:
        adc_path = Path(google_play_adc).expanduser()
        if not adc_path.is_absolute():
            adc_path = (cwd if cwd is not None else Path.cwd()) / adc_path
        env_checks.append(
            {"name": f"{google_play_adc} (GOOGLE_APPLICATION_CREDENTIALS ADC file)", "present": adc_path.is_file()}
        )
    missing_tools = [check["name"] for check in tool_checks if not check["available"]]
    missing_env = [check["name"] for check in env_checks if not check["present"]]
    status = "ok" if not errors and not missing_tools and not missing_env else "error"
    return {
        "schema_version": 1,
        "pipeline": name,
        "pipelines": pipeline_names(config),
        "plugins": list(config.plugins),
        "status": status,
        "tools": tool_checks,
        "env": env_checks,
        "missing_tools": missing_tools,
        "missing_env": missing_env,
        "errors": errors,
    }


def _iter_steps(items: list[PipelineItemSpec]):
    for item in items:
        if isinstance(item, (ParallelSpec, SequenceSpec)):
            yield from _iter_steps(item.steps)
        else:
            yield item
