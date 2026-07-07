import json
from pathlib import Path

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
    step_names = {getattr(step, "name", step.__class__.__name__) for step in steps}
    if resume_from is not None and resume_from not in step_names:
        raise typer.BadParameter(f"Unknown resume step: {resume_from}")
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
        _restore_resume_status(ctx, resume_status_file or status_file)
    PipelineExecutor().run(steps, ctx, resume_from=resume_from)


def _restore_resume_status(ctx: PipelineContext, status_file: Path | None) -> None:
    if status_file is None:
        raise typer.BadParameter("Resume requires --status-file or --resume-status-file")
    if not status_file.exists():
        raise typer.BadParameter(f"Resume status file not found: {status_file}")
    try:
        payload = json.loads(status_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"Invalid resume status JSON: {status_file}") from exc
    if not isinstance(payload, dict):
        raise typer.BadParameter(f"Invalid resume status JSON: {status_file}")

    ctx.completed_steps = [step for step in payload.get("completed_steps", []) if isinstance(step, str)]
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
