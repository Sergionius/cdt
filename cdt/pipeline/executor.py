import re
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

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
        ctx.mark_status_started()
        try:
            for step in steps:
                before_artifacts = set(ctx.artifacts)
                step_name = getattr(step, "name", step.__class__.__name__)
                ctx.mark_step_started(step_name)
                try:
                    step.run(ctx)
                    ctx.mark_step_completed(step_name)
                except typer.BadParameter as exc:
                    produced = sorted(set(ctx.artifacts) - before_artifacts)
                    command = _step_command(step)
                    artifacts = ", ".join(produced) or "none"
                    exit_code = _exit_code(str(exc))
                    summary = (
                        f"Failed step: {step_name}; command: {command}; "
                        f"exit code: {exit_code}; artifacts produced: {artifacts}"
                    )
                    error = f"{exc}. {summary}"
                    ctx.mark_status_failed(step_name, error)
                    raise typer.BadParameter(error) from exc
                except Exception as exc:
                    ctx.mark_status_failed(step_name, str(exc))
                    raise
        except Exception:
            raise
        else:
            ctx.mark_status_success()


def _exit_code(message: str) -> str:
    match = re.search(r"exit code\s+(-?\d+)", message, flags=re.IGNORECASE)
    return match.group(1) if match else "unknown"


def _step_command(step: Any) -> str:
    options = getattr(step, "options", None)
    if isinstance(options, dict):
        for key in ("command", "script"):
            value = options.get(key)
            if isinstance(value, str):
                return value
    for key in ("command", "script"):
        value = getattr(step, key, None)
        if isinstance(value, str):
            return value
    return "unknown"
