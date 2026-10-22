"""Selection and safe removal of recorded runs for history retention.

:func:`build_retention_plan` builds a deterministic deletion plan without
modifying any files; :func:`execute_retention` performs the actual removal.
Only finished runs may become candidates; active, unverifiable and corrupted
records are always skipped with an explicit reason.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import filelock

from .runs import (
    PROJECT_LOCK_TIMEOUT,
    RUN_SCHEMA_VERSION,
    acquire_run_lock,
    latest_marker,
    list_all_runs,
    list_runs,
    project_lock,
    read_json,
    release_run_lock,
    run_paths,
    runs_dir,
    set_latest_run,
)
from .user_settings import run_retention_enabled

TERMINAL_RUN_STATUSES = frozenset({"success", "failed", "cancelled", "blocked"})
# A run whose recorded process is confirmed dead ("stale") can no longer make
# progress, so it is removable as well.
REMOVABLE_RUN_STATUSES = TERMINAL_RUN_STATUSES | {"stale"}

_SECONDS_PER_DAY = 86400

_KEPT_REASON_YOUNG = "younger than the age limit"
_KEPT_REASON_PROTECTED = "protected by keep-last"

# Fixed experimental auto-retention policy: both filters apply at once, so a
# candidate must be old enough *and* outside every pipeline's protected window.
AUTO_PRUNE_OLDER_THAN_DAYS = 30
AUTO_PRUNE_KEEP_LAST = 10
AUTO_PRUNE_SCOPE_TEXT = "the setting is global, but every run prunes only the current project's .cdt/runs"
AUTO_PRUNE_POLICY_TEXT = (
    "deletes finished runs older than 30 days and outside the 10 newest records of each pipeline"
)


@dataclass(frozen=True)
class RetentionRecord:
    """A run selected for deletion."""

    run_id: str
    pipeline: str | None
    status: str
    started_at: str | None
    age_days: float
    size_bytes: int


@dataclass(frozen=True)
class RetentionKept:
    """A run that stays because the filters do not select it."""

    record: RetentionRecord
    reason: str


@dataclass(frozen=True)
class RetentionSkip:
    """A run that cannot be judged safely and is left untouched."""

    run_id: str
    pipeline: str | None
    reason: str


@dataclass(frozen=True)
class RetentionPlan:
    """Deterministic retention outcome; building it never touches files."""

    schema_version: int
    generated_at: str
    filters: dict[str, int | str | None]
    candidates: list[RetentionRecord]
    kept: list[RetentionKept]
    skipped: list[RetentionSkip]
    total_size_bytes: int


@dataclass(frozen=True)
class RetentionFailure:
    """A candidate whose directory could not be fully removed."""

    run_id: str
    error: str


@dataclass(frozen=True)
class RetentionResult:
    """Outcome of an executed retention pass."""

    schema_version: int
    mode: str
    deleted: list[RetentionRecord]
    skipped: list[RetentionSkip]
    failed: list[RetentionFailure]
    freed_bytes: int


class RetentionError(RuntimeError):
    """Retention cannot run in this project right now."""


def parse_older_than_days(value: str | int) -> int:
    """Parse an ``Nd`` age filter into a positive integer number of days."""
    text = str(value).strip().lower()
    if text.endswith("d"):
        text = text[:-1]
    try:
        days = int(text)
    except ValueError:
        raise ValueError(f"Expected a positive integer number of days, got {value!r}") from None
    if days < 1:
        raise ValueError(f"Expected a positive integer number of days, got {value!r}")
    return days


def build_retention_plan(
    cwd: Path,
    *,
    now: datetime,
    older_than_days: int | None = None,
    keep_last: int | None = None,
    pipeline: str | None = None,
) -> RetentionPlan:
    """Compute which recorded runs a retention pass would delete.

    ``older_than_days`` keeps only runs strictly older than the cutoff; age is
    taken from ``manifest.started_at`` in UTC. ``keep_last`` protects the N most
    recent records of every pipeline (ordered by start time, then run id). With
    both filters only runs that are old enough *and* outside the protected
    windows become candidates. Active, unverifiable and corrupted records are
    skipped with explicit reasons.
    """
    now_utc = _as_utc(now)
    if older_than_days is not None:
        _require_int(older_than_days, minimum=1, name="older_than_days")
    if keep_last is not None:
        _require_int(keep_last, minimum=0, name="keep_last")
    if older_than_days is None and keep_last is None:
        raise ValueError("Retention requires an age or count limit; refusing to select every run")

    cutoff = now_utc - timedelta(days=older_than_days) if older_than_days is not None else None
    items = list_all_runs(cwd, pipeline=pipeline)
    protected = _keep_last_ids(items, keep_last) if keep_last is not None else set()

    candidates: list[RetentionRecord] = []
    kept: list[RetentionKept] = []
    skipped: list[RetentionSkip] = []
    for item in items:
        run_id = str(item["run_id"])
        item_pipeline = item.get("pipeline")
        try:
            paths = run_paths(cwd, run_id)
        except ValueError:
            skipped.append(RetentionSkip(run_id=run_id, pipeline=item_pipeline, reason="invalid run id"))
            continue
        if paths.root.is_symlink():
            skipped.append(RetentionSkip(run_id=run_id, pipeline=item_pipeline, reason="run directory is a symlink"))
            continue
        evaluated = _evaluate_run(paths, now_utc=now_utc)
        if isinstance(evaluated, RetentionSkip):
            skipped.append(evaluated)
            continue
        started_at = _parse_timestamp(evaluated.started_at)
        if cutoff is not None and started_at is not None and started_at >= cutoff:
            kept.append(RetentionKept(record=evaluated, reason=_KEPT_REASON_YOUNG))
        elif run_id in protected:
            kept.append(RetentionKept(record=evaluated, reason=_KEPT_REASON_PROTECTED))
        else:
            candidates.append(evaluated)

    candidates.sort(key=lambda record: record.run_id)
    kept.sort(key=lambda entry: entry.record.run_id)
    skipped.sort(key=lambda entry: entry.run_id)
    return RetentionPlan(
        schema_version=RUN_SCHEMA_VERSION,
        generated_at=now_utc.isoformat(),
        filters={"older_than_days": older_than_days, "keep_last": keep_last, "pipeline": pipeline},
        candidates=candidates,
        kept=kept,
        skipped=skipped,
        total_size_bytes=sum(record.size_bytes for record in candidates),
    )


def execute_retention(
    cwd: Path,
    plan: RetentionPlan,
    *,
    lock_timeout: float = PROJECT_LOCK_TIMEOUT,
) -> RetentionResult:
    """Delete the plan's candidates with lifecycle-aware safety checks.

    The whole pass runs under one project lock, which also guards record
    creation and reuse: an owner can never take a run's lifecycle lock between
    the safety re-check and the removal, and a starting run can never claim a
    record mid-deletion. For every candidate the per-run lock is probed without
    blocking, the state is re-read and the process is re-checked immediately
    before removal, so a record that merely looks terminal cannot be deleted
    while its owner is still writing it. Only the selected immediate directory
    inside the real ``.cdt/runs/`` is removed; symlinks are never followed and
    paths recorded in a status are never treated as deletion targets. A file
    error on one candidate never stops the remaining candidates.
    """
    base = runs_dir(cwd)
    if base.is_symlink():
        raise RetentionError("Refusing to prune runs: .cdt/runs is a symbolic link")
    guard = project_lock(cwd, timeout=lock_timeout)
    try:
        guard.acquire()
    except filelock.Timeout as exc:
        raise RetentionError("Another CDT run operation is in progress; retry pruning later") from exc
    try:
        deleted: list[RetentionRecord] = []
        skipped: list[RetentionSkip] = []
        failed: list[RetentionFailure] = []
        now_utc = _as_utc(datetime.now(timezone.utc))
        for record in plan.candidates:
            outcome = _delete_candidate(cwd, base, record, now_utc=now_utc)
            if isinstance(outcome, RetentionFailure):
                failed.append(outcome)
            elif isinstance(outcome, RetentionSkip):
                skipped.append(outcome)
            else:
                deleted.append(outcome)
        _refresh_latest_markers(cwd, deleted)
    finally:
        release_run_lock(guard)
    return RetentionResult(
        schema_version=RUN_SCHEMA_VERSION,
        mode="delete",
        deleted=deleted,
        skipped=skipped,
        failed=failed,
        freed_bytes=sum(record.size_bytes for record in deleted),
    )


def run_auto_prune(
    cwd: Path,
    *,
    exclude_run_id: str | None = None,
    now_value: datetime | None = None,
) -> RetentionResult | None:
    """Apply the fixed experimental auto-retention policy to the current project.

    Does nothing (returns ``None`` without touching any files) while the
    global ``experimental.run-retention`` setting is off, unreadable or the
    fixed policy selects no candidates. With the setting on, the one pass
    deletes finished runs older than :data:`AUTO_PRUNE_OLDER_THAN_DAYS` days
    that also fall outside the :data:`AUTO_PRUNE_KEEP_LAST` newest records of
    their pipeline. ``exclude_run_id`` (the just-finished run) is never a
    candidate of its own cleanup pass.
    """
    if not run_retention_enabled():
        return None
    moment = _as_utc(now_value) if now_value is not None else datetime.now(timezone.utc)
    plan = build_retention_plan(
        cwd,
        now=moment,
        older_than_days=AUTO_PRUNE_OLDER_THAN_DAYS,
        keep_last=AUTO_PRUNE_KEEP_LAST,
    )
    if exclude_run_id is not None:
        plan = _exclude_from_plan(plan, exclude_run_id)
    if not plan.candidates:
        return None
    return execute_retention(cwd, plan)


def auto_prune_after_run(cwd: Path, run_id: str | None = None) -> RetentionResult | None:
    """Best-effort auto-prune for run owners; never raises and never blocks.

    Every failure is reported as a stderr warning only: an auto-prune problem
    must not change the run's status, exit code or original pipeline
    exception. Call this exactly once, after the owner has finalized the
    status/exit code and closed the run log.
    """
    try:
        return run_auto_prune(cwd, exclude_run_id=run_id)
    except Exception as exc:  # noqa: BLE001 - retention must never break a finished run
        print(f"Warning: experimental run retention failed: {exc}", file=sys.stderr)
        return None


def _exclude_from_plan(plan: RetentionPlan, run_id: str) -> RetentionPlan:
    """Drop one run from a plan's candidates without touching the files."""
    candidates = [record for record in plan.candidates if record.run_id != run_id]
    if len(candidates) == len(plan.candidates):
        return plan
    return replace(plan, candidates=candidates, total_size_bytes=sum(record.size_bytes for record in candidates))


