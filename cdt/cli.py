import json
from pathlib import Path

import typer

from . import __version__
from .config import _load_project_env, _set_ui_mode
from .pipeline.builtins import register_builtin_steps
from .pipeline.config import load_pipeline_config, load_plugins
from .pipeline.planning import plan_payload
from .pipeline.registry import list_steps
from .pipeline.runner import run_configured_pipeline
from .pipeline.validation import inspect_payload, step_tree, steps_payload, validate_payload, validate_pipeline
from .self_update import run_self_update

app = typer.Typer(no_args_is_help=True)
pipeline_app = typer.Typer(no_args_is_help=True)

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
):
    """Update CDT to the latest release from GitHub."""
    exit_code = run_self_update(repo_url=_DEFAULT_REPO_URL, dry_run=dry_run)
    raise typer.Exit(code=exit_code)


@app.command(name="run")
def run_pipeline(
    name: str = typer.Argument(..., help="Pipeline name from cdt.yaml"),
    id: list[str] = typer.Option([], "--id", help="Repeatable: --id A --id B"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show pipeline plan without executing steps"),
):
    """Run a pipeline from cdt.yaml."""
    cwd = Path.cwd()
    if dry_run:
        _pipeline_plan(cwd, name, json_output=False)
        return
    env = _load_project_env(cwd)
    _set_ui_mode(env)
    run_configured_pipeline(cwd, env, name, ids=id)


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
):
    """Validate cdt.yaml schema and registered step names without running steps."""
    cwd = Path.cwd()
    if json_output:
        config, errors = _load_config_for_json(cwd)
        if config is not None:
            register_builtin_steps()
            errors.extend(_load_plugins_for_json(config.plugins))
            errors.extend(validate_pipeline(config, name))
        payload = validate_payload(config, name, errors=errors) if config is not None else _error_payload(name, errors)
        _echo_json(payload)
        if errors:
            raise typer.Exit(code=1)
        return

    config = load_pipeline_config(cwd)
    register_builtin_steps()
    load_plugins(config.plugins)
    errors = validate_pipeline(config, name)
    if errors:
        for error in errors:
            typer.echo(f"{error.get('path', '')}: {error['message']}", err=True)
        raise typer.Exit(code=1)
    target = name or "all pipelines"
    typer.echo(f"Valid: {target}")


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


app.add_typer(pipeline_app, name="pipeline")


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
    typer.echo(f"Overall risk: {payload['overall_risk']}")
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
        if node["type"] == "parallel":
            typer.echo(f"{prefix}- parallel:")
            _echo_step_tree(node["steps"], indent + 1)
            continue
        typer.echo(f"{prefix}- {node['name']}")
        if node["options"]:
            for key, value in node["options"].items():
                typer.echo(f"{prefix}    {key}: {value}")


def _echo_plan_tree(nodes: list[dict], indent: int = 1) -> None:
    prefix = "  " * indent
    for node in nodes:
        if node["type"] == "parallel":
            typer.echo(f"{prefix}- parallel [{node['risk']}]")
            _echo_plan_tree(node["steps"], indent + 1)
            continue
        typer.echo(f"{prefix}- {node['name']} [{node['risk']}]")
        if node["options"]:
            for key, value in node["options"].items():
                typer.echo(f"{prefix}    {key}: {value}")


if __name__ == "__main__":
    app()
