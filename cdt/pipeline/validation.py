from __future__ import annotations

import inspect
from difflib import get_close_matches
from typing import Any

from .config import ParallelSpec, PipelineConfig, PipelineItemSpec, PipelineSpec, StepSpec
from .registry import get_step_factory, list_steps


def pipeline_names(config: PipelineConfig) -> list[str]:
    return sorted(config.pipelines)


def step_tree(items: list[PipelineItemSpec]) -> list[dict[str, Any]]:
    return [_step_node(item) for item in items]


def validate_pipeline(config: PipelineConfig, name: str | None = None) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    if name is not None:
        try:
            pipelines = [config.pipelines[name]]
        except KeyError:
            return [
                {
                    "code": "unknown_pipeline",
                    "message": f"Unknown pipeline: {name}",
                    "path": f"pipelines.{name}",
                }
            ]
    else:
        pipelines = [config.pipelines[pipeline_name] for pipeline_name in pipeline_names(config)]

    for pipeline in pipelines:
        errors.extend(_validate_steps(pipeline))
    return errors


def inspect_payload(
    config: PipelineConfig,
    name: str,
    *,
    errors: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    pipeline = config.pipelines.get(name)
    return {
        "schema_version": 1,
        "pipeline": name,
        "pipelines": pipeline_names(config),
        "plugins": list(config.plugins),
        "steps": step_tree(pipeline.steps) if pipeline is not None else [],
        "registered_steps": list_steps(),
        "errors": errors or [],
    }


def validate_payload(
    config: PipelineConfig,
    name: str | None,
    *,
    errors: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "pipeline": name,
        "pipelines": pipeline_names(config),
        "plugins": list(config.plugins),
        "registered_steps": list_steps(),
        "errors": errors or [],
    }


def steps_payload(config: PipelineConfig | None, *, errors: list[dict[str, str]] | None = None) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "pipelines": pipeline_names(config) if config is not None else [],
        "plugins": list(config.plugins) if config is not None else [],
        "registered_steps": list_steps(),
        "errors": errors or [],
    }


def _validate_steps(pipeline: PipelineSpec) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    for index, item in enumerate(pipeline.steps):
        path = f"pipelines.{pipeline.name}.steps[{index}]"
        if isinstance(item, ParallelSpec):
            for child_index, child in enumerate(item.steps):
                errors.extend(_validate_step(child, f"{path}.parallel.steps[{child_index}]"))
        else:
            errors.extend(_validate_step(item, path))
    return errors


def _validate_step(step: StepSpec, path: str) -> list[dict[str, str]]:
    try:
        factory = get_step_factory(step.name)
    except Exception as exc:
        return [{"code": "unknown_step", "message": str(exc), "path": path}]
    return _validate_step_options(step, factory, path)


def _validate_step_options(step: StepSpec, factory: Any, path: str) -> list[dict[str, str]]:
    try:
        signature = inspect.signature(factory)
    except (TypeError, ValueError):
        return []

    parameters = signature.parameters
    if any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
        return []

    allowed_options = {
        name
        for name, parameter in parameters.items()
        if parameter.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }
    errors: list[dict[str, str]] = []
    for option in sorted(set(step.options) - allowed_options):
        message = f"Unknown option '{option}' for step {step.name}."
        if option == "env" and "profile" in allowed_options:
            message += " Use 'profile' instead."
        else:
            matches = get_close_matches(option, sorted(allowed_options), n=1)
            if matches:
                message += f" Did you mean '{matches[0]}'?"
        errors.append({"code": "unknown_step_option", "message": message, "path": f"{path}.{option}"})
    return errors


def _step_node(item: PipelineItemSpec) -> dict[str, Any]:
    if isinstance(item, ParallelSpec):
        return {
            "type": "parallel",
            "steps": [_step_node(step) for step in item.steps],
        }
    return {
        "type": "step",
        "name": item.name,
        "options": item.options,
    }
