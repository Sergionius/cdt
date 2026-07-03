from typing import Protocol

from .context import PipelineContext


class Step(Protocol):
    name: str

    def run(self, ctx: PipelineContext) -> None:
        ...
