from collections.abc import Callable
from dataclasses import dataclass
from difflib import get_close_matches

import typer

from .step import Step

StepFactory = Callable[..., Step]


def _string_tuple(value: object, field_name: str) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    try:
        items = tuple(value)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ValueError(f"{field_name} must contain strings") from exc
    if not all(isinstance(item, str) and item for item in items):
        raise ValueError(f"{field_name} must contain non-empty strings")
    return items


@dataclass(frozen=True)
class ResultRequirement:
    """Describes a result/artifact that a step needs from previous steps.

    result_types: static result/artifact types the step consumes.
    mode:        "all" if every type is required, "any" if at least one is enough.
    name_options: YAML option keys whose values provide the configured artifact
                  name(s) for this requirement. Empty means no static name
                  inference is possible.
    """

    result_types: tuple[str, ...]
    mode: str = "all"
    name_options: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        result_types = _string_tuple(self.result_types, "ResultRequirement.result_types")
        if not result_types:
            raise ValueError("ResultRequirement.result_types cannot be empty")
        if self.mode not in {"all", "any"}:
            raise ValueError("ResultRequirement.mode must be 'all' or 'any'")
        object.__setattr__(self, "result_types", result_types)
        object.__setattr__(self, "name_options", _string_tuple(self.name_options, "ResultRequirement.name_options"))

    def to_dict(self) -> dict:
        return {
            "result_types": list(self.result_types),
            "mode": self.mode,
            "name_options": list(self.name_options),
        }


@dataclass(frozen=True)
class ResultProduction:
    """Describes a result/artifact that a step produces.

    result_type: static result/artifact type the step creates.
    name_options: YAML option keys whose values provide the configured artifact
                  name(s). Empty means the step does not expose a configurable
                  artifact name.
    """

    result_type: str
    name_options: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.result_type, str) or not self.result_type:
            raise ValueError("ResultProduction.result_type cannot be empty")
        object.__setattr__(self, "name_options", _string_tuple(self.name_options, "ResultProduction.name_options"))

    def to_dict(self) -> dict:
        return {
            "result_type": self.result_type,
            "name_options": list(self.name_options),
        }


@dataclass(frozen=True)
class StepMetadata:
    """Static step metadata used for planning and preflight checks.

    `requires` and `produces` use structured dataclasses. They describe static
    result/artifact types, not configured pipeline-local artifact names.
    """

    name: str
    description: str = ""
    category: str = "custom"
    risk: str = "custom"
    requires: tuple[ResultRequirement, ...] = ()
    produces: tuple[ResultProduction, ...] = ()
    external_tools: tuple[str, ...] = ()
    plugin: bool = False

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "risk": self.risk,
            "requires": [req.to_dict() for req in self.requires],
            "produces": [prod.to_dict() for prod in self.produces],
            "external_tools": list(self.external_tools),
            "plugin": self.plugin,
        }


_STEP_FACTORIES: dict[str, StepFactory] = {}
_STEP_METADATA: dict[str, StepMetadata] = {}


def register_step(name: str, factory: StepFactory, metadata: StepMetadata | None = None) -> StepFactory:
    step_name = name.strip()
    if not step_name:
        raise typer.BadParameter("Pipeline step name cannot be empty")
    if step_name in _STEP_FACTORIES:
        raise typer.BadParameter(f"Pipeline step is already registered: {step_name}")
    _STEP_FACTORIES[step_name] = factory
    _STEP_METADATA[step_name] = _normalize_metadata(step_name, metadata)
    return factory


def get_step_factory(name: str) -> StepFactory:
    try:
        return _STEP_FACTORIES[name]
    except KeyError as exc:
        steps = list_steps()
        available = ", ".join(steps) or "none"
        matches = get_close_matches(name, steps, n=3)
        hint = f" Did you mean: {', '.join(matches)}?" if matches else ""
        raise typer.BadParameter(f"Unknown pipeline step: {name}.{hint} Available steps: {available}") from exc


def list_steps() -> list[str]:
    return sorted(_STEP_FACTORIES)


def get_step_metadata(name: str) -> StepMetadata:
    if name in _STEP_METADATA:
        return _STEP_METADATA[name]
    if name in _STEP_FACTORIES:
        return _default_metadata(name)
    get_step_factory(name)
    raise AssertionError("unreachable")


def list_step_metadata() -> list[StepMetadata]:
    return [get_step_metadata(name) for name in list_steps()]


def _normalize_metadata(name: str, metadata: StepMetadata | None) -> StepMetadata:
    if metadata is None:
        return _default_metadata(name)
    return StepMetadata(
        name=name,
        description=metadata.description,
        category=metadata.category,
        risk=metadata.risk,
        requires=tuple(
            ResultRequirement(
                result_types=req.result_types,
                mode=req.mode,
                name_options=req.name_options,
            )
            for req in metadata.requires
        ),
        produces=tuple(
            ResultProduction(
                result_type=prod.result_type,
                name_options=prod.name_options,
            )
            for prod in metadata.produces
        ),
        external_tools=tuple(metadata.external_tools),
        plugin=metadata.plugin,
    )


def _default_metadata(name: str) -> StepMetadata:
    category = name.split(".", 1)[0] if "." in name else "custom"
    return StepMetadata(name=name, category=category, risk="custom", plugin=True)


def _clear_steps_for_tests() -> None:
    _STEP_FACTORIES.clear()
    _STEP_METADATA.clear()
