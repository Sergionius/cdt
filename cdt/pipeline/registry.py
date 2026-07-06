from collections.abc import Callable
from dataclasses import asdict, dataclass
from difflib import get_close_matches

import typer

from .step import Step

StepFactory = Callable[..., Step]


@dataclass(frozen=True)
class StepMetadata:
    name: str
    description: str = ""
    category: str = "custom"
    risk: str = "custom"
    requires_artifacts: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()
    external_tools: tuple[str, ...] = ()
    plugin: bool = False

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["requires_artifacts"] = list(self.requires_artifacts)
        payload["produces"] = list(self.produces)
        payload["external_tools"] = list(self.external_tools)
        return payload


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
        requires_artifacts=tuple(metadata.requires_artifacts),
        produces=tuple(metadata.produces),
        external_tools=tuple(metadata.external_tools),
        plugin=metadata.plugin,
    )


def _default_metadata(name: str) -> StepMetadata:
    category = name.split(".", 1)[0] if "." in name else "custom"
    return StepMetadata(name=name, category=category, risk="custom", plugin=True)


def _clear_steps_for_tests() -> None:
    _STEP_FACTORIES.clear()
    _STEP_METADATA.clear()
