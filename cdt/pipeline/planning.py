from __future__ import annotations

from typing import Any

from .config import ParallelSpec, PipelineConfig, PipelineItemSpec, StepSpec
from .registry import StepMetadata, get_step_metadata
from .validation import pipeline_names, validate_pipeline

_RISK_ORDER = {
    "safe": 0,
    "artifact": 1,
    "build": 2,
    "hook": 3,
    "upload": 4,
    "deploy": 5,
    "push": 6,
    "custom": 7,
}


def plan_payload(config: PipelineConfig, name: str, *, errors: list[dict[str, str]] | None = None) -> dict[str, Any]:
    validation_errors = errors if errors is not None else validate_pipeline(config, name)
    pipeline = config.pipelines.get(name)
    warnings: list[dict[str, str]] = []
    steps = [] if pipeline is None else [_plan_node(item, warnings) for item in pipeline.steps]
    return {
        "schema_version": 1,
        "pipeline": name,
        "pipelines": pipeline_names(config),
        "plugins": list(config.plugins),
        "overall_risk": _aggregate_risks([_node_risk(step) for step in steps]),
        "steps": steps,
        "warnings": warnings,
        "errors": validation_errors,
    }


def _plan_node(item: PipelineItemSpec, warnings: list[dict[str, str]]) -> dict[str, Any]:
    if isinstance(item, ParallelSpec):
        steps = [_plan_step(step, warnings) for step in item.steps]
        return {
            "type": "parallel",
            "risk": _aggregate_risks([_node_risk(step) for step in steps]),
            "steps": steps,
        }
    return _plan_step(item, warnings)


def _plan_step(step: StepSpec, warnings: list[dict[str, str]]) -> dict[str, Any]:
    try:
        metadata = get_step_metadata(step.name)
    except Exception:
        metadata = StepMetadata(name=step.name)
    if metadata.risk == "custom":
        warnings.append(
            {
                "code": "custom_step_risk",
                "message": f"Step {step.name} has custom or incomplete metadata.",
                "path": step.name,
            }
        )
    payload = metadata.to_dict()
    return {
        "type": "step",
        "name": step.name,
        "category": metadata.category,
        "risk": metadata.risk,
        "options": step.options,
        "metadata": payload,
    }


def _node_risk(node: dict[str, Any]) -> str:
    return str(node.get("risk", "custom"))


def _aggregate_risks(risks: list[str]) -> str:
    if not risks:
        return "safe"
    return max(risks, key=lambda risk: _RISK_ORDER.get(risk, _RISK_ORDER["custom"]))
