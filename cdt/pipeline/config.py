from __future__ import annotations

import importlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import typer

from ..versioning import _current_flutter_version
from .context import PipelineContext
from .executor import ParallelStepGroup
from .registry import get_step_factory

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - exercised only without dependency installed.
    yaml = None

_INTERPOLATION_RE = re.compile(r"\$\{([^}]+)\}")


@dataclass(frozen=True)
class StepSpec:
    name: str
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ParallelSpec:
    steps: list[StepSpec]


PipelineItemSpec = StepSpec | ParallelSpec


@dataclass(frozen=True)
class PipelineSpec:
    name: str
    steps: list[PipelineItemSpec]


@dataclass(frozen=True)
class PipelineConfig:
    path: Path
    plugins: list[str]
    pipelines: dict[str, PipelineSpec]


@dataclass
class ConfiguredStep:
    name: str
    options: dict[str, Any]

    def run(self, ctx: PipelineContext) -> None:
        resolved_options = resolve_value(self.options, ctx)
        step = get_step_factory(self.name)(**resolved_options)
        step.run(ctx)


def load_pipeline_config(cwd: Path, filename: str = "cdt.yaml") -> PipelineConfig:
    path = cwd / filename
    if not path.exists():
        raise typer.BadParameter(f"Pipeline config not found: {path}. See examples/cdt.yaml.")
    if yaml is None:
        raise typer.BadParameter("PyYAML is required to read cdt.yaml. Install package dependency: PyYAML")

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:  # type: ignore[attr-defined]
        mark = getattr(exc, "problem_mark", None)
        location = f" line {mark.line + 1}, column {mark.column + 1}" if mark is not None else ""
        example = (
            "Example: version: 1\\npipelines:\\n  test:\\n    steps:\\n"
            "      - hook.python_script: {script: scripts/test.py}"
        )
        raise typer.BadParameter(f"YAML parse error in {path}{location}: {exc}. {example}") from exc
    if not isinstance(data, dict):
        raise typer.BadParameter("cdt.yaml must contain a mapping")

    allowed_top_level = {"version", "plugins", "pipelines"}
    unknown_top_level = sorted(set(data) - allowed_top_level)
    if unknown_top_level:
        raise typer.BadParameter("Unsupported top-level cdt.yaml fields: " + ", ".join(unknown_top_level))

    version = data.get("version")
    if version != 1:
        raise typer.BadParameter("cdt.yaml version must be 1")

    raw_plugins = data.get("plugins", [])
    if raw_plugins is None:
        raw_plugins = []
    if not isinstance(raw_plugins, list) or not all(isinstance(item, str) for item in raw_plugins):
        raise typer.BadParameter("cdt.yaml plugins must be a list of module names")

    raw_pipelines = data.get("pipelines")
    if not isinstance(raw_pipelines, dict) or not raw_pipelines:
        raise typer.BadParameter("cdt.yaml pipelines must be a non-empty mapping")

    pipelines: dict[str, PipelineSpec] = {}
    for pipeline_name, pipeline_data in raw_pipelines.items():
        if not isinstance(pipeline_name, str) or not pipeline_name.strip():
            raise typer.BadParameter("Pipeline names must be non-empty strings")
        if not isinstance(pipeline_data, dict):
            raise typer.BadParameter(f"Pipeline '{pipeline_name}' must be a mapping")
        raw_steps = pipeline_data.get("steps")
        if not isinstance(raw_steps, list):
            raise typer.BadParameter(f"Pipeline '{pipeline_name}' steps must be a list")
        pipelines[pipeline_name] = PipelineSpec(
            name=pipeline_name,
            steps=[_parse_step_spec(pipeline_name, index, item) for index, item in enumerate(raw_steps, start=1)],
        )

    return PipelineConfig(path=path, plugins=list(raw_plugins), pipelines=pipelines)


def load_plugins(plugins: list[str]) -> None:
    for plugin in plugins:
        try:
            importlib.import_module(plugin)
        except Exception as exc:
            raise typer.BadParameter(f"Failed to import pipeline plugin '{plugin}': {exc}") from exc


