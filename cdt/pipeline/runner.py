import json
import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import typer

from ..artifacts import BuildArtifact
from ..redaction import SecretRedactor
from ..runner import CommandRunner
from ..runs import RunOutputRecorder, ensure_run, write_exit_code, write_text_atomic
from .builtins import register_builtin_steps
from .config import configured_steps, load_pipeline_config, load_plugins, validate_pipeline_inputs
from .context import PipelineContext
from .executor import PipelineExecutionError, PipelineExecutor
from .validation import validate_pipeline


def run_configured_pipeline(
    cwd: Path,
    env: dict[str, str],
    name: str,
    ids: list[str] | None = None,
    runner: CommandRunner | None = None,
    status_file: Path | None = None,
    resume_from: str | None = None,
    skip_completed: bool = False,
    resume_status_file: Path | None = None,
    run_id: str | None = None,
    detached: bool = False,
    record_run: bool = True,
    inputs: dict[str, str] | None = None,
) -> str | None:
    config = load_pipeline_config(cwd)
    register_builtin_steps()
    load_plugins(config.plugins)
    try:
        pipeline = config.pipelines[name]
    except KeyError as exc:
        available = ", ".join(sorted(config.pipelines)) or "none"
        raise typer.BadParameter(f"Unknown pipeline: {name}. Available pipelines: {available}") from exc
    errors = validate_pipeline(config, name)
    if errors:
        raise typer.BadParameter("Invalid pipeline config: " + "; ".join(error["message"] for error in errors))
    inputs = dict(inputs or {})
    validate_pipeline_inputs(pipeline, inputs)
    steps = configured_steps(pipeline)
    resume_step_id = _resolve_resume_from(steps, resume_from) if resume_from is not None else None
    run_paths = None
    if record_run:
        command = ["cdt", "run", name]
        for input_name, input_value in inputs.items():
            command.extend(["--input", f"{input_name}={input_value}"])
        for task_id in ids or []:
            command.extend(["--id", task_id])
        run_paths = ensure_run(
            cwd,
            name,
            ids=ids,
            run_id=run_id,
            command=command,
            detached=detached,
            inputs=inputs,
            env=env,
        )
    if run_paths is not None and not detached:
        write_text_atomic(run_paths.pid, f"{os.getpid()}\n")
    # Direct runs tee CDT-owned output into the run log. Detached workers already
    # capture the combined subprocess stream, so no recorder is installed there.
    recorder: RunOutputRecorder | None = None
    if run_paths is not None and not detached:
        recorder = RunOutputRecorder(run_paths.log, SecretRedactor.from_env(env))
        recorder.install()
    primary_status = run_paths.status if run_paths is not None else status_file
    mirror_status = status_file if run_paths is not None and status_file != primary_status else None
    ctx = PipelineContext(
        cwd=cwd,
        env=env,
        runner=runner or CommandRunner(),
        ids=ids or [],
        pipeline_name=name,
        inputs=inputs,
        status_file=primary_status,
        mirror_status_file=mirror_status,
        run_dir=run_paths.root if run_paths is not None else None,
        run_id=run_paths.run_id if run_paths is not None else run_id,
        skip_completed=skip_completed,
    )
    try:
        if resume_from or skip_completed:
            _restore_resume_status(ctx, resume_status_file)
        try:
            PipelineExecutor().run(steps, ctx, resume_from=resume_step_id)
        except BaseException as exc:
            if run_paths is not None:
                write_exit_code(run_paths.exit, 1)
            if recorder is not None:
                recorder.record_line(_terminal_failure_summary(exc))
            _rollback_release_files(ctx)
            raise
        else:
            if run_paths is not None:
                write_exit_code(run_paths.exit, 0)
    finally:
        if recorder is not None:
            recorder.close()
    return run_paths.run_id if run_paths is not None else None


def _rollback_release_files(ctx: PipelineContext) -> None:
    """Restore snapshotted release files when a run fails before the release commit."""
    if not ctx.rollback_pending:
        return
    restored = ctx.perform_rollback()
    typer.echo(
        f"==> Rolled back {len(restored)} release file(s) to their pre-release state before the release commit",
        err=True,
    )
    ctx.write_status("failed")


def _terminal_failure_summary(exc: BaseException) -> str:
    """Readable terminal summary recorded into the run log before re-raising."""
    if isinstance(exc, PipelineExecutionError):
        return exc.message
    detail = str(exc).strip()
    if not detail:
        return f"CDT run failed: {type(exc).__name__}"
    return f"CDT run failed: {type(exc).__name__}: {detail}"


