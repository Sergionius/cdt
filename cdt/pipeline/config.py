from __future__ import annotations

import importlib
import math
import os
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import typer

from ..runner import supports_process_groups
from ..versioning import _current_flutter_version
from .context import PipelineContext
from .executor import ParallelStepGroup, SequentialStepGroup
from .policy import MAX_ATTEMPTS_LIMIT, MAX_DELAY_SECONDS, RetryableStepError, RetryPolicy, run_with_retry_policy
from .registry import get_step_factory, get_step_metadata

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - exercised only without dependency installed.
    yaml = None

_INTERPOLATION_RE = re.compile(r"\$\{([^}]+)\}")
_INPUT_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
_INPUT_FIELDS = {"required", "pattern"}


@dataclass(frozen=True)
class StepSpec:
    name: str
    options: dict[str, Any] = field(default_factory=dict)
    when: dict[str, Any] | None = None
    retry: RetryPolicy | None = None
    timeout_seconds: float | None = None


@dataclass(frozen=True)
class SequenceSpec:
    steps: list[StepSpec]


@dataclass(frozen=True)
class ParallelSpec:
    steps: list[StepSpec | SequenceSpec]


PipelineItemSpec = StepSpec | ParallelSpec | SequenceSpec


@dataclass(frozen=True)
class InputSpec:
    name: str
    required: bool = False
    pattern: str | None = None


@dataclass(frozen=True)
class PipelineSpec:
    name: str
    steps: list[PipelineItemSpec]
    risk: str = "standard"
    inputs: dict[str, InputSpec] = field(default_factory=dict)


@dataclass(frozen=True)
class PipelineConfig:
    path: Path
    plugins: list[str]
    pipelines: dict[str, PipelineSpec]


@dataclass
class ConfiguredStep:
    name: str
    options: dict[str, Any]
    step_id: str | None = None
    when: dict[str, Any] | None = None
    retry: RetryPolicy | None = None
    timeout_seconds: float | None = None

    def run(self, ctx: PipelineContext) -> None:
        resolved_options = resolve_value(self.options, ctx)
        self._inject_timeout_option(resolved_options)
        policy = self.retry or RetryPolicy()
        if policy.enabled:
            # Defense in depth: validation already rejects retries for steps
            # without the explicit retry_safe capability.
            if not get_step_metadata(self.name).retry_safe:
                raise typer.BadParameter(
                    f"Step {self.name} is not declared retry_safe; "
                    "retry.max_attempts > 1 requires an explicit retry-safe step"
                )
        attempt_id = self.step_id or self.name

        def attempt() -> None:
            # A fresh runtime instance per attempt: the previous instance may
            # keep state mutated by the failed attempt.
            step = get_step_factory(self.name)(**resolved_options)
            step.run(ctx)

        def on_retryable_failure(count: int, exc: RetryableStepError) -> None:
            ctx.mark_step_retry(attempt_id, count, str(exc))

        try:
            run_with_retry_policy(policy, attempt, on_retryable_failure=on_retryable_failure, sleep=time.sleep)
        except RetryableStepError as exc:
            if policy.enabled:
                # Exhaustion is terminal: keep the final attempts count recorded.
                ctx.mark_step_retry(attempt_id, policy.max_attempts, str(exc))
            raise

    def _inject_timeout_option(self, resolved_options: dict[str, Any]) -> None:
        """Deliver the envelope timeout into the declared native option.

        Validation already rejects missing capabilities, ambiguous settings and
        unsupported platforms; these runtime checks are defense in depth.
        """
        if self.timeout_seconds is None:
            return
        timeout_option = get_step_metadata(self.name).timeout_option
        if not timeout_option:
            raise typer.BadParameter(
                f"Step {self.name} does not declare a native timeout parameter (timeout_option); "
                "timeout_seconds requires an explicit step capability"
            )
        if timeout_option in resolved_options:
            raise typer.BadParameter(
                f"Step {self.name} received both timeout_seconds and with.{timeout_option}; "
                "remove one to make the timeout unambiguous"
            )
        if not supports_process_groups():
            raise typer.BadParameter(
                f"timeout_seconds requires a platform with POSIX process groups; this platform ({os.name}) "
                f"cannot guarantee process-tree termination for {self.name}"
            )
        resolved_options[timeout_option] = self.timeout_seconds


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
        unknown_pipeline_fields = sorted(set(pipeline_data) - {"steps", "risk", "inputs"})
        if unknown_pipeline_fields:
            raise typer.BadParameter(
                f"Pipeline '{pipeline_name}' has unsupported fields: " + ", ".join(unknown_pipeline_fields)
            )
        risk = pipeline_data.get("risk", "standard")
        if risk not in {"standard", "production"}:
            raise typer.BadParameter(f"Pipeline '{pipeline_name}' risk must be 'standard' or 'production'")
        raw_steps = pipeline_data.get("steps")
        if not isinstance(raw_steps, list):
            raise typer.BadParameter(f"Pipeline '{pipeline_name}' steps must be a list")
        raw_inputs = pipeline_data.get("inputs") or {}
        if not isinstance(raw_inputs, dict):
            raise typer.BadParameter(f"Pipeline '{pipeline_name}' inputs must be a mapping")
        pipelines[pipeline_name] = PipelineSpec(
            name=pipeline_name,
            steps=[_parse_step_spec(pipeline_name, index, item) for index, item in enumerate(raw_steps, start=1)],
            risk=risk,
            inputs={
                input_name: _parse_input_spec(pipeline_name, input_name, input_data)
                for input_name, input_data in raw_inputs.items()
            },
        )

    return PipelineConfig(path=path, plugins=list(raw_plugins), pipelines=pipelines)


