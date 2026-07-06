from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from .pipeline.context import PipelineContext
from .pipeline.registry import StepMetadata, register_step

T = TypeVar("T")


@dataclass
class FunctionStep:
    name: str
    fn: Callable[..., None]
    options: dict[str, Any]

    def run(self, ctx: PipelineContext) -> None:
        self.fn(ctx, **self.options)


def step(
    name: str,
    *,
    metadata: StepMetadata | None = None,
    **metadata_kwargs: Any,
) -> Callable[[T], T]:
    def decorator(target: T) -> T:
        step_metadata = _build_metadata(name, metadata, metadata_kwargs)
        if isinstance(target, type):
            register_step(name, lambda **options: target(**options), metadata=step_metadata)
            return target

        if callable(target):
            register_step(name, lambda **options: FunctionStep(name, target, options), metadata=step_metadata)
            return target

        raise TypeError("@step can decorate only functions or classes")

    return decorator


def _build_metadata(
    name: str,
    metadata: StepMetadata | None,
    metadata_kwargs: dict[str, Any],
) -> StepMetadata:
    if metadata is not None:
        return StepMetadata(
            name=name,
            description=metadata.description,
            category=metadata.category,
            risk=metadata.risk,
            requires_artifacts=tuple(metadata.requires_artifacts),
            produces=tuple(metadata.produces),
            external_tools=tuple(metadata.external_tools),
            plugin=True,
        )

    kwargs = dict(metadata_kwargs)
    if "category" not in kwargs:
        kwargs["category"] = name.split(".", 1)[0] if "." in name else "custom"
    if "risk" not in kwargs:
        kwargs["risk"] = "custom"
    return StepMetadata(name=name, plugin=True, **kwargs)


__all__ = ["PipelineContext", "StepMetadata", "step"]