def _delete_candidate(
    cwd: Path,
    base: Path,
    record: RetentionRecord,
    *,
    now_utc: datetime,
) -> RetentionRecord | RetentionSkip | RetentionFailure:
    """Remove one candidate; never raises for expected filesystem states."""
    try:
        paths = run_paths(cwd, record.run_id)
    except ValueError:
        return RetentionSkip(run_id=record.run_id, pipeline=record.pipeline, reason="invalid run id")
    if paths.root.parent != base:
        # run_paths already pins the record to the real .cdt/runs directory;
        # this defensive check keeps removal inside it even if that changes.
        return RetentionSkip(
            run_id=record.run_id,
            pipeline=record.pipeline,
            reason="run directory is outside .cdt/runs",
        )
    if paths.root.is_symlink():
        return RetentionSkip(run_id=record.run_id, pipeline=record.pipeline, reason="run directory is a symlink")
    if not paths.root.is_dir():
        return RetentionSkip(
            run_id=record.run_id,
            pipeline=record.pipeline,
            reason="run directory is no longer present",
        )
    # Non-blocking lifecycle check: a held lock proves an owner is still
    # finalizing the record even when status and exit code look terminal.
    lock = acquire_run_lock(paths, timeout=0.0)
    if lock is None:
        return RetentionSkip(
            run_id=record.run_id,
            pipeline=record.pipeline,
            reason="run is active (lifecycle lock held by its owner)",
        )
    try:
        # Re-read the state and re-check the process right before deletion;
        # records predating the lock protocol fall back to the conservative
        # PID/status verification of _evaluate_run.
        recheck = _evaluate_run(paths, now_utc=now_utc)
    finally:
        # Releasing before removal is safe under the project lock: a new owner
        # must take the same project lock before it can acquire this run.
        release_run_lock(lock)
    if isinstance(recheck, RetentionSkip):
        return recheck
    try:
        shutil.rmtree(paths.root)
    except OSError as exc:
        return RetentionFailure(
            run_id=record.run_id,
            error=f"could not fully remove the run directory: {exc}",
        )
    return recheck