def _parse_input_spec(pipeline_name: str, input_name: Any, input_data: Any) -> InputSpec:
    prefix = f"Pipeline '{pipeline_name}' input"
    if not isinstance(input_name, str) or _INPUT_NAME_RE.fullmatch(input_name) is None:
        raise typer.BadParameter(
            f"{prefix} name {input_name!r} is invalid; use letters, digits, '-' and '_' starting with a letter"
        )
    if input_data is None:
        return InputSpec(name=input_name)
    if not isinstance(input_data, dict):
        raise typer.BadParameter(f"{prefix} '{input_name}' must be a mapping")
    unknown_fields = sorted(set(input_data) - _INPUT_FIELDS)
    if unknown_fields:
        raise typer.BadParameter(f"{prefix} '{input_name}' has unsupported fields: " + ", ".join(unknown_fields))
    required = input_data.get("required", False)
    if not isinstance(required, bool):
        raise typer.BadParameter(f"{prefix} '{input_name}' required must be a boolean")
    pattern = input_data.get("pattern")
    if pattern is not None:
        if not isinstance(pattern, str) or not pattern.strip():
            raise typer.BadParameter(f"{prefix} '{input_name}' pattern must be a non-empty string")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise typer.BadParameter(f"{prefix} '{input_name}' pattern is not a valid regex: {exc}") from exc
    return InputSpec(name=input_name, required=required, pattern=pattern)


def parse_pipeline_inputs(entries: Sequence[str]) -> dict[str, str]:
    """Parse repeatable ``--input KEY=VALUE`` entries preserving order."""
    inputs: dict[str, str] = {}
    for entry in entries:
        if "=" not in entry:
            raise typer.BadParameter(f"Invalid --input entry '{entry}'. Use --input KEY=VALUE")
        key, value = entry.split("=", 1)
        if not key.strip():
            raise typer.BadParameter(f"Invalid --input entry '{entry}': key must not be empty")
        if key in inputs:
            raise typer.BadParameter(f"Duplicate --input key: {key}")
        inputs[key] = value
    return inputs


def validate_pipeline_inputs(pipeline: PipelineSpec, inputs: Mapping[str, str]) -> None:
    """Reject unknown, missing required, or pattern-violating pipeline inputs."""
    declared = pipeline.inputs
    unknown = sorted(set(inputs) - set(declared))
    if unknown:
        available = ", ".join(sorted(declared)) or "none"
        raise typer.BadParameter(
            f"Unknown pipeline input for '{pipeline.name}': {', '.join(unknown)}. Declared inputs: {available}"
        )
    missing = [name for name, spec in declared.items() if spec.required and not inputs.get(name, "").strip()]
    if missing:
        raise typer.BadParameter(f"Missing required pipeline input(s) for '{pipeline.name}': {', '.join(missing)}")
    for name, spec in declared.items():
        value = inputs.get(name)
        if value is None or spec.pattern is None:
            continue
        if re.fullmatch(spec.pattern, value) is None:
            raise typer.BadParameter(f"Pipeline input '{name}' does not match pattern '{spec.pattern}': {value!r}")


