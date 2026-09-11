import shlex
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

import typer

from ..runner import CommandExecutionError
from .context import PipelineContext
from .step import Step

_PARALLEL_COMPLETION_NOTICE = "Other parallel steps were allowed to finish."


class PipelineExecutionError(typer.BadParameter):
    """Runtime pipeline failure carrying a readable, assembled summary.

    It subclasses ``typer.BadParameter`` so existing callers that handle step
    failures keep working, while callers can catch this class to render a
    runtime failure without usage help instead of invalid-input diagnostics.
    """

    def __init__(self, message: str, *, failed_step_id: str, failed_step_label: str) -> None:
        super().__init__(message)
        self.message = message
        self.failed_step_id = failed_step_id
        self.failed_step_label = failed_step_label


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
class ChildFailure:
    """Metadata of a single failed step (parallel child or sequential step)."""

    step_id: str
    step_name: str
    exception: Exception
    command: str | None
    command_args: list[str] | None = None
    exit_code: int | None = None

    @property
    def label(self) -> str:
        if self.step_id == self.step_name:
            return self.step_name
        return f"{self.step_id} ({self.step_name})"


@dataclass
class ParallelStepGroup:
    steps: Sequence[Step]
    step_id: str = "parallel"

    @property
    def name(self) -> str:
        return "parallel"

    def run(self, ctx: PipelineContext) -> None:
        selected_child = _selected_parallel_child(ctx.resume_from, self.step_id)
        runnable_steps = [step for step in self.steps if selected_child is None or _step_id(step) == selected_child]
        failures: dict[int, ChildFailure] = {}
        with ThreadPoolExecutor(max_workers=len(runnable_steps)) as pool:
            futures = {pool.submit(_run_parallel_child, step, ctx): step for step in runnable_steps}
            for future in as_completed(futures):
                step = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    failures[id(step)] = _describe_child_failure(step, ctx, exc)

        ordered_failures = [failures[id(step)] for step in runnable_steps if id(step) in failures]
        if ordered_failures:
            primary = ordered_failures[0]
            lines = [f"Pipeline failed at step {primary.label}."]
            lines.extend(_format_child_failure(primary))
            for failure in ordered_failures[1:]:
                lines.append(f"{failure.label}:")
                lines.extend(_format_child_failure(failure))
            lines.append(_PARALLEL_COMPLETION_NOTICE)
            raise PipelineExecutionError(
                "\n".join(lines),
                failed_step_id=primary.step_id,
                failed_step_label=primary.label,
            ) from primary.exception


class PipelineExecutor:
    def run(self, steps: Sequence[Step], ctx: PipelineContext, *, resume_from: str | None = None) -> None:
        ctx.mark_status_started()
        skipping_until = resume_from
        try:
            for step in steps:
                before_artifacts = set(ctx.artifacts)
                step_name = getattr(step, "name", step.__class__.__name__)
                step_id = getattr(step, "step_id", None) or step_name
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
                except PipelineExecutionError as exc:
                    error = ctx.redact(_append_artifacts(exc.message, ctx, before_artifacts))
                    ctx.mark_status_failed(exc.failed_step_id, error)
                    raise PipelineExecutionError(
                        error, failed_step_id=exc.failed_step_id, failed_step_label=exc.failed_step_label
                    ) from exc
                except (typer.BadParameter, CommandExecutionError) as exc:
                    failure = _describe_step_failure(step, exc)
                    lines = [f"Pipeline failed at step {failure.label}."]
                    lines.extend(_format_child_failure(failure))
                    error = ctx.redact(_append_artifacts("\n".join(lines), ctx, before_artifacts))
                    ctx.mark_status_failed(step_id, error)
                    raise PipelineExecutionError(
                        error, failed_step_id=step_id, failed_step_label=failure.label
                    ) from exc
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


def _describe_child_failure(step: Any, ctx: PipelineContext, exc: Exception) -> ChildFailure:
    """Collect id, name, original exception and command metadata of a failed parallel child."""
    step_id = _step_id(step)
    failed_id = _deepest_failed_step_id(ctx, step_id)
    target: Any = step if failed_id == step_id else (_find_step_by_id(step, failed_id) or step)
    failure = ChildFailure(
        step_id=_step_id(target),
        step_name=_step_name(target),
        exception=exc,
        command=_step_command(target),
    )
    _apply_command_execution_metadata(failure, exc)
    return failure


def _describe_step_failure(step: Any, exc: Exception) -> ChildFailure:
    """Collect failure metadata of a failed sequential step for the executor summary."""
    failure = ChildFailure(
        step_id=_step_id(step),
        step_name=_step_name(step),
        exception=exc,
        command=_step_command(step),
    )
    _apply_command_execution_metadata(failure, exc)
    return failure


def _apply_command_execution_metadata(failure: ChildFailure, exc: Exception) -> None:
    """Prefer structured command and exit-code metadata over configured options."""
    if isinstance(exc, CommandExecutionError):
        failure.command_args = list(exc.command)
        failure.exit_code = exc.exit_code


def _child_cause(exc: Exception) -> str:
    """Readable cause, with fallbacks for exceptions without a useful message."""
    if isinstance(exc, typer.Exit):
        return f"Step exited with code {exc.exit_code}."
    text = str(exc).strip()
    return text if text else type(exc).__name__


def _format_child_failure(failure: ChildFailure) -> list[str]:
    """Body lines of one failed-step block: cause plus available command details."""
    lines = [_child_cause(failure.exception)]
    command = shlex.join(failure.command_args) if failure.command_args else failure.command
    if command is not None:
        lines.append(f"Command: {command}")
    if failure.exit_code is not None:
        lines.append(f"Exit code: {failure.exit_code}")
    return lines


def _append_artifacts(message: str, ctx: PipelineContext, before_artifacts: set[str]) -> str:
    produced = sorted(set(ctx.artifacts) - before_artifacts)
    return f"{message}\nArtifacts produced: {', '.join(produced) or 'none'}"


def _find_step_by_id(step: Any, target_id: str) -> Any:
    if _step_id(step) == target_id:
        return step
    nested = getattr(step, "steps", None)
    if isinstance(nested, (list, tuple)):
        for child in nested:
            found = _find_step_by_id(child, target_id)
            if found is not None:
                return found
    return None


def _step_name(step: Any) -> str:
    return str(getattr(step, "name", step.__class__.__name__))


def _step_id(step: Any) -> str:
    return str(getattr(step, "step_id", None) or _step_name(step))


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


def _step_command(step: Any) -> str | None:
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
    return None
