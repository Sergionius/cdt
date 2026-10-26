"""Agent release start/status/stop commands."""

from pathlib import Path

import typer

# Unambiguous relative import: ``cdt.agent_release`` is the domain module, this
# file is the CLI command module ``cdt.cli_commands.agent_release``.
from ..agent_release import (
    format_yamlish,
    parse_duration,
    release_status,
    start_release,
    stop_release,
    wait_for_release,
)
from ..pipeline.config import load_pipeline_config, parse_pipeline_inputs, validate_pipeline_inputs
from .common import _confirmation_required, _echo_json

agent_release_app = typer.Typer(no_args_is_help=True)


@agent_release_app.command(name="start")
def agent_release_start(
    pipeline: str = typer.Argument(..., help="Pipeline name from cdt.yaml"),
    id: list[str] = typer.Option([], "--id", help="Repeatable: --id A --id B"),
    input: list[str] = typer.Option([], "--input", help="Repeatable pipeline input: --input KEY=VALUE"),
    confirm: str | None = typer.Option(None, "--confirm", help="Exact pipeline name required for production"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
):
    """Start a pipeline in the background with agent-friendly log/status files."""
    try:
        config = load_pipeline_config(Path.cwd())
    except typer.BadParameter as exc:
        if not json_output:
            raise
        _echo_json(
            {
                "schema_version": 1,
                "status": "error",
                "pipeline": pipeline,
                "error": str(exc),
            }
        )
        raise typer.Exit(code=1) from exc
    if pipeline not in config.pipelines:
        available = ", ".join(sorted(config.pipelines)) or "none"
        message = f"Unknown pipeline: {pipeline}. Available pipelines: {available}"
        if not json_output:
            raise typer.BadParameter(message)
        _echo_json({"schema_version": 1, "status": "error", "pipeline": pipeline, "error": message})
        raise typer.Exit(code=1)
    try:
        inputs = parse_pipeline_inputs(input)
        validate_pipeline_inputs(config.pipelines[pipeline], inputs)
    except typer.BadParameter as exc:
        if not json_output:
            raise
        _echo_json({"schema_version": 1, "status": "error", "pipeline": pipeline, "error": str(exc)})
        raise typer.Exit(code=1) from exc
    if _confirmation_required(config, pipeline, confirm):
        payload = {
            "schema_version": 1,
            "status": "confirmation_required",
            "pipeline": pipeline,
            "required_confirmation": pipeline,
        }
        _echo_json(payload) if json_output else typer.echo(format_yamlish(payload))
        raise typer.Exit(code=2)
    payload = start_release(pipeline, ids=id, confirm=confirm, inputs=inputs)
    _echo_json(payload) if json_output else typer.echo(format_yamlish(payload))
    if payload["status"] == "failed":
        raise typer.Exit(code=1)


@agent_release_app.command(name="status")
def agent_release_status(
    pipeline: str | None = typer.Argument(None, help="Pipeline name; resolves its latest run"),
    run_id: str | None = typer.Option(None, "--run", help="Exact run id"),
    wait: bool = typer.Option(False, "--wait", help="Wait until the release finishes"),
    timeout: str = typer.Option("40m", "--timeout", help="Wait timeout, e.g. 30s, 40m, 1h"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
):
    """Print a compact release status without reading the build log."""
    if pipeline is None and run_id is None:
        raise typer.BadParameter("Provide a pipeline name or --run <run-id>")
    payload = (
        wait_for_release(pipeline, parse_duration(timeout), run_id=run_id)
        if wait
        else release_status(pipeline, run_id=run_id)
    )
    _echo_json(payload) if json_output else typer.echo(format_yamlish(payload))


@agent_release_app.command(name="stop")
def agent_release_stop(
    pipeline: str | None = typer.Argument(None, help="Pipeline name; resolves its latest run"),
    run_id: str | None = typer.Option(None, "--run", help="Exact run id"),
    timeout: str = typer.Option("30s", "--timeout", help="Graceful stop timeout, e.g. 30s, 2m"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
):
    """Stop a background agent release."""
    if pipeline is None and run_id is None:
        raise typer.BadParameter("Provide a pipeline name or --run <run-id>")
    payload = stop_release(pipeline, timeout_seconds=parse_duration(timeout), run_id=run_id)
    _echo_json(payload) if json_output else typer.echo(format_yamlish(payload))
