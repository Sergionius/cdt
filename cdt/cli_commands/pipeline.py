"""Pipeline inspection, planning, validation, preflight and step listing commands."""

import json
from pathlib import Path

import typer

from ..config import _load_project_env
from ..pipeline.builtins import register_builtin_steps
from ..pipeline.config import (
    load_pipeline_config,
    load_plugins,
    parse_pipeline_inputs,
    validate_pipeline_inputs,
)
from ..pipeline.planning import plan_payload
from ..pipeline.preflight import preflight_payload
from ..pipeline.registry import list_steps
from ..pipeline.validation import inspect_payload, step_tree, steps_payload, validate_payload, validate_pipeline
from .common import _echo_json

pipeline_app = typer.Typer(no_args_is_help=True)


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
    _echo_declared_inputs(config.pipelines[name].inputs)
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
    input: list[str] = typer.Option([], "--input", help="Repeatable pipeline input: --input KEY=VALUE"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
):
    """Show the static execution plan without running steps."""
    _pipeline_plan(Path.cwd(), name, json_output=json_output, inputs=parse_pipeline_inputs(input) if input else None)


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
    payload = preflight_payload(config, name, env, cwd=cwd)
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


def _pipeline_plan(cwd: Path, name: str, *, json_output: bool, inputs: dict[str, str] | None = None) -> None:
    if json_output:
        register_builtin_steps()
        config, errors = _load_config_for_json(cwd)
        if config is not None:
            errors.extend(_load_plugins_for_json(config.plugins))
            errors.extend(validate_pipeline(config, name))
            _validate_plan_inputs(config, name, inputs, errors)
            _echo_json(plan_payload(config, name, errors=errors, inputs=inputs))
        else:
            _echo_json(_error_payload(name, errors))
        if errors:
            raise typer.Exit(code=1)
        return

    config = load_pipeline_config(cwd)
    register_builtin_steps()
    load_plugins(config.plugins)
    errors = validate_pipeline(config, name)
    _validate_plan_inputs(config, name, inputs, errors)
    payload = plan_payload(config, name, errors=errors, inputs=inputs)
    if name not in config.pipelines:
        available = ", ".join(sorted(config.pipelines)) or "none"
        raise typer.BadParameter(f"Unknown pipeline: {name}. Available pipelines: {available}")
    typer.echo(f"Pipeline: {name}")
    typer.echo(f"Declared risk: {payload['declared_risk']}")
    typer.echo(f"Overall step risk: {payload['overall_risk']}")
    _echo_declared_inputs(config.pipelines[name].inputs)
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


def _validate_plan_inputs(config, name: str, inputs: dict[str, str] | None, errors: list[dict[str, str]]) -> None:
    """Validate run-mode inputs during dry-run; ``None`` inputs skip validation."""
    if inputs is None:
        return
    pipeline = config.pipelines.get(name)
    if pipeline is None:
        return
    try:
        validate_pipeline_inputs(pipeline, inputs)
    except typer.BadParameter as exc:
        errors.append({"code": "invalid_pipeline_input", "message": str(exc), "path": f"pipelines.{name}.inputs"})


def _echo_declared_inputs(inputs: dict) -> None:
    if not inputs:
        return
    typer.echo("Inputs:")
    for input_name, spec in inputs.items():
        details = []
        if spec.required:
            details.append("required")
        if spec.pattern:
            details.append(f"pattern: {spec.pattern}")
        suffix = f" ({', '.join(details)})" if details else ""
        typer.echo(f"  {input_name}{suffix}")


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
        typer.echo(f"{prefix}- {node.get('step_id', '?')} {node['name']} [{node['risk']}] {node['decision']}")
        if node.get("when") is not None:
            typer.echo(f"{prefix}    when: {json.dumps(node['when'], ensure_ascii=False)}")
        if node["options"]:
            for key, value in node["options"].items():
                typer.echo(f"{prefix}    {key}: {value}")