def _resolve_resume_from(steps: Sequence[Any], selector: str) -> str:
    all_steps = list(_flatten_steps(steps))
    by_id = {_step_id(step): step for step in all_steps}
    if selector in by_id:
        return selector

    if _is_step_id(selector):
        raise typer.BadParameter(f"Unknown resume step id: {selector}")

    if "@" in selector:
        name, step_id = selector.rsplit("@", 1)
        try:
            step = by_id[step_id]
        except KeyError as exc:
            raise typer.BadParameter(f"Unknown resume step id: {step_id}") from exc
        actual_name = _step_name(step)
        if actual_name != name:
            raise typer.BadParameter(f"Resume selector {selector} does not match step {step_id} {actual_name}.")
        return step_id

    matches = [_step_id(step) for step in all_steps if _step_name(step) == selector]
    if not matches:
        raise typer.BadParameter(f"Unknown resume step: {selector}")
    if len(matches) > 1:
        joined = ", ".join(matches)
        qualified = ", ".join(f"{selector}@{step_id}" for step_id in matches)
        raise typer.BadParameter(
            f"Ambiguous resume step: {selector} matches step ids {joined}. Use {joined}, {qualified}."
        )
    return matches[0]


def _flatten_steps(steps: Sequence[Any]):
    for step in steps:
        yield step
        children = getattr(step, "steps", None)
        if isinstance(children, Sequence):
            yield from _flatten_steps(children)


def _step_id(step: Any) -> str:
    return str(getattr(step, "step_id", None) or _step_name(step))


def _step_name(step: Any) -> str:
    return str(getattr(step, "name", step.__class__.__name__))


def _restore_resume_status(ctx: PipelineContext, status_file: Path | None) -> None:
    if status_file is None:
        raise typer.BadParameter("Resume requires --resume-status-file. --status-file only writes the new run status.")
    if not status_file.exists():
        raise typer.BadParameter(f"Resume status file not found: {status_file}")
    try:
        payload = json.loads(status_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"Invalid resume status JSON: {status_file}") from exc
    if not isinstance(payload, dict):
        raise typer.BadParameter(f"Invalid resume status JSON: {status_file}")

    raw_completed_steps = payload.get("completed_steps", [])
    if not isinstance(raw_completed_steps, list):
        raise typer.BadParameter("Resume status completed_steps must be a list")
    completed_steps = [step for step in raw_completed_steps if isinstance(step, str)]
    _validate_resume_step_ids(completed_steps)
    ctx.completed_steps = completed_steps
    raw_inputs = payload.get("inputs")
    saved_inputs = _validate_resume_inputs(raw_inputs)
    if saved_inputs != ctx.inputs:
        raise typer.BadParameter(
            "Resume inputs do not match the original run: "
            f"original inputs: {saved_inputs or {}}, current inputs: {ctx.inputs or {}}. "
            "A release cannot be continued with different inputs."
        )
    ctx.old_version = payload.get("old_version") if isinstance(payload.get("old_version"), str) else None
    ctx.new_version = payload.get("new_version") if isinstance(payload.get("new_version"), str) else None
    artifacts = payload.get("artifacts", [])
    if not isinstance(artifacts, list):
        raise typer.BadParameter("Resume status artifacts must be a list")
    for artifact_payload in artifacts:
        if not isinstance(artifact_payload, dict) or not isinstance(artifact_payload.get("name"), str):
            continue
        artifact = BuildArtifact.from_json(artifact_payload)
        if not artifact.path.exists():
            raise typer.BadParameter(f"Resume artifact does not exist: {artifact.path}")
        ctx.artifacts[artifact_payload["name"]] = artifact


def _validate_resume_inputs(raw_inputs: Any) -> dict[str, str]:
    if raw_inputs is None:
        return {}
    if not isinstance(raw_inputs, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in raw_inputs.items()
    ):
        raise typer.BadParameter("Resume status inputs must be a mapping of strings")
    return dict(raw_inputs)


def _validate_resume_step_ids(step_ids: list[str]) -> None:
    if all(_is_step_id(step_id) for step_id in step_ids):
        return
    raise typer.BadParameter(
        "Resume status file uses step names from an older CDT version. "
        "Current CDT requires step ids because duplicate names are ambiguous. "
        "Rerun without --skip-completed or recreate the status file."
    )


def _is_step_id(value: str) -> bool:
    return re.fullmatch(r"\d+(?:/\d+)*", value) is not None
