from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from .pipeline.context import PipelineContext
from .pipeline.registry import ResultProduction, ResultRequirement, StepMetadata, register_step

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
    requires: list[ResultRequirement] | tuple[ResultRequirement, ...] | None = None,
    produces: list[ResultProduction] | tuple[ResultProduction, ...] | None = None,
    **metadata_kwargs: Any,
) -> Callable[[T], T]:
    def decorator(target: T) -> T:
        step_metadata = _build_metadata(name, metadata, requires, produces, metadata_kwargs)
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
    requires: list[ResultRequirement] | tuple[ResultRequirement, ...] | None,
    produces: list[ResultProduction] | tuple[ResultProduction, ...] | None,
    metadata_kwargs: dict[str, Any],
) -> StepMetadata:
    if metadata is not None:
        if requires is not None or produces is not None:
            raise TypeError("Cannot pass both 'metadata' and 'requires'/'produces' to @step")
        return StepMetadata(
            name=name,
            description=metadata.description,
            category=metadata.category,
            risk=metadata.risk,
            requires=tuple(metadata.requires),
            produces=tuple(metadata.produces),
            external_tools=tuple(metadata.external_tools),
            plugin=True,
        )

    kwargs = dict(metadata_kwargs)
    if "category" not in kwargs:
        kwargs["category"] = name.split(".", 1)[0] if "." in name else "custom"
    if "risk" not in kwargs:
        kwargs["risk"] = "custom"
    if requires is not None:
        kwargs["requires"] = tuple(requires)
    if produces is not None:
        kwargs["produces"] = tuple(produces)
    return StepMetadata(name=name, plugin=True, **kwargs)


__all__ = ["PipelineContext", "ResultProduction", "ResultRequirement", "StepMetadata", "step"]