def load_plugins(plugins: list[str]) -> None:
    for plugin in plugins:
        try:
            importlib.import_module(plugin)
        except Exception as exc:
            raise typer.BadParameter(f"Failed to import pipeline plugin '{plugin}': {exc}") from exc


def evaluate_condition(when: dict[str, Any] | None, inputs: Mapping[str, str] | None) -> str:
    """Evaluate literal input conditions; None denotes an unknown input set."""
    if when is None:
        return "run"
    if inputs is None:
        return "unknown"
    value = inputs.get(when["input"])
    if "present" in when:
        matches = bool(value) == when["present"]
    elif "equals" in when:
        matches = value is not None and value == when["equals"]
    else:
        matches = value is None or value != when["not_equals"]
    return "run" if matches else "skip"


def configured_steps(pipeline: PipelineSpec) -> list[ConfiguredStep | ParallelStepGroup | SequentialStepGroup]:
    return [_configured_item(item, str(index)) for index, item in enumerate(pipeline.steps)]


def _configured_item(
    item: PipelineItemSpec,
    step_id: str,
) -> ConfiguredStep | ParallelStepGroup | SequentialStepGroup:
    if isinstance(item, ParallelSpec):
        children = [_configured_item(child, f"{step_id}/{index}") for index, child in enumerate(item.steps)]
        return ParallelStepGroup(children, step_id=step_id)
    if isinstance(item, SequenceSpec):
        children = [
            ConfiguredStep(step.name, step.options, f"{step_id}/{index}", step.when, step.retry, step.timeout_seconds)
            for index, step in enumerate(item.steps)
        ]
        return SequentialStepGroup(children, step_id=step_id)
    return ConfiguredStep(item.name, item.options, step_id, item.when, item.retry, item.timeout_seconds)


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
    if isinstance(item, dict) and "step" in item:
        if set(item) - {"step", "with", "when", "retry", "timeout_seconds"}:
            raise typer.BadParameter(f"{prefix} has unsupported extended step fields")
        name = item["step"]
        if not isinstance(name, str) or not name.strip() or name in {"parallel", "sequence"}:
            raise typer.BadParameter(f"{prefix} step must be a leaf step name")
        options = item.get("with", {})
        if not isinstance(options, dict):
            raise typer.BadParameter(f"{prefix} with must be a mapping")
        if "when" in item:
            validate_condition(item["when"])
        retry = parse_retry_policy(item["retry"], prefix) if "retry" in item else None
        timeout_seconds = parse_timeout_seconds(item["timeout_seconds"], prefix) if "timeout_seconds" in item else None
        return StepSpec(name=name, options=options, when=item.get("when"), retry=retry, timeout_seconds=timeout_seconds)
    if isinstance(item, dict) and len(item) == 1:
        name, options = next(iter(item.items()))
        if not isinstance(name, str) or not name.strip():
            raise typer.BadParameter(f"{prefix} name must be a non-empty string")
        if name == "parallel":
            return _parse_parallel_spec(pipeline_name, index, options)
        if name == "sequence":
            return _parse_sequence_spec(pipeline_name, index, options)
        if options is None:
            options = {}
        if not isinstance(options, dict):
            raise typer.BadParameter(f"{prefix} options must be a mapping")
        return StepSpec(name=name, options=options)
    raise typer.BadParameter(f"{prefix} must be a step name or a single-key mapping")


def validate_condition(when: Any, inputs: Mapping[str, InputSpec] | None = None) -> None:
    if not isinstance(when, dict):
        raise typer.BadParameter("when must be a mapping")
    operators = set(when) - {"input"}
    if len(operators) != 1 or not operators <= {"equals", "not_equals", "present"}:
        raise typer.BadParameter("when requires exactly one operator: equals, not_equals or present")
    name = when.get("input")
    if not isinstance(name, str) or _INPUT_NAME_RE.fullmatch(name) is None:
        raise typer.BadParameter("when input must be an input name, without interpolation")
    if inputs is not None and name not in inputs:
        raise typer.BadParameter(f"when references undeclared input: {name}")
    operator = next(iter(operators))
    value = when[operator]
    if operator == "present":
        if not isinstance(value, bool):
            raise typer.BadParameter("when present must be a boolean")
    elif not isinstance(value, str) or "${" in value:
        raise typer.BadParameter(f"when {operator} must be a string without interpolation")


