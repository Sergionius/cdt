from __future__ import annotations

from dataclasses import dataclass, field
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

_ARTIFACT_NAME_PRODUCING_TYPES = {"artifact", "ios_ipa", "android_aab", "android_apk", "web_build"}


@dataclass
class _PlanState:
    available_names: set[str] = field(default_factory=set)
    available_types: set[str] = field(default_factory=set)

    def copy(self) -> "_PlanState":
        return _PlanState(set(self.available_names), set(self.available_types))

    def add_flow(self, flow: dict[str, list[str]]) -> None:
        self.available_names.update(flow["produces_names"])
        self.available_types.update(flow["produces_types"])


def plan_payload(config: PipelineConfig, name: str, *, errors: list[dict[str, str]] | None = None) -> dict[str, Any]:
    validation_errors = errors if errors is not None else validate_pipeline(config, name)
    pipeline = config.pipelines.get(name)
    warnings: list[dict[str, str]] = []
    steps = [] if pipeline is None else _plan_sequence(pipeline.steps, warnings, f"pipelines.{pipeline.name}.steps")
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


def _plan_sequence(
    items: list[PipelineItemSpec],
    warnings: list[dict[str, str]],
    path_prefix: str,
) -> list[dict[str, Any]]:
    state = _PlanState()
    nodes: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        node = _plan_node(item, warnings, state, f"{path_prefix}[{index}]")
        nodes.append(node)
    return nodes


def _plan_node(
    item: PipelineItemSpec,
    warnings: list[dict[str, str]],
    state: _PlanState,
    path: str,
) -> dict[str, Any]:
    if isinstance(item, ParallelSpec):
        before_group = state.copy()
        planned_steps = [
            _plan_step(step, warnings, f"{path}.parallel.steps[{index}]") for index, step in enumerate(item.steps)
        ]
        sibling_produced_names = set().union(
            *(set(step["artifact_flow"]["produces_names"]) for step in planned_steps)
        )
        for step in planned_steps:
            step_flow = step["artifact_flow"]
            produced_by_other_siblings = sibling_produced_names - set(step_flow["produces_names"])
            _warn_for_missing_artifacts(
                step["name"],
                step_flow,
                before_group,
                warnings,
                step["path"],
                sibling_produced_names=produced_by_other_siblings,
            )
        for step in planned_steps:
            state.add_flow(step["artifact_flow"])
        return {
            "type": "parallel",
            "risk": _aggregate_risks([_node_risk(step) for step in planned_steps]),
            "steps": [_strip_internal_path(step) for step in planned_steps],
        }
    node = _plan_step(item, warnings, path)
    _warn_for_missing_artifacts(item.name, node["artifact_flow"], state, warnings, path)
    state.add_flow(node["artifact_flow"])
    return _strip_internal_path(node)


def _plan_step(step: StepSpec, warnings: list[dict[str, str]], path: str) -> dict[str, Any]:
    try:
        metadata = get_step_metadata(step.name)
    except Exception:
        metadata = StepMetadata(name=step.name)
    if metadata.risk == "custom":
        warnings.append(
            {
                "code": "custom_step_risk",
                "message": f"Step {step.name} has custom or incomplete metadata.",
                "path": path,
            }
        )
    artifact_flow = _artifact_flow(step, metadata)
    return {
        "type": "step",
        "name": step.name,
        "category": metadata.category,
        "risk": metadata.risk,
        "options": step.options,
        "metadata": _compact_metadata(metadata),
        "artifact_flow": artifact_flow,
        "path": path,
    }


def _compact_metadata(metadata: StepMetadata) -> dict[str, Any]:
    payload = metadata.to_dict()
    for duplicated_key in ("name", "category", "risk"):
        payload.pop(duplicated_key, None)
    return payload


def _artifact_flow(step: StepSpec, metadata: StepMetadata) -> dict[str, list[str]]:
    artifact_name = _static_artifact_name(step.options)
    requires_names: list[str] = []
    produces_names: list[str] = []

    if artifact_name is not None and metadata.requires_artifacts:
        requires_names.append(artifact_name)
    if artifact_name is not None and set(metadata.produces) & _ARTIFACT_NAME_PRODUCING_TYPES:
        produces_names.append(artifact_name)

    return {
        "requires_names": requires_names,
        "produces_names": produces_names,
        "requires_types": list(metadata.requires_artifacts),
        "produces_types": list(metadata.produces),
    }


def _static_artifact_name(options: dict[str, Any]) -> str | None:
    value = options.get("artifact")
    if not isinstance(value, str) or "${" in value:
        return None
    return value


def _warn_for_missing_artifacts(
    step_name: str,
    artifact_flow: dict[str, list[str]],
    state: _PlanState,
    warnings: list[dict[str, str]],
    path: str,
    *,
    sibling_produced_names: set[str] | None = None,
) -> None:
    sibling_produced_names = sibling_produced_names or set()
    for artifact_name in artifact_flow["requires_names"]:
        if artifact_name in state.available_names:
            continue
        if artifact_name in sibling_produced_names:
            warnings.append(
                {
                    "code": "parallel_artifact_dependency",
                    "message": (
                        f"Step {step_name} requires artifact name {artifact_name} from the same parallel group; "
                        "parallel branches start together."
                    ),
                    "path": path,
                }
            )
            continue
        warnings.append(
            {
                "code": "missing_required_artifact",
                "message": (
                    f"Step {step_name} requires artifact name {artifact_name}, "
                    "but no previous step declares it."
                ),
                "path": path,
            }
        )


def _strip_internal_path(node: dict[str, Any]) -> dict[str, Any]:
    node.pop("path", None)
    return node


def _node_risk(node: dict[str, Any]) -> str:
    return str(node.get("risk", "custom"))


def _aggregate_risks(risks: list[str]) -> str:
    if not risks:
        return "safe"
    return max(risks, key=lambda risk: _RISK_ORDER.get(risk, _RISK_ORDER["custom"]))
