"""Selection of recorded runs for history retention.

This module builds a deterministic deletion plan without modifying any
files: the actual removal is a separate step. Only finished runs may
become candidates; active, unverifiable and corrupted records are always
skipped with an explicit reason.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .runs import RUN_SCHEMA_VERSION, list_all_runs, read_json, run_paths

TERMINAL_RUN_STATUSES = frozenset({"success", "failed", "cancelled", "blocked"})
# A run whose recorded process is confirmed dead ("stale") can no longer make
# progress, so it is removable as well.
REMOVABLE_RUN_STATUSES = TERMINAL_RUN_STATUSES | {"stale"}

_SECONDS_PER_DAY = 86400

_KEPT_REASON_YOUNG = "younger than the age limit"
_KEPT_REASON_PROTECTED = "protected by keep-last"


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
