"""CLI helpers shared by several command modules."""

import json

import typer


def _echo_json(payload: dict) -> None:
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


def _confirmation_required(config, name: str, confirm: str | None) -> bool:
    pipeline = config.pipelines.get(name)
    return pipeline is not None and pipeline.risk == "production" and confirm != name
