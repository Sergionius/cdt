import json
from pathlib import Path

import typer

from . import __version__
from .agent_release import format_yamlish, parse_duration, release_status, start_release, stop_release, wait_for_release
from .config import _load_project_env, _set_ui_mode
from .doctor import run_doctor
from .init_project import initialize_project
from .pipeline.builtins import register_builtin_steps
from .pipeline.config import load_pipeline_config, load_plugins
from .pipeline.planning import plan_payload
from .pipeline.preflight import preflight_payload
from .pipeline.registry import list_steps
from .pipeline.runner import run_configured_pipeline
from .pipeline.validation import inspect_payload, step_tree, steps_payload, validate_payload, validate_pipeline
from .runs import list_runs, resolve_run
from .schema import bundled_schema_path
from .self_update import SelfUpdateError, run_self_update

app = typer.Typer(no_args_is_help=True)
pipeline_app = typer.Typer(no_args_is_help=True)
agent_release_app = typer.Typer(no_args_is_help=True)

_DEFAULT_REPO_URL = "https://github.com/Sergionius/cdt"


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"cdt {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
):
    """CDT CLI entry point."""
    return


@app.command(name="self-update")
def self_update(
    dry_run: bool = typer.Option(False, "--dry-run", help="Show latest release and update command without executing"),
    check: bool = typer.Option(False, "--check", help="Check for updates without changing anything"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    manager: str | None = typer.Option(None, "--manager", help="Install manager to use: pipx, pip, or uv"),
):
    """Update CDT to the latest release from GitHub."""
    if manager is not None and manager not in {"pipx", "pip", "uv"}:
        typer.echo("Error: --manager must be one of: pipx, pip, uv", err=True)
        raise typer.Exit(code=1)
    try:
        exit_code = run_self_update(
            repo_url=_DEFAULT_REPO_URL,
            dry_run=dry_run,
            check=check,
            json_output=json_output,
            manager=manager,
        )
    except SelfUpdateError as exc:
        if json_output:
            typer.echo(
                json.dumps(
                    {
                        "current": __version__,
                        "latest": None,
                        "update_available": False,
                        "status": "error",
                        "message": str(exc),
                        "error": str(exc),
                    },
                    sort_keys=True,
                )
            )
        else:
            typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    raise typer.Exit(code=exit_code)


@app.command(name="doctor")
def doctor(json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON")):
    """Check CDT environment health."""
    raise typer.Exit(code=run_doctor(Path.cwd(), json_output=json_output))


@app.command(name="schema")
def print_schema(
    output: Path | None = typer.Option(None, "--output", "-o", help="Write schema to a file"),
):
    """Print the JSON Schema for cdt.yaml."""
    text = bundled_schema_path().read_text(encoding="utf-8")
    if output is None:
        typer.echo(text, nl=False)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    typer.echo(f"Wrote: {output}")


@app.command(name="init")
def init_project(
    force: bool = typer.Option(False, "--force", help="Replace an existing cdt.yaml"),
):
    """Detect a Flutter project and create a reviewable cdt.yaml."""
    target, detection = initialize_project(Path.cwd(), force=force)
    detected_platforms = (("iOS", detection.ios), ("Android", detection.android), ("web", detection.web))
    platforms = [name for name, present in detected_platforms if present]
    typer.echo("Detected: Flutter" + (f" ({', '.join(platforms)})" if platforms else ""))
    if detection.flavor_candidates:
        typer.echo("Flavor candidates: " + ", ".join(detection.flavor_candidates))
    if detection.firebase:
        typer.echo("Firebase configuration detected; upload steps were not added automatically.")
    typer.echo(f"Created: {target}")
    typer.echo("Next: cdt pipeline validate && cdt run test --dry-run")


@app.command(name="history")
def run_history(
    limit: int = typer.Option(20, "--limit", min=1, max=500, help="Maximum runs to show"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
):
    """List recent pipeline runs."""
    runs = list_runs(Path.cwd(), limit=limit)
    if json_output:
        _echo_json({"schema_version": 1, "runs": runs})
        return
    if not runs:
        typer.echo("No recorded runs.")
        return
    for item in runs:
        typer.echo(f"{item['run_id']}  {item['status']:<9}  {item['pipeline']}")


@app.command(name="status")
def run_status(
    run_id: str = typer.Argument(..., help="Run id from cdt history"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
):
    """Show a recorded pipeline run."""
    payload = release_status(run_id=run_id)
    _echo_json(payload) if json_output else typer.echo(format_yamlish(payload))
    if payload["status"] == "unknown":
        raise typer.Exit(code=1)


@app.command(name="logs")
def run_logs(
    run_id: str = typer.Argument(..., help="Run id from cdt history"),
    tail: int = typer.Option(80, "--tail", min=1, max=10000, help="Number of trailing lines"),
):
    """Print the tail of a recorded run log."""
    paths = resolve_run(Path.cwd(), run_id=run_id)
    if paths is None:
        raise typer.BadParameter(f"Unknown run id: {run_id}")
    try:
        lines = paths.log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise typer.BadParameter(f"Cannot read run log: {exc}") from exc
    typer.echo("\n".join(lines[-tail:]))


@app.command(name="run")
def run_pipeline(
    name: str = typer.Argument(..., help="Pipeline name from cdt.yaml"),
    id: list[str] = typer.Option([], "--id", help="Repeatable: --id A --id B"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show pipeline plan without executing steps"),
    status_file: Path | None = typer.Option(None, "--status-file", help="Write machine-readable run status JSON"),
    resume_from: str | None = typer.Option(None, "--resume-from", help="Resume execution from this top-level step"),
    skip_completed: bool = typer.Option(
        False,
        "--skip-completed",
        help="Skip steps already completed in a status file",
    ),
    resume_status_file: Path | None = typer.Option(
        None,
        "--resume-status-file",
        help="Read previous run status JSON for --resume-from or --skip-completed",
    ),
    confirm: str | None = typer.Option(None, "--confirm", help="Exact pipeline name required for production"),
    run_id: str | None = typer.Option(None, "--run-id", help="Use an existing CDT run id", hidden=True),
):
    """Run a pipeline from cdt.yaml."""
    cwd = Path.cwd()
    if dry_run:
        _pipeline_plan(cwd, name, json_output=False)
        return
    config = load_pipeline_config(cwd)
    _confirm_pipeline_risk(config, name, confirm)
    env = _load_project_env(cwd)
    _set_ui_mode(env)
    completed_run_id = run_configured_pipeline(
        cwd,
        env,
        name,
        ids=id,
        status_file=status_file,
        resume_from=resume_from,
        skip_completed=skip_completed,
        resume_status_file=resume_status_file,
        run_id=run_id,
        detached=run_id is not None,
    )
    if completed_run_id is not None and run_id is None:
        typer.echo(f"Run: {completed_run_id}")


@pipeline_app.command(name="list")
def pipeline_list():
    """List pipelines from cdt.yaml."""
    cwd = Path.cwd()
    config = load_pipeline_config(cwd)
    for pipeline_name in sorted(config.pipelines):
        typer.echo(pipeline_name)


@pipeline_app.command(name="inspect")
def pipeline_inspect(
    name: str = typer.Argument(..., help="Pipeline name from cdt.yaml"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
):
    """Show configured steps without running them."""
    cwd = Path.cwd()
    if json_output:
        register_builtin_steps()
        config, errors = _load_config_for_json(cwd)
        if config is not None:
            errors.extend(_load_plugins_for_json(config.plugins))
            errors.extend(validate_pipeline(config, name))
            _echo_json(inspect_payload(config, name, errors=errors))
        else:
            _echo_json(_error_payload(name, errors))
        if errors:
            raise typer.Exit(code=1)
        return

    config = load_pipeline_config(cwd)
    register_builtin_steps()
    load_plugins(config.plugins)
    errors = validate_pipeline(config, name)
    if name not in config.pipelines:
        available = ", ".join(sorted(config.pipelines)) or "none"
        raise typer.BadParameter(f"Unknown pipeline: {name}. Available pipelines: {available}")
    typer.echo(f"Pipeline: {name}")
    typer.echo(f"Declared risk: {config.pipelines[name].risk}")
    typer.echo("Steps:")
    _echo_step_tree(step_tree(config.pipelines[name].steps))
    typer.echo("")
    typer.echo("Registered steps:")
    for step_name in list_steps():
        typer.echo(f"  {step_name}")
    if errors:
        typer.echo("")
        typer.echo("Errors:")
        for error in errors:
            typer.echo(f"  {error.get('path', '')}: {error['message']}")
        raise typer.Exit(code=1)


@pipeline_app.command(name="plan")
def pipeline_plan(
    name: str = typer.Argument(..., help="Pipeline name from cdt.yaml"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
):
    """Show the static execution plan without running steps."""
    _pipeline_plan(Path.cwd(), name, json_output=json_output)


@pipeline_app.command(name="validate")
def pipeline_validate(
    name: str | None = typer.Argument(None, help="Optional pipeline name from cdt.yaml"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    strict: bool = typer.Option(False, "--strict", help="Fail when pipeline plan emits warnings"),
):
    """Validate cdt.yaml schema and registered step names without running steps."""
    cwd = Path.cwd()
    if json_output:
        config, errors = _load_config_for_json(cwd)
        warnings: list[dict[str, str]] = []
        if config is not None:
            register_builtin_steps()
            errors.extend(_load_plugins_for_json(config.plugins))
            errors.extend(validate_pipeline(config, name))
            if strict and not errors:
                warnings = _strict_warnings(config, name)
                errors.extend(_strict_errors(warnings))
        payload = validate_payload(config, name, errors=errors) if config is not None else _error_payload(name, errors)
        if warnings:
            payload["warnings"] = warnings
        _echo_json(payload)
        if errors:
            raise typer.Exit(code=1)
        return

    config = load_pipeline_config(cwd)
    register_builtin_steps()
    load_plugins(config.plugins)
    errors = validate_pipeline(config, name)
    warnings: list[dict[str, str]] = []
    if strict and not errors:
        warnings = _strict_warnings(config, name)
        errors.extend(_strict_errors(warnings))
    if errors:
        for error in errors:
            typer.echo(f"{error.get('path', '')}: {error['message']}", err=True)
        raise typer.Exit(code=1)
    target = name or "all pipelines"
    typer.echo(f"Valid: {target}")


@pipeline_app.command(name="preflight")
def pipeline_preflight(
    name: str = typer.Argument(..., help="Pipeline name from cdt.yaml"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
):
    """Check required external tools and env keys for one pipeline."""
    cwd = Path.cwd()
    env = _load_project_env(cwd)
    config = load_pipeline_config(cwd)
    register_builtin_steps()
    load_plugins(config.plugins)
    payload = preflight_payload(config, name, env)
    if json_output:
        _echo_json(payload)
    else:
        typer.echo(f"Preflight: {name} ({payload['status']})")
        if payload["tools"]:
            typer.echo("Tools:")
            for check in payload["tools"]:
                marker = "ok" if check["available"] else "missing"
                typer.echo(f"  [{marker}] {check['name']}")
        if payload["env"]:
            typer.echo("Env:")
            for check in payload["env"]:
                marker = "ok" if check["present"] else "missing"
                typer.echo(f"  [{marker}] {check['name']}")
        for error in payload["errors"]:
            typer.echo(f"{error.get('path', '')}: {error['message']}", err=True)
    if payload["status"] != "ok":
        raise typer.Exit(code=1)


@pipeline_app.command(name="steps")
def pipeline_steps(json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON")):
    """List built-in and configured plugin steps."""
    cwd = Path.cwd()
    register_builtin_steps()
    config = None
    errors: list[dict[str, str]] = []
    if (cwd / "cdt.yaml").exists():
        if json_output:
            config, errors = _load_config_for_json(cwd)
            if config is not None:
                errors.extend(_load_plugins_for_json(config.plugins))
        else:
            config = load_pipeline_config(cwd)
            load_plugins(config.plugins)

    if json_output:
        _echo_json(steps_payload(config, errors=errors))
        if errors:
            raise typer.Exit(code=1)
        return

    for step_name in list_steps():
        typer.echo(step_name)


@agent_release_app.command(name="start")
def agent_release_start(
    pipeline: str = typer.Argument(..., help="Pipeline name from cdt.yaml"),
    id: list[str] = typer.Option([], "--id", help="Repeatable: --id A --id B"),
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
    if _confirmation_required(config, pipeline, confirm):
        payload = {
            "schema_version": 1,
            "status": "confirmation_required",
            "pipeline": pipeline,
            "required_confirmation": pipeline,
        }
        _echo_json(payload) if json_output else typer.echo(format_yamlish(payload))
        raise typer.Exit(code=2)
    payload = start_release(pipeline, ids=id, confirm=confirm)
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


app.add_typer(pipeline_app, name="pipeline")
app.add_typer(agent_release_app, name="agent-release")


def _confirmation_required(config, name: str, confirm: str | None) -> bool:
    pipeline = config.pipelines.get(name)
    return pipeline is not None and pipeline.risk == "production" and confirm != name


def _confirm_pipeline_risk(config, name: str, confirm: str | None) -> None:
    if not _confirmation_required(config, name, confirm):
        return
    if confirm is not None:
        raise typer.BadParameter(f"Production pipeline '{name}' requires --confirm {name}")
    entered = typer.prompt(f"Pipeline '{name}' is production. Enter the pipeline name to continue")
    if entered != name:
        raise typer.BadParameter(f"Production pipeline '{name}' was not confirmed")


def _pipeline_plan(cwd: Path, name: str, *, json_output: bool) -> None:
    if json_output:
        register_builtin_steps()
        config, errors = _load_config_for_json(cwd)
        if config is not None:
            errors.extend(_load_plugins_for_json(config.plugins))
            errors.extend(validate_pipeline(config, name))
            _echo_json(plan_payload(config, name, errors=errors))
        else:
            _echo_json(_error_payload(name, errors))
        if errors:
            raise typer.Exit(code=1)
        return

    config = load_pipeline_config(cwd)
    register_builtin_steps()
    load_plugins(config.plugins)
    errors = validate_pipeline(config, name)
    payload = plan_payload(config, name, errors=errors)
    if name not in config.pipelines:
        available = ", ".join(sorted(config.pipelines)) or "none"
        raise typer.BadParameter(f"Unknown pipeline: {name}. Available pipelines: {available}")
    typer.echo(f"Pipeline: {name}")
    typer.echo(f"Declared risk: {payload['declared_risk']}")
    typer.echo(f"Overall step risk: {payload['overall_risk']}")
    typer.echo("Steps:")
    _echo_plan_tree(payload["steps"])
    if payload["warnings"]:
        typer.echo("")
        typer.echo("Warnings:")
        for warning in payload["warnings"]:
            typer.echo(f"  {warning['message']}")
    if errors:
        typer.echo("")
        typer.echo("Errors:")
        for error in errors:
            typer.echo(f"  {error.get('path', '')}: {error['message']}")
        raise typer.Exit(code=1)


def _strict_warnings(config, name: str | None) -> list[dict[str, str]]:
    targets = [name] if name is not None else sorted(config.pipelines)
    warnings: list[dict[str, str]] = []
    for target in targets:
        warnings.extend(plan_payload(config, target, errors=[])["warnings"])
    return warnings


def _strict_errors(warnings: list[dict[str, str]]) -> list[dict[str, str]]:
    return [
        {
            "code": f"strict_{warning['code']}",
            "message": warning["message"],
            "path": warning.get("path", ""),
        }
        for warning in warnings
    ]


def _echo_json(payload: dict) -> None:
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


def _load_config_for_json(cwd: Path):
    try:
        return load_pipeline_config(cwd), []
    except typer.BadParameter as exc:
        return None, [{"code": "invalid_config", "message": str(exc), "path": "cdt.yaml"}]


def _load_plugins_for_json(plugins: list[str]) -> list[dict[str, str]]:
    try:
        load_plugins(plugins)
    except typer.BadParameter as exc:
        return [{"code": "plugin_import_failed", "message": str(exc), "path": "plugins"}]
    return []


def _error_payload(name: str | None, errors: list[dict[str, str]]) -> dict:
    return {
        "schema_version": 1,
        "pipeline": name,
        "pipelines": [],
        "plugins": [],
        "registered_steps": list_steps(),
        "errors": errors,
    }


def _echo_step_tree(nodes: list[dict], indent: int = 1) -> None:
    prefix = "  " * indent
    for node in nodes:
        if node["type"] in {"parallel", "sequence"}:
            typer.echo(f"{prefix}- {node.get('step_id', '?')} {node['type']}:")
            _echo_step_tree(node["steps"], indent + 1)
            continue
        typer.echo(f"{prefix}- {node.get('step_id', '?')} {node['name']}")
        if node["options"]:
            for key, value in node["options"].items():
                typer.echo(f"{prefix}    {key}: {value}")


def _echo_plan_tree(nodes: list[dict], indent: int = 1) -> None:
    prefix = "  " * indent
    for node in nodes:
        if node["type"] in {"parallel", "sequence"}:
            typer.echo(f"{prefix}- {node.get('step_id', '?')} {node['type']} [{node['risk']}]")
            _echo_plan_tree(node["steps"], indent + 1)
            continue
        typer.echo(f"{prefix}- {node.get('step_id', '?')} {node['name']} [{node['risk']}]")
        if node["options"]:
            for key, value in node["options"].items():
                typer.echo(f"{prefix}    {key}: {value}")


if __name__ == "__main__":
    app()