def _refresh_latest_markers(cwd: Path, deleted: list[RetentionRecord]) -> None:
    """Point affected latest-* markers at surviving runs or drop stale ones."""
    for pipeline in sorted({record.pipeline for record in deleted if record.pipeline}):
        marker = latest_marker(cwd, pipeline)
        recent = list_runs(cwd, limit=1, pipeline=pipeline)
        if recent:
            # Different pipeline names can share a marker slug, so the pointer
            # is always recomputed from this pipeline's surviving records.
            set_latest_run(cwd, pipeline, str(recent[0]["run_id"]))
            continue
        try:
            current = marker.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not current:
            continue
        try:
            paths = run_paths(cwd, current)
        except ValueError:
            paths = None
        if paths is None or not paths.root.is_dir():
            # Dangling pointer: the pipeline has no records left. A marker that
            # still resolves a live run belongs to another pipeline sharing the
            # same slug and must survive.
            try:
                marker.unlink()
            except OSError:
                pass


def _evaluate_run(paths: Any, *, now_utc: datetime) -> RetentionRecord | RetentionSkip:
    manifest = read_json(paths.manifest)
    if manifest is None:
        return RetentionSkip(run_id=paths.run_id, pipeline=None, reason="manifest.json is missing or corrupted")
    status_payload = read_json(paths.status)
    if status_payload is None:
        return RetentionSkip(
            run_id=paths.run_id,
            pipeline=manifest.get("pipeline"),
            reason="status.json is missing or corrupted",
        )
    pipeline = status_payload.get("pipeline") or manifest.get("pipeline")

    liveness = _process_liveness(paths)
    if liveness == "running":
        # An exit code alone does not prove the owner finished writing the record.
        return RetentionSkip(run_id=paths.run_id, pipeline=pipeline, reason="the recorded process is still running")
    if liveness == "unknown":
        return RetentionSkip(run_id=paths.run_id, pipeline=pipeline, reason="process liveness could not be verified")

    status = _effective_run_status(paths, status_payload, process_dead=liveness == "dead")
    if status not in REMOVABLE_RUN_STATUSES:
        return RetentionSkip(run_id=paths.run_id, pipeline=pipeline, reason=f"run is not removable (status: {status})")

    started_at_raw = manifest.get("started_at")
    started_at = _parse_timestamp(started_at_raw)
    if started_at is None:
        return RetentionSkip(
            run_id=paths.run_id,
            pipeline=pipeline,
            reason="manifest.started_at is missing or not a valid timestamp",
        )

    return RetentionRecord(
        run_id=paths.run_id,
        pipeline=pipeline,
        status=status,
        started_at=started_at_raw if isinstance(started_at_raw, str) else None,
        age_days=(now_utc - started_at).total_seconds() / _SECONDS_PER_DAY,
        size_bytes=_directory_size(paths.root),
    )