def configured_steps(pipeline: PipelineSpec) -> list[ConfiguredStep | ParallelStepGroup]:
    configured = []
    for item in pipeline.steps:
        if isinstance(item, ParallelSpec):
            configured.append(ParallelStepGroup([ConfiguredStep(step.name, step.options) for step in item.steps]))
        else:
            configured.append(ConfiguredStep(item.name, item.options))
    return configured


def resolve_value(value: Any, ctx: PipelineContext) -> Any:
    if isinstance(value, str):
        return _INTERPOLATION_RE.sub(lambda match: str(_resolve_expression(match.group(1), ctx)), value)
    if isinstance(value, list):
        return [resolve_value(item, ctx) for item in value]
    if isinstance(value, dict):
        return {key: resolve_value(item, ctx) for key, item in value.items()}
    return value


def _parse_step_spec(pipeline_name: str, index: int, item: Any) -> PipelineItemSpec:
    prefix = f"Pipeline '{pipeline_name}' step #{index}"
    if isinstance(item, str):
        return StepSpec(name=item)
    if isinstance(item, dict) and len(item) == 1:
        name, options = next(iter(item.items()))
        if not isinstance(name, str) or not name.strip():
            raise typer.BadParameter(f"{prefix} name must be a non-empty string")
        if name == "parallel":
            return _parse_parallel_spec(pipeline_name, index, options)
        if options is None:
            options = {}
        if not isinstance(options, dict):
            raise typer.BadParameter(f"{prefix} options must be a mapping")
        return StepSpec(name=name, options=options)
    raise typer.BadParameter(f"{prefix} must be a step name or a single-key mapping")


def _parse_parallel_spec(pipeline_name: str, index: int, options: Any) -> ParallelSpec:
    prefix = f"Pipeline '{pipeline_name}' step #{index} parallel"
    if not isinstance(options, dict):
        raise typer.BadParameter(f"{prefix} must be a mapping with a non-empty steps list")
    if set(options) != {"steps"}:
        raise typer.BadParameter(f"{prefix} must contain only the steps key")
    raw_steps = options.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise typer.BadParameter(f"{prefix}.steps must be a non-empty list")

    steps: list[StepSpec] = []
    for child_index, child in enumerate(raw_steps, start=1):
        child_prefix = f"{prefix}.steps #{child_index}"
        if isinstance(child, dict) and len(child) == 1 and next(iter(child)) == "parallel":
            raise typer.BadParameter(f"{child_prefix} cannot be a nested parallel group in YAML v1")
        parsed = _parse_step_spec(pipeline_name, index, child)
        if isinstance(parsed, ParallelSpec):
            raise typer.BadParameter(f"{child_prefix} cannot be a nested parallel group in YAML v1")
        steps.append(parsed)
    return ParallelSpec(steps=steps)


def _resolve_expression(expression: str, ctx: PipelineContext) -> str:
    key = expression.strip()
    if key == "ids":
        return ", ".join(ctx.ids)
    if key == "flutter.version":
        return ctx.values.get("flutter.version") or _current_flutter_version(ctx.cwd)
    if key.startswith("values."):
        value_key = key.removeprefix("values.")
        try:
            return ctx.values[value_key]
        except KeyError as exc:
            raise typer.BadParameter(f"Missing pipeline value: {value_key}") from exc
    if key.startswith("artifact."):
        parts = key.split(".")
        if len(parts) != 3:
            raise typer.BadParameter(f"Invalid artifact interpolation: ${{{key}}}")
        artifact = ctx.artifact(parts[1])
        attr = parts[2]
        if attr == "path":
            return str(artifact.path)
        if attr == "kind":
            return str(artifact.kind.value)
        if attr == "label":
            return artifact.label
        raise typer.BadParameter(f"Unknown artifact interpolation attribute: {attr}")
    return ctx.require_env(key)
