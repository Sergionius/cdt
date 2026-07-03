from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

import typer

from .context import PipelineContext
from .step import Step


@dataclass
class ParallelStepGroup:
    steps: Sequence[Step]

    @property
    def name(self) -> str:
        return "parallel"

    def run(self, ctx: PipelineContext) -> None:
        failures: list[tuple[str, Exception]] = []
        with ThreadPoolExecutor(max_workers=len(self.steps)) as pool:
            futures = {pool.submit(step.run, ctx): step for step in self.steps}
            for future in as_completed(futures):
                step = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    failures.append((getattr(step, "name", step.__class__.__name__), exc))

        if failures:
            names = ", ".join(name for name, _ in failures)
            raise typer.BadParameter(f"Parallel group failed after all steps finished: {names}") from failures[0][1]


class PipelineExecutor:
    def run(self, steps: Sequence[Step], ctx: PipelineContext) -> None:
        for step in steps:
            step.run(ctx)