def parse_retry_policy(raw: Any, prefix: str) -> RetryPolicy:
    """Parse the extended-record ``retry`` block; constructor options never see it."""
    label = f"{prefix} retry"
    if not isinstance(raw, dict):
        raise typer.BadParameter(f"{label} must be a mapping")
    unknown_fields = sorted(set(raw) - {"max_attempts", "delay_seconds"})
    if unknown_fields:
        raise typer.BadParameter(f"{label} has unsupported fields: " + ", ".join(unknown_fields))
    max_attempts = raw.get("max_attempts", 1)
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
        raise typer.BadParameter(f"{label} max_attempts must be an integer, not a boolean or string")
    if not 1 <= max_attempts <= MAX_ATTEMPTS_LIMIT:
        raise typer.BadParameter(f"{label} max_attempts must be between 1 and {MAX_ATTEMPTS_LIMIT}")
    delay_seconds = raw.get("delay_seconds", 0)
    if isinstance(delay_seconds, bool) or not isinstance(delay_seconds, (int, float)):
        raise typer.BadParameter(f"{label} delay_seconds must be a number, not a boolean or string")
    if not math.isfinite(delay_seconds):
        raise typer.BadParameter(f"{label} delay_seconds must be a finite number")
    if not 0 <= delay_seconds <= MAX_DELAY_SECONDS:
        raise typer.BadParameter(f"{label} delay_seconds must be between 0 and {MAX_DELAY_SECONDS} seconds")
    return RetryPolicy(max_attempts=max_attempts, delay_seconds=float(delay_seconds))


def parse_timeout_seconds(raw: Any, prefix: str) -> float:
    """Parse the extended-record ``timeout_seconds``; constructor options never see it."""
    label = f"{prefix} timeout_seconds"
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise typer.BadParameter(f"{label} must be a number, not a boolean or string")
    if not math.isfinite(raw):
        raise typer.BadParameter(f"{label} must be a finite number")
    if raw <= 0:
        raise typer.BadParameter(f"{label} must be a positive number of seconds")
    return float(raw)


def _parse_parallel_spec(pipeline_name: str, index: int, options: Any) -> ParallelSpec:
    prefix = f"Pipeline '{pipeline_name}' step #{index} parallel"
    raw_steps = _group_steps(prefix, options)
    steps: list[StepSpec | SequenceSpec] = []
    for child_index, child in enumerate(raw_steps, start=1):
        child_prefix = f"{prefix}.steps #{child_index}"
        parsed = _parse_step_spec(pipeline_name, index, child)
        if isinstance(parsed, ParallelSpec):
            raise typer.BadParameter(f"{child_prefix} cannot be a nested parallel group in YAML v1")
        steps.append(parsed)
    return ParallelSpec(steps=steps)


def _parse_sequence_spec(pipeline_name: str, index: int, options: Any) -> SequenceSpec:
    prefix = f"Pipeline '{pipeline_name}' step #{index} sequence"
    raw_steps = _group_steps(prefix, options)
    steps: list[StepSpec] = []
    for child_index, child in enumerate(raw_steps, start=1):
        child_prefix = f"{prefix}.steps #{child_index}"
        parsed = _parse_step_spec(pipeline_name, index, child)
        if isinstance(parsed, (ParallelSpec, SequenceSpec)):
            raise typer.BadParameter(f"{child_prefix} cannot contain a nested group in YAML v1")
        steps.append(parsed)
    return SequenceSpec(steps=steps)


def _group_steps(prefix: str, options: Any) -> list[Any]:
    if not isinstance(options, dict):
        raise typer.BadParameter(f"{prefix} must be a mapping with a non-empty steps list")
    if set(options) != {"steps"}:
        raise typer.BadParameter(f"{prefix} must contain only the steps key")
    raw_steps = options.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise typer.BadParameter(f"{prefix}.steps must be a non-empty list")
    return raw_steps


def _resolve_expression(expression: str, ctx: PipelineContext) -> str:
    key = expression.strip()
    if key == "ids":
        return ", ".join(ctx.ids)
    if key == "flutter.version":
        return ctx.values.get("flutter.version") or _current_flutter_version(ctx.cwd)
    if key.startswith("inputs."):
        input_key = key.removeprefix("inputs.")
        try:
            return ctx.inputs[input_key]
        except KeyError as exc:
            raise typer.BadParameter(f"Missing pipeline input: {input_key}") from exc
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
