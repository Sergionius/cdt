"""Standalone utility commands: init, doctor, schema, and self-update."""

import json
from pathlib import Path

import typer

from .. import __version__
from ..doctor import run_doctor
from ..init_project import initialize_project
from ..schema import bundled_schema_path
from ..self_update import SelfUpdateError, run_self_update

_DEFAULT_REPO_URL = "https://github.com/Sergionius/cdt"


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


def doctor(json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON")):
    """Check CDT environment health."""
    raise typer.Exit(code=run_doctor(Path.cwd(), json_output=json_output))


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
