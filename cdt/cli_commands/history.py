"""Run history, pruning and the root status/logs commands."""

from datetime import datetime, timezone
from pathlib import Path

import typer

from ..agent_release import format_yamlish, release_status
from ..config import _load_project_env
from ..redaction import SecretRedactor
from ..retention import RetentionError, build_retention_plan, execute_retention, parse_older_than_days
from ..runs import RUN_SCHEMA_VERSION, list_runs, resolve_run, runs_dir
from .common import _echo_json

history_app = typer.Typer(invoke_without_command=True)


@history_app.callback(invoke_without_command=True)
def history(
    ctx: typer.Context,
    limit: int = typer.Option(20, "--limit", min=1, max=500, help="Maximum runs to show"),
    pipeline: str | None = typer.Option(None, "--pipeline", help="Only show runs for this pipeline"),
    status_filter: str | None = typer.Option(None, "--status", help="Only show runs with this effective status"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
):
    """List recent pipeline runs."""
    if ctx.invoked_subcommand is not None:
        return
    _list_history(limit=limit, pipeline=pipeline, status_filter=status_filter, json_output=json_output)


def _list_history(
    *,
    limit: int,
    pipeline: str | None,
    status_filter: str | None,
    json_output: bool,
) -> None:
    valid_statuses = {"queued", "running", "success", "failed", "cancelled", "blocked", "stale", "unknown"}
    if status_filter is not None and status_filter not in valid_statuses:
        choices = ", ".join(sorted(valid_statuses))
        raise typer.BadParameter(f"Unknown run status: {status_filter}. Available statuses: {choices}")
    runs = list_runs(Path.cwd(), limit=limit, pipeline=pipeline, status=status_filter)
    if json_output:
        _echo_json(
            {
                "schema_version": 1,
                "filters": {"pipeline": pipeline, "status": status_filter, "limit": limit},
                "runs": runs,
            }
        )
        return
    if not runs:
        message = "No recorded runs matched the selected filters." if pipeline or status_filter else "No recorded runs."
        typer.echo(message)
        return
    for item in runs:
        typer.echo(f"{item['run_id']}  {item['status']:<9}  {item['pipeline']}")


@history_app.command(name="prune")
def history_prune(
    older_than: str | None = typer.Option(
        None,
        "--older-than",
        help="Consider finished runs strictly older than this age, e.g. 30d",
    ),
    keep_last: int | None = typer.Option(
        None,
        "--keep-last",
        min=0,
        help="Protect the N most recent runs of every pipeline from deletion",
    ),
    pipeline: str | None = typer.Option(None, "--pipeline", help="Only prune runs of this pipeline"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview deletion without removing anything"),
    confirm: bool = typer.Option(False, "--confirm", help="Delete the selected run records"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
):
    """Delete recorded runs by age and count; without --confirm only a preview is shown."""
    if dry_run and confirm:
        raise typer.BadParameter("--dry-run and --confirm are mutually exclusive; choose one")
    if older_than is None and keep_last is None:
        raise typer.BadParameter("Provide at least one filter: --older-than Nd or --keep-last N")
    try:
        older_than_days = parse_older_than_days(older_than) if older_than is not None else None
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    filters = {"older_than_days": older_than_days, "keep_last": keep_last, "pipeline": pipeline}
    mode = "delete" if confirm else "preview"
    try:
        if runs_dir(Path.cwd()).is_symlink():
            raise RetentionError("Refusing to prune runs: .cdt/runs is a symbolic link")
        # With --confirm the plan is built and verified right before deletion;
        # an earlier preview never guarantees that history has not changed.
        plan = build_retention_plan(
            Path.cwd(),
            now=datetime.now(timezone.utc),
            older_than_days=older_than_days,
            keep_last=keep_last,
            pipeline=pipeline,
        )
        result = execute_retention(Path.cwd(), plan) if confirm else None
    except RetentionError as exc:
        if json_output:
            _echo_json(_prune_payload(mode=mode, filters=filters, errors=[{"run_id": None, "error": str(exc)}]))
        else:
            typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    skipped = [*plan.skipped, *(result.skipped if result else [])]
    errors = [{"run_id": failure.run_id, "error": failure.error} for failure in result.failed] if result else []
    if json_output:
        _echo_json(
            _prune_payload(
                mode=mode,
                filters=filters,
                candidates=[_prune_record_payload(record) for record in plan.candidates],
                deleted=[_prune_record_payload(record) for record in result.deleted] if result else [],
                skipped=[_prune_skip_payload(skip) for skip in skipped],
                errors=errors,
                total_size_bytes=plan.total_size_bytes,
                freed_bytes=result.freed_bytes if result else 0,
            )
        )
    else:
        _echo_prune_text(plan=plan, result=result, skipped=skipped)
    # Expected skips (for example an active run) are not errors; only read or
    # deletion failures that leave the cleanup incomplete fail the command.
    raise typer.Exit(code=1 if errors else 0)


def _prune_payload(
    *,
    mode: str,
    filters: dict,
    candidates: list[dict] | None = None,
    deleted: list[dict] | None = None,
    skipped: list[dict] | None = None,
    errors: list[dict] | None = None,
    total_size_bytes: int = 0,
    freed_bytes: int = 0,
) -> dict:
    return {
        "schema_version": RUN_SCHEMA_VERSION,
        "mode": mode,
        "filters": filters,
        "candidates": candidates or [],
        "deleted": deleted or [],
        "skipped": skipped or [],
        "errors": errors or [],
        "total_size_bytes": total_size_bytes,
        "freed_bytes": freed_bytes,
    }


def _prune_record_payload(record) -> dict:
    return {
        "run_id": record.run_id,
        "pipeline": record.pipeline,
        "status": record.status,
        "started_at": record.started_at,
        "age_days": round(record.age_days, 2),
        "size_bytes": record.size_bytes,
    }


def _prune_skip_payload(skip) -> dict:
    return {"run_id": skip.run_id, "pipeline": skip.pipeline, "reason": skip.reason}


def _echo_prune_text(plan, result, skipped) -> None:
    if not plan.candidates:
        typer.echo("No deletable run records matched the selected filters.")
    elif result is None:
        total = _format_bytes(plan.total_size_bytes)
        typer.echo(f"{len(plan.candidates)} recorded run(s) would be deleted ({total} total):")
        for record in plan.candidates:
            _echo_prune_record(record)
    else:
        typer.echo(f"Deleted {len(result.deleted)} recorded run(s), freed {_format_bytes(result.freed_bytes)}:")
        for record in result.deleted:
            _echo_prune_record(record)
    if skipped:
        typer.echo(f"Skipped records ({len(skipped)}):")
        for skip in skipped:
            typer.echo(f"  {skip.run_id}  {skip.reason}")
    if result is not None:
        for failure in result.failed:
            typer.echo(f"Error: {failure.run_id}: {failure.error}", err=True)
        if not result.failed:
            typer.echo("Deletion complete.")
    else:
        typer.echo(
            "Warning: pruning deletes the entire run record of any result, "
            "including logs and data needed to resume."
        )
        typer.echo("This was a preview; nothing was deleted. Re-run with --confirm to delete these records.")


def _echo_prune_record(record) -> None:
    details = (
        f"{record.pipeline or '?'}  {record.status}  "
        f"{_format_age(record.age_days)}  {_format_bytes(record.size_bytes)}"
    )
    typer.echo(f"  {record.run_id}  {details}")


def _format_age(age_days: float) -> str:
    return f"{age_days:.1f}d"


def _format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def run_status(
    run_id: str | None = typer.Argument(None, help="Run id from cdt history; defaults to the newest run"),
    pipeline: str | None = typer.Option(None, "--pipeline", help="Use the newest run for this pipeline"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
):
    """Show a recorded pipeline run."""
    payload = release_status(pipeline=pipeline, run_id=run_id)
    _echo_json(payload) if json_output else typer.echo(format_yamlish(payload))
    if payload["status"] == "unknown":
        raise typer.Exit(code=1)


def run_logs(
    run_id: str | None = typer.Argument(None, help="Run id from cdt history; defaults to the newest run"),
    pipeline: str | None = typer.Option(None, "--pipeline", help="Use the newest run for this pipeline"),
    tail: int = typer.Option(80, "--tail", min=1, max=10000, help="Number of trailing lines"),
):
    """Print the tail of a recorded run log."""
    cwd = Path.cwd()
    paths = resolve_run(cwd, run_id=run_id, pipeline=pipeline)
    if paths is None:
        selector = f"run id: {run_id}" if run_id else f"pipeline: {pipeline}" if pipeline else "recent runs"
        raise typer.BadParameter(f"Unknown {selector}")
    try:
        lines = paths.log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise typer.BadParameter(f"Cannot read run log: {exc}") from exc
    redactor = SecretRedactor.from_env(_load_project_env(cwd))
    typer.echo(redactor.redact("\n".join(lines[-tail:])))
