import re
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

import typer

from .context import PipelineContext
from .step import Step


@dataclass
class SequentialStepGroup:
    steps: Sequence[Step]
    step_id: str = "sequence"

    @property
    def name(self) -> str:
        return "sequence"

    def run(self, ctx: PipelineContext) -> None:
        resume_from = ctx.resume_from if _is_descendant(ctx.resume_from, self.step_id) else None
        matched_resume_step = resume_from is None or resume_from == self.step_id
        for step in self.steps:
            step_id = _step_id(step)
            if not matched_resume_step:
                if step_id != resume_from:
                    continue
                matched_resume_step = True
            if ctx.should_skip_step(step_id):
                continue
            ctx.mark_parallel_step_started(step_id)
            try:
                step.run(ctx)
            except Exception as exc:
                ctx.mark_parallel_step_failed(step_id, str(exc))
                raise
            else:
                ctx.mark_parallel_step_completed(step_id)
        if not matched_resume_step:
            raise typer.BadParameter(f"Unknown resume step: {resume_from}")


@dataclass
class ParallelStepGroup:
    steps: Sequence[Step]
    step_id: str = "parallel"

    @property
    def name(self) -> str:
        return "parallel"

    def run(self, ctx: PipelineContext) -> None:
        failures: list[tuple[str, Exception]] = []
        selected_child = _selected_parallel_child(ctx.resume_from, self.step_id)
        runnable_steps = [step for step in self.steps if selected_child is None or _step_id(step) == selected_child]
        with ThreadPoolExecutor(max_workers=len(runnable_steps)) as pool:
            futures = {pool.submit(_run_parallel_child, step, ctx): step for step in runnable_steps}
            for future in as_completed(futures):
                step = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    failures.append((_step_label(step), exc))

        if failures:
            names = ", ".join(name for name, _ in failures)
            error = typer.BadParameter(f"Parallel group failed after all steps finished: {names}")
            error.failed_step_id = _deepest_failed_step_id(ctx, self.step_id)  # type: ignore[attr-defined]
            raise error from failures[0][1]


class PipelineExecutor:
    def run(self, steps: Sequence[Step], ctx: PipelineContext, *, resume_from: str | None = None) -> None:
        ctx.mark_status_started()
        skipping_until = resume_from
        try:
            for step in steps:
                before_artifacts = set(ctx.artifacts)
                step_name = getattr(step, "name", step.__class__.__name__)
                step_id = getattr(step, "step_id", None) or step_name
                step_label = _step_label(step)
                resume_root = skipping_until.split("/", 1)[0] if skipping_until is not None else None
                if resume_root is not None and step_id != resume_root:
                    continue
                if skipping_until is not None:
                    ctx.resume_from = skipping_until
                    skipping_until = None
                if ctx.should_skip_step(step_id):
                    ctx.resume_from = None
                    continue
                ctx.mark_step_started(step_id)
                try:
                    step.run(ctx)
                    ctx.mark_step_completed(step_id)
                    ctx.resume_from = None
                except typer.BadParameter as exc:
                    produced = sorted(set(ctx.artifacts) - before_artifacts)
                    command = _step_command(step)
                    artifacts = ", ".join(produced) or "none"
                    exit_code = _exit_code(str(exc))
                    summary = (
                        f"Failed step: {step_label}; command: {command}; "
                        f"exit code: {exit_code}; artifacts produced: {artifacts}"
                    )
                    error = f"{exc}. {summary}"
                    failed_step_id = str(getattr(exc, "failed_step_id", step_id))
                    ctx.mark_status_failed(failed_step_id, error)
                    raise typer.BadParameter(error) from exc
                except Exception as exc:
                    ctx.mark_status_failed(step_id, str(exc))
                    raise
        except Exception:
            raise
        else:
            if skipping_until is not None:
                raise typer.BadParameter(f"Unknown resume step: {resume_from}")
            ctx.mark_status_success()


def _run_parallel_child(step: Step, ctx: PipelineContext) -> None:
    step_id = _step_id(step)
    if ctx.should_skip_step(step_id):
        return
    ctx.mark_parallel_step_started(step_id)
    try:
        step.run(ctx)
    except Exception as exc:
        ctx.mark_parallel_step_failed(step_id, str(exc))
        raise
    else:
        ctx.mark_parallel_step_completed(step_id)


def _step_id(step: Any) -> str:
    step_name = getattr(step, "name", step.__class__.__name__)
    return str(getattr(step, "step_id", None) or step_name)


def _is_descendant(selector: str | None, parent_id: str) -> bool:
    return selector is not None and selector.startswith(parent_id + "/")


def _selected_parallel_child(selector: str | None, group_id: str) -> str | None:
    if not _is_descendant(selector, group_id):
        return None
    remainder = selector[len(group_id) + 1 :]
    child_index = remainder.split("/", 1)[0]
    return f"{group_id}/{child_index}"


def _deepest_failed_step_id(ctx: PipelineContext, group_id: str) -> str:
    failed_ids = [failure.split(":", 1)[0] for failure in ctx.parallel_failed if failure.startswith(group_id + "/")]
    return max(failed_ids, key=lambda step_id: step_id.count("/"), default=group_id)


def _exit_code(message: str) -> str:
    match = re.search(r"exit code\s+(-?\d+)", message, flags=re.IGNORECASE)
    return match.group(1) if match else "unknown"


def _step_label(step: Any) -> str:
    step_name = getattr(step, "name", step.__class__.__name__)
    step_id = getattr(step, "step_id", None) or step_name
    return step_name if step_id == step_name else f"{step_id} {step_name}"


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