def _process_liveness(paths: Any) -> str:
    """Classify the recorded process: ``absent``, ``running``, ``dead`` or ``unknown``."""
    pid = _read_int_value(paths.pid)
    if pid is None:
        return "absent"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "dead"
    except OSError:
        # A denied signal (PermissionError) means liveness cannot be established.
        return "unknown"
    return "running"


def _effective_run_status(paths: Any, status_payload: dict[str, Any], *, process_dead: bool) -> str:
    recorded = status_payload.get("status")
    exit_code = _read_int_value(paths.exit)
    if exit_code == 0:
        return "success"
    if exit_code is not None:
        return "cancelled" if recorded == "cancelled" else "failed"
    if process_dead:
        return "stale"
    return str(recorded or "unknown")


def _keep_last_ids(items: list[dict[str, Any]], keep_last: int | None) -> set[str]:
    """Pick the N most recent run ids of every pipeline from newest-first items."""
    if not keep_last or keep_last <= 0:
        return set()
    protected: set[str] = set()
    counts: dict[str, int] = {}
    for item in items:  # list_all_runs sorts newest first by (started_at, run_id)
        name = str(item.get("pipeline") or "unknown")
        counts[name] = counts.get(name, 0) + 1
        if counts[name] <= keep_last:
            protected.add(str(item["run_id"]))
    return protected


def _directory_size(root: Path) -> int:
    """Sum file sizes under ``root`` without following symbolic links."""
    total = 0
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            total += entry.stat(follow_symlinks=False).st_size
                        elif entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _read_int_value(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _require_int(value: int, *, minimum: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        qualifier = "a positive integer" if minimum == 1 else f"an integer of at least {minimum}"
        raise ValueError(f"{name} must be {qualifier}, got {value!r}")
