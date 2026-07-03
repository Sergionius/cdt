from collections.abc import Callable

import typer

from .step import Step

StepFactory = Callable[..., Step]

_STEP_FACTORIES: dict[str, StepFactory] = {}


def register_step(name: str, factory: StepFactory) -> StepFactory:
    step_name = name.strip()
    if not step_name:
        raise typer.BadParameter("Pipeline step name cannot be empty")
    if step_name in _STEP_FACTORIES:
        raise typer.BadParameter(f"Pipeline step is already registered: {step_name}")
    _STEP_FACTORIES[step_name] = factory
    return factory


def get_step_factory(name: str) -> StepFactory:
    try:
        return _STEP_FACTORIES[name]
    except KeyError as exc:
        available = ", ".join(list_steps()) or "none"
        raise typer.BadParameter(f"Unknown pipeline step: {name}. Available steps: {available}") from exc


def list_steps() -> list[str]:
    return sorted(_STEP_FACTORIES)


def _clear_steps_for_tests() -> None:
    _STEP_FACTORIES.clear()
