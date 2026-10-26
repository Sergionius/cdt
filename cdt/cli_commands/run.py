"""Root run command: flag and input validation, confirmation, capture supervision and finalization."""

import os
from collections.abc import Sequence
from pathlib import Path

import typer

from ..config import _load_project_env, _set_ui_mode
from ..foreground_run import ForegroundCaptureError, build_child_command, require_posix_capture, run_captured_child
from ..orca_status import report as report_orca_status
from ..pipeline.config import load_pipeline_config, parse_pipeline_inputs, validate_pipeline_inputs
from ..pipeline.executor import PipelineExecutionError
from ..pipeline.runner import run_configured_pipeline
from ..redaction import SecretRedactor
from ..retention import auto_prune_after_run
from ..runs import (
    RUN_SCHEMA_VERSION,
    acquire_run_lock,
    create_run,
    now,
    project_lock,
    read_json,
    release_run_lock,
    write_exit_code,
    write_json_atomic,
    write_text_atomic,
)
from .common import _confirmation_required
from .pipeline import _pipeline_plan


def run_pipeline(
    name: str = typer.Argument(..., help="Pipeline name from cdt.yaml"),
    id: list[str] = typer.Option([], "--id", help="Repeatable: --id A --id B"),
    input: list[str] = typer.Option([], "--input", help="Repeatable pipeline input: --input KEY=VALUE"),
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
    capture_output: bool = typer.Option(
        False,
        "--capture-output",
        help="Foreground run that shows and saves the redacted combined output (POSIX only, no stdin)",
    ),
    capture_child: bool = typer.Option(False, "--capture-child", hidden=True),
):
    """Run a pipeline from cdt.yaml."""
    cwd = Path.cwd()
    inputs = parse_pipeline_inputs(input)
    if dry_run:
        _pipeline_plan(cwd, name, json_output=False, inputs=inputs)
        return
    _validate_capture_flags(capture_output, capture_child, run_id)
    config = load_pipeline_config(cwd)
    _validate_inputs(config, name, inputs)
    if capture_output and confirm is None and _confirmation_required(config, name, confirm):
        raise typer.BadParameter(f"Captured runs cannot prompt: production pipeline '{name}' requires --confirm {name}")
    _confirm_pipeline_risk(config, name, confirm)
    env = _load_project_env(cwd)
    _set_ui_mode(env)
    if capture_output:
        exit_code = _run_capture_output(
            cwd,
            env,
            name,
            ids=id,
            inputs=inputs,
            status_file=status_file,
            resume_from=resume_from,
            skip_completed=skip_completed,
            resume_status_file=resume_status_file,
            confirm=confirm,
        )
        if exit_code != 0:
            raise typer.Exit(code=exit_code)
        return
    # The mode is decided by explicit flags: --capture-child marks the supervised
    # capture child, --run-id alone marks the detached worker child, and neither
    # means an ordinary direct run. Run-id presence alone is never the source.
    direct_run = run_id is None and not capture_child
    if direct_run:
        report_orca_status("working", name)
    succeeded = False
    try:
        completed_run_id = run_configured_pipeline(
            cwd,
            env,
            name,
            ids=id,
            inputs=inputs,
            status_file=status_file,
            resume_from=resume_from,
            skip_completed=skip_completed,
            resume_status_file=resume_status_file,
            run_id=run_id,
            detached=run_id is not None,
            capture_child=capture_child,
        )
        succeeded = True
    except PipelineExecutionError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    finally:
        if direct_run:
            report_orca_status("done", name, failed=not succeeded)
    if completed_run_id is not None and direct_run:
        typer.echo(f"Run: {completed_run_id}")


_CAPTURE_CHILD_FLAG = "--capture-child"
_TERMINAL_RUN_STATUSES = {"success", "failed", "cancelled", "blocked"}


def _validate_capture_flags(capture_output: bool, capture_child: bool, run_id: str | None) -> None:
    """Reject impossible capture flag combinations before any run record exists."""
    if capture_child:
        if capture_output:
            raise typer.BadParameter(
                "Recursive foreground capture is not supported: "
                f"{_CAPTURE_CHILD_FLAG} cannot be combined with --capture-output"
            )
        if run_id is None:
            raise typer.BadParameter(f"Internal {_CAPTURE_CHILD_FLAG} requires an existing --run-id")
        return
    if capture_output and run_id is not None:
        raise typer.BadParameter(
            "--capture-output creates its own foreground run record and cannot be combined with --run-id"
        )


