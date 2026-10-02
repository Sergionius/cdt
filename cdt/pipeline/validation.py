from __future__ import annotations

import inspect
from difflib import get_close_matches
from typing import Any

from .config import ParallelSpec, PipelineConfig, PipelineItemSpec, PipelineSpec, SequenceSpec, StepSpec
from .registry import get_step_factory, get_step_metadata, list_step_metadata, list_steps


def pipeline_names(config: PipelineConfig) -> list[str]:
    return sorted(config.pipelines)


def step_tree(items: list[PipelineItemSpec]) -> list[dict[str, Any]]:
    return [_step_node(item, str(index)) for index, item in enumerate(items)]


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


def _google_play_risk_error(step_name: str, pipeline_risk: str, path: str) -> dict[str, str]:
    return {
        "code": "production_risk_required",
        "message": (
            f"Step {step_name} publishes to Google Play and requires pipeline risk: production "
            f"(declared risk: {pipeline_risk!r})."
        ),
        "path": path,
    }


def declared_inputs_payload(pipeline: PipelineSpec | None) -> dict[str, dict[str, Any]]:
    """Declarations only: never include runtime input values."""
    if pipeline is None:
        return {}
    return {
        name: ({"required": spec.required, "pattern": spec.pattern} if spec.pattern else {"required": spec.required})
        for name, spec in pipeline.inputs.items()
    }


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
        "declared_risk": pipeline.risk if pipeline is not None else None,
        "inputs": declared_inputs_payload(pipeline),
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
        "steps": [metadata.to_dict() for metadata in list_step_metadata()],
        "errors": errors or [],
    }


def _validate_steps(pipeline: PipelineSpec) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    for index, item in enumerate(pipeline.steps):
        path = f"pipelines.{pipeline.name}.steps[{index}]"
        errors.extend(_validate_item(item, path, pipeline.risk))
    return errors


def _validate_item(item: PipelineItemSpec, path: str, pipeline_risk: str) -> list[dict[str, str]]:
    if isinstance(item, (ParallelSpec, SequenceSpec)):
        group_name = "parallel" if isinstance(item, ParallelSpec) else "sequence"
        errors: list[dict[str, str]] = []
        for child_index, child in enumerate(item.steps):
            errors.extend(_validate_item(child, f"{path}.{group_name}.steps[{child_index}]", pipeline_risk))
        return errors
    return _validate_step(item, path, pipeline_risk)


def _validate_step(step: StepSpec, path: str, pipeline_risk: str) -> list[dict[str, str]]:
    try:
        factory = get_step_factory(step.name)
    except Exception as exc:
        return [{"code": "unknown_step", "message": str(exc), "path": path}]
    errors: list[dict[str, str]] = []
    # Any Google Play step demands a production pipeline, recursively through
    # sequence/parallel groups; a dynamic track cannot bypass this protection.
    if get_step_metadata(step.name).category == "google_play" and pipeline_risk != "production":
        errors.append(_google_play_risk_error(step.name, pipeline_risk, path))
    errors.extend(_validate_step_options(step, factory, path))
    return errors


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


def _step_node(item: PipelineItemSpec, step_id: str) -> dict[str, Any]:
    if isinstance(item, (ParallelSpec, SequenceSpec)):
        return {
            "type": "parallel" if isinstance(item, ParallelSpec) else "sequence",
            "step_id": step_id,
            "steps": [_step_node(step, f"{step_id}/{child_index}") for child_index, step in enumerate(item.steps)],
        }
    return {
        "type": "step",
        "step_id": step_id,
        "name": item.name,
        "options": item.options,
    }
