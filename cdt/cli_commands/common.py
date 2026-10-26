"""CLI helpers shared by several command modules."""

import json

import typer


def _echo_json(payload: dict) -> None:
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))