def _run_capture_output(
    cwd: Path,
    env: dict[str, str],
    name: str,
    *,
    ids: list[str],
    inputs: dict[str, str],
    status_file: Path | None,
    resume_from: str | None,
    skip_completed: bool,
    resume_status_file: Path | None,
    confirm: str | None,
) -> int:
    """Supervise one captured foreground child CDT run and finalize its record."""
    try:
        require_posix_capture()
    except ForegroundCaptureError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    report_orca_status("working", name)
    failed = True
    lifecycle = None
    captured_run_id: str | None = None
    try:
        paths = create_run(
            cwd,
            name,
            ids=ids,
            command=_capture_manifest_command(
                name,
                ids=ids,
                inputs=inputs,
                status_file=status_file,
                resume_from=resume_from,
                skip_completed=skip_completed,
                resume_status_file=resume_status_file,
                confirm=confirm,
            ),
            detached=False,
            inputs=inputs,
            env=env,
            capture_output=True,
        )
        # The supervisor owns the live pid and the final exit code; the child
        # writes only the pipeline status and artifacts.
        write_text_atomic(paths.pid, f"{os.getpid()}\n")
        with project_lock(cwd):
            # The supervisor keeps the lifecycle lock until the final status and
            # exit code are written, so pruning cannot race the capture; the
            # supervised child never acquires it itself.
            lifecycle = acquire_run_lock(paths, timeout=0.0)
        captured_run_id = paths.run_id
        child_args = _capture_child_args(
            name,
            paths.run_id,
            ids=ids,
            inputs=inputs,
            status_file=status_file,
            resume_from=resume_from,
            skip_completed=skip_completed,
            resume_status_file=resume_status_file,
            confirm=confirm,
        )
        # Forced verbose transport lives only in the child environment: external
        # commands inherit stdout/stderr instead of pretty temp logs, so the
        # supervisor sees and saves their output. The parent keeps its own mode.
        child_env = {**env, "CDT_UI": "verbose"}
        try:
            result = run_captured_child(build_child_command(child_args), cwd=cwd, log_path=paths.log, env=child_env)
        except ForegroundCaptureError as exc:
            # Startup failure: the pipeline never executed; record a safe error.
            typer.echo(f"Error: {exc}", err=True)
            exit_code = _finalize_captured_run(env, paths, name, status_file, 1, str(exc))
            typer.echo(f"Run: {paths.run_id}")
            return exit_code
        # An incomplete capture never counts as a clean success: the whole
        # point of --capture-output is the saved, redacted combined output.
        exit_code = _finalize_captured_run(
            env, paths, name, status_file, _terminal_exit_code(result), None, capture_problems=result.capture_errors
        )
        if not result.capture_ok:
            typer.echo(
                "Warning: foreground capture was incomplete; the run is not reported as a success",
                err=True,
            )
        failed = exit_code != 0
        typer.echo(f"Run: {paths.run_id}")
        return exit_code
    finally:
        release_run_lock(lifecycle)
        if captured_run_id is not None:
            # The supervisor is the capture owner: after finalizing status, exit
            # code and log it runs the experimental auto-prune exactly once,
            # excluding the just-finished record from its own cleanup pass.
            auto_prune_after_run(cwd, captured_run_id)
        report_orca_status("done", name, failed=failed)


def _capture_flags(
    *,
    inputs: dict[str, str],
    ids: list[str],
    status_file: Path | None,
    resume_from: str | None,
    skip_completed: bool,
    resume_status_file: Path | None,
    confirm: str | None,
) -> list[str]:
    """User-facing run options shared by the manifest command and the child argv."""
    flags: list[str] = []
    for input_name, input_value in inputs.items():
        flags.extend(["--input", f"{input_name}={input_value}"])
    for task_id in ids or []:
        flags.extend(["--id", task_id])
    if resume_from is not None:
        flags.extend(["--resume-from", resume_from])
    if skip_completed:
        flags.append("--skip-completed")
    if resume_status_file is not None:
        flags.extend(["--resume-status-file", str(resume_status_file)])
    if status_file is not None:
        flags.extend(["--status-file", str(status_file)])
    if confirm is not None:
        flags.extend(["--confirm", confirm])
    return flags


