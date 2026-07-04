from collections.abc import Callable
from difflib import get_close_matches

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
        steps = list_steps()
        available = ", ".join(steps) or "none"
        matches = get_close_matches(name, steps, n=3)
        hint = f" Did you mean: {', '.join(matches)}?" if matches else ""
        raise typer.BadParameter(f"Unknown pipeline step: {name}.{hint} Available steps: {available}") from exc


def list_steps() -> list[str]:
    return sorted(_STEP_FACTORIES)


def _clear_steps_for_tests() -> None:
    _STEP_FACTORIES.clear()
