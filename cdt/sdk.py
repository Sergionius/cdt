from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from .pipeline.context import PipelineContext
from .pipeline.registry import register_step

T = TypeVar("T")


@dataclass
class FunctionStep:
    name: str
    fn: Callable[..., None]
    options: dict[str, Any]

    def run(self, ctx: PipelineContext) -> None:
        self.fn(ctx, **self.options)


def step(name: str) -> Callable[[T], T]:
    def decorator(target: T) -> T:
        if isinstance(target, type):
            register_step(name, lambda **options: target(**options))
            return target

        if callable(target):
            register_step(name, lambda **options: FunctionStep(name, target, options))
            return target

        raise TypeError("@step can decorate only functions or classes")

    return decorator


__all__ = ["PipelineContext", "step"]