def _capture_manifest_command(
    name, *, ids, inputs, status_file, resume_from, skip_completed, resume_status_file, confirm
):
    command = ["cdt", "run", name, "--capture-output"]
    command.extend(
        _capture_flags(
            inputs=inputs,
            ids=ids,
            status_file=status_file,
            resume_from=resume_from,
            skip_completed=skip_completed,
            resume_status_file=resume_status_file,
            confirm=confirm,
        )
    )
    return command


def _capture_child_args(
    name, run_id, *, ids, inputs, status_file, resume_from, skip_completed, resume_status_file, confirm
):
    """Child argv tail: never --capture-output, always the hidden service flag."""
    args = ["run", name, "--run-id", run_id, _CAPTURE_CHILD_FLAG]
    args.extend(
        _capture_flags(
            inputs=inputs,
            ids=ids,
            status_file=status_file,
            resume_from=resume_from,
            skip_completed=skip_completed,
            resume_status_file=resume_status_file,
            confirm=confirm,
        )
    )
    return args


def _terminal_exit_code(result) -> int:
    """Report signal terminations using the conventional 128+signal exit code."""
    if result.exit_code < 0:
        return 128 - result.exit_code
    return result.exit_code


def _finalize_captured_run(
    env: dict[str, str],
    paths,
    name: str,
    status_file: Path | None,
    exit_code: int,
    startup_error: str | None,
    capture_problems: Sequence[str] = (),
) -> int:
    """Record a safe terminal outcome once the child can no longer write the files.

    The child is reaped at this point, so nothing competes for the run status
    or the user's mirror status file. A missing terminal status is recorded as
    a failure with a readable error instead of a fake success; already saved
    artifacts, completed steps and checkpoints are preserved untouched.

    ``capture_problems`` are the supervisor's capture-side failures. They never
    upgrade an existing failure and never let a child success stand: an
    incomplete capture fails the run with a safe error while keeping the
    child's progress, artifacts and non-success status untouched.
    """
    payload = read_json(paths.status) or {}
    if capture_problems:
        if exit_code == 0:
            exit_code = 1
        if payload.get("status") == "success":
            payload.update(
                {
                    "status": "failed",
                    "error": "Foreground capture was incomplete: " + "; ".join(capture_problems),
                    "finished_at": now(),
                    "updated_at": now(),
                }
            )
            sanitized = SecretRedactor.from_env(env).redact_data(payload)
            write_json_atomic(paths.status, sanitized)
            if status_file is not None and status_file != paths.status:
                write_json_atomic(status_file, sanitized)
    elif payload.get("status") not in _TERMINAL_RUN_STATUSES:
        if startup_error:
            message = startup_error
        elif exit_code == 0:
            exit_code = 1
            message = "CDT subprocess exited without writing a terminal status"
        else:
            message = f"CDT subprocess exited with code {exit_code} before writing a terminal status"
        payload.update(
            {
                "schema_version": RUN_SCHEMA_VERSION,
                "run_id": paths.run_id,
                "pipeline": payload.get("pipeline") or name,
                "status": "failed",
                "error": message,
                "finished_at": now(),
                "updated_at": now(),
            }
        )
        sanitized = SecretRedactor.from_env(env).redact_data(payload)
        write_json_atomic(paths.status, sanitized)
        if status_file is not None and status_file != paths.status:
            write_json_atomic(status_file, sanitized)
    write_exit_code(paths.exit, exit_code)
    return exit_code


def _validate_inputs(config, name: str, inputs: dict[str, str]) -> None:
    pipeline = config.pipelines.get(name)
    if pipeline is not None:
        validate_pipeline_inputs(pipeline, inputs)


def _confirm_pipeline_risk(config, name: str, confirm: str | None) -> None:
    if not _confirmation_required(config, name, confirm):
        return
    if confirm is not None:
        raise typer.BadParameter(f"Production pipeline '{name}' requires --confirm {name}")
    entered = typer.prompt(f"Pipeline '{name}' is production. Enter the pipeline name to continue")
    if entered != name:
        raise typer.BadParameter(f"Production pipeline '{name}' was not confirmed")
