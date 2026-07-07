from __future__ import annotations

import shutil
from typing import Any

from .config import ParallelSpec, PipelineConfig, PipelineItemSpec
from .registry import get_step_metadata
from .validation import pipeline_names, validate_pipeline


def preflight_payload(config: PipelineConfig, name: str, env: dict[str, str]) -> dict[str, Any]:
    errors = validate_pipeline(config, name)
    pipeline = config.pipelines.get(name)
    tools: set[str] = set()
    env_keys: set[str] = set()
    if pipeline is not None and not errors:
        for step in _iter_steps(pipeline.steps):
            metadata = get_step_metadata(step.name)
            tools.update(metadata.external_tools)
            env_keys.update(metadata.requires_env)

    tool_checks = [
        {"name": tool, "available": shutil.which(tool) is not None}
        for tool in sorted(tools)
    ]
    env_checks = [
        {"name": key, "present": bool(env.get(key, "").strip())}
        for key in sorted(env_keys)
    ]
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
        if isinstance(item, ParallelSpec):
            yield from item.steps
        else:
            yield item
