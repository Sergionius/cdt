"""Per-user experimental settings commands."""

import typer

from ..retention import AUTO_PRUNE_POLICY_TEXT, AUTO_PRUNE_SCOPE_TEXT
from ..user_settings import (
    EXPERIMENTAL_FLAGS,
    ORCA_STATUS,
    RUN_RETENTION,
    orca_status_enabled,
    run_retention_enabled,
    set_experimental_flag,
)

settings_app = typer.Typer(no_args_is_help=True)


@settings_app.command(name="enable")
def settings_enable(name: str = typer.Argument(..., help="Experimental setting to enable")):
    """Enable a per-user experimental feature."""
    _set_experimental_setting(name, True)


@settings_app.command(name="disable")
def settings_disable(name: str = typer.Argument(..., help="Experimental setting to disable")):
    """Disable a per-user experimental feature."""
    _set_experimental_setting(name, False)


@settings_app.command(name="show")
def settings_show():
    """Show per-user experimental settings."""
    typer.echo(f"{ORCA_STATUS}: {'enabled' if orca_status_enabled() else 'disabled'}")
    retention_enabled = run_retention_enabled()
    typer.echo(f"{RUN_RETENTION}: {'enabled' if retention_enabled else 'disabled'}")
    if retention_enabled:
        _echo_auto_prune_scope_and_policy()


def _echo_auto_prune_scope_and_policy() -> None:
    typer.echo(f"  scope: {AUTO_PRUNE_SCOPE_TEXT}")
    typer.echo(f"  policy: {AUTO_PRUNE_POLICY_TEXT}")


def _set_experimental_setting(name: str, enabled: bool) -> None:
    if name not in EXPERIMENTAL_FLAGS:
        available = ", ".join(EXPERIMENTAL_FLAGS)
        raise typer.BadParameter(f"Unknown setting: {name}. Available: {available}")
    try:
        path = set_experimental_flag(name, enabled)
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(f"Cannot save CDT settings: {exc}") from exc
    typer.echo(f"{name}: {'enabled' if enabled else 'disabled'} ({path})")
    if name == RUN_RETENTION and enabled:
        # Make the experimental scope explicit: the flag is stored globally,
        # but the auto-prune itself only ever touches the current project.
        _echo_auto_prune_scope_and_policy()
