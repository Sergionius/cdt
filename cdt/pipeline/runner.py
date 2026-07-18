import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import typer

from ..artifacts import BuildArtifact
from ..runner import CommandRunner
from .builtins import register_builtin_steps
from .config import configured_steps, load_pipeline_config, load_plugins
from .context import PipelineContext
from .executor import PipelineExecutor
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
) -> None:
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
    steps = configured_steps(pipeline)
    resume_step_id = _resolve_resume_from(steps, resume_from) if resume_from is not None else None
    ctx = PipelineContext(
        cwd=cwd,
        env=env,
        runner=runner or CommandRunner(),
        ids=ids or [],
        pipeline_name=name,
        status_file=status_file,
        skip_completed=skip_completed,
    )
    if resume_from or skip_completed:
        _restore_resume_status(ctx, resume_status_file)
    PipelineExecutor().run(steps, ctx, resume_from=resume_step_id)


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
