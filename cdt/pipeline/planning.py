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


@dataclass
class _PlanState:
    available_names: set[str] = field(default_factory=set)
    available_types: set[str] = field(default_factory=set)

    def copy(self) -> "_PlanState":
        return _PlanState(set(self.available_names), set(self.available_types))

    def add_flow(self, flow: dict[str, Any]) -> None:
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


def _artifact_flow(step: StepSpec, metadata: StepMetadata) -> dict[str, Any]:
    produces_names: list[str] = []
    produces_types: list[str] = []
    for production in metadata.produces:
        produces_types.append(production.result_type)
        produces_names.extend(_static_names_from_options(step.options, production.name_options))

    requires: list[dict[str, Any]] = []
    requires_names: list[str] = []
    for requirement in metadata.requires:
        names = _static_names_from_options(step.options, requirement.name_options)
        requires_names.extend(names)
        requires.append(
            {
                "types": list(requirement.result_types),
                "mode": requirement.mode,
                "names": names,
            }
        )

    return {
        "requires": requires,
        "requires_names": requires_names,
        "produces_names": produces_names,
        "produces_types": produces_types,
    }


def _static_names_from_options(options: dict[str, Any], name_options: tuple[str, ...]) -> list[str]:
    names: list[str] = []
    for option_name in name_options:
        value = options.get(option_name)
        if isinstance(value, str) and "${" not in value:
            names.append(value)
    return names


def _warn_for_missing_artifacts(
    step_name: str,
    artifact_flow: dict[str, Any],
    state: _PlanState,
    warnings: list[dict[str, str]],
    path: str,
    *,
    sibling_produced_names: set[str] | None = None,
) -> None:
    sibling_produced_names = sibling_produced_names or set()
    for requirement in artifact_flow["requires"]:
        for artifact_name in requirement["names"]:
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
