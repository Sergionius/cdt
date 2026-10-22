import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cdt.retention import (
    REMOVABLE_RUN_STATUSES,
    RetentionPlan,
    build_retention_plan,
    parse_older_than_days,
)
from cdt.runs import (
    RunPaths,
    create_run,
    list_all_runs,
    list_runs,
    read_json,
    run_paths,
    write_exit_code,
    write_json_atomic,
)

NOW = datetime(2026, 10, 22, 12, 0, 0, tzinfo=timezone.utc)

FINISHED_STATUSES = {"success", "failed", "cancelled", "blocked"}


def make_run(
    root: Path,
    pipeline: str,
    *,
    age_days: float | None = None,
    started: datetime | None = None,
    status: str = "success",
    exit_code: int | None = 0,
    pid: int | None = None,
) -> RunPaths:
    """Create a finished-looking run record with a controlled start time."""
    paths = create_run(root, pipeline)
    timestamp = started if started is not None else (NOW - timedelta(days=age_days) if age_days is not None else None)
    if timestamp is not None:
        manifest = read_json(paths.manifest)
        manifest["started_at"] = timestamp.isoformat()
        write_json_atomic(paths.manifest, manifest)
    payload = read_json(paths.status)
    payload["status"] = status
    if timestamp is not None:
        payload["started_at"] = timestamp.isoformat()
    if status in FINISHED_STATUSES:
        payload["finished_at"] = timestamp.isoformat() if timestamp else payload["started_at"]
    write_json_atomic(paths.status, payload)
    if exit_code is not None:
        write_exit_code(paths.exit, exit_code)
    if pid is not None:
        paths.pid.write_text(f"{pid}\n", encoding="utf-8")
    return paths


def candidate_ids(plan: RetentionPlan) -> list[str]:
    return [record.run_id for record in plan.candidates]


def kept_ids(plan: RetentionPlan) -> list[str]:
    return [entry.record.run_id for entry in plan.kept]


def skip_reasons(plan: RetentionPlan) -> dict[str, str]:
    return {entry.run_id: entry.reason for entry in plan.skipped}


def test_older_than_selects_old_finished_runs_and_keeps_recent(tmp_path):
    old = make_run(tmp_path, "test", age_days=40)
    fresh = make_run(tmp_path, "test", age_days=1)

    plan = build_retention_plan(tmp_path, now=NOW, older_than_days=30)

    assert candidate_ids(plan) == [old.run_id]
    assert kept_ids(plan) == [fresh.run_id]
    assert plan.kept[0].reason == "younger than the age limit"
    assert plan.skipped == []
    assert plan.candidates[0].status == "success"
    assert plan.candidates[0].age_days == pytest.approx(40)
    assert plan.total_size_bytes == plan.candidates[0].size_bytes > 0
    assert plan.filters == {"older_than_days": 30, "keep_last": None, "pipeline": None}
    assert plan.generated_at == NOW.isoformat()


def test_keep_last_protects_newest_run_of_each_pipeline(tmp_path):
    oldest = make_run(tmp_path, "alpha", age_days=50)
    middle = make_run(tmp_path, "alpha", age_days=40)
    newest = make_run(tmp_path, "alpha", age_days=30)
    beta = make_run(tmp_path, "beta", age_days=1)

    plan = build_retention_plan(tmp_path, now=NOW, keep_last=1)

    assert candidate_ids(plan) == sorted([oldest.run_id, middle.run_id])
    assert kept_ids(plan) == sorted([newest.run_id, beta.run_id])
    assert all(entry.reason == "protected by keep-last" for entry in plan.kept)


def test_combined_filters_intersect(tmp_path):
    young = make_run(tmp_path, "alpha", age_days=20)
    old_protected = make_run(tmp_path, "alpha", age_days=35)
    old_first = make_run(tmp_path, "alpha", age_days=40)
    old_second = make_run(tmp_path, "alpha", age_days=45)

    plan = build_retention_plan(tmp_path, now=NOW, older_than_days=30, keep_last=2)

    assert candidate_ids(plan) == sorted([old_first.run_id, old_second.run_id])
    assert kept_ids(plan) == sorted([young.run_id, old_protected.run_id])
    reasons = {entry.record.run_id: entry.reason for entry in plan.kept}
    assert reasons[young.run_id] == "younger than the age limit"
    assert reasons[old_protected.run_id] == "protected by keep-last"


def test_age_boundary_requires_strictly_older_runs(tmp_path):
    exact = make_run(tmp_path, "test", started=NOW - timedelta(days=30))
    older = make_run(tmp_path, "test", started=NOW - timedelta(days=30) - timedelta(seconds=1))

    plan = build_retention_plan(tmp_path, now=NOW, older_than_days=30)

    assert candidate_ids(plan) == [older.run_id]
    assert kept_ids(plan) == [exact.run_id]


def test_identical_start_times_break_ties_by_run_id(tmp_path):
    started = NOW - timedelta(days=40)
    first = make_run(tmp_path, "test", started=started)
    second = make_run(tmp_path, "test", started=started)
    third = make_run(tmp_path, "test", started=started)
    newest_id = max(first.run_id, second.run_id, third.run_id)

    plan = build_retention_plan(tmp_path, now=NOW, keep_last=1)

    assert kept_ids(plan) == [newest_id]
    assert candidate_ids(plan) == sorted({first.run_id, second.run_id, third.run_id} - {newest_id})


def test_keep_last_zero_protects_nothing(tmp_path):
    first = make_run(tmp_path, "test", age_days=40)
    second = make_run(tmp_path, "test", age_days=39)

    plan = build_retention_plan(tmp_path, now=NOW, keep_last=0)

    assert candidate_ids(plan) == sorted([first.run_id, second.run_id])
    assert plan.kept == []


@pytest.mark.parametrize(
    ("status", "exit_code"),
    [("success", 0), ("failed", 1), ("cancelled", 3), ("blocked", None)],
)
def test_finished_runs_are_candidates_regardless_of_outcome(tmp_path, status, exit_code):
    run = make_run(tmp_path, "test", age_days=40, status=status, exit_code=exit_code)

    plan = build_retention_plan(tmp_path, now=NOW, older_than_days=30)

    assert candidate_ids(plan) == [run.run_id]
    assert plan.candidates[0].status == status


def test_queued_and_running_runs_are_skipped(tmp_path):
    queued = make_run(tmp_path, "test", age_days=40, status="queued", exit_code=None)
    running = make_run(tmp_path, "test", age_days=40, status="running", exit_code=None, pid=os.getpid())

    plan = build_retention_plan(tmp_path, now=NOW, older_than_days=30)

    assert candidate_ids(plan) == []
    assert kept_ids(plan) == []
    reasons = skip_reasons(plan)
    assert reasons[queued.run_id] == "run is not removable (status: queued)"
    assert reasons[running.run_id] == "the recorded process is still running"


def test_exit_code_alone_does_not_authorize_deletion_while_process_alive(tmp_path):
    run = make_run(tmp_path, "test", age_days=40, status="success", exit_code=0, pid=os.getpid())

    plan = build_retention_plan(tmp_path, now=NOW, older_than_days=30)

    assert candidate_ids(plan) == []
    assert skip_reasons(plan) == {run.run_id: "the recorded process is still running"}


def test_stale_run_with_confirmed_dead_process_is_candidate(tmp_path):
    run = make_run(tmp_path, "test", age_days=40, status="running", exit_code=None, pid=999999)

    plan = build_retention_plan(tmp_path, now=NOW, older_than_days=30)

    assert candidate_ids(plan) == [run.run_id]
    assert plan.candidates[0].status == "stale"


def test_unverifiable_process_is_skipped(tmp_path, monkeypatch):
    run = make_run(tmp_path, "test", age_days=40, status="success", exit_code=0, pid=os.getpid())

    def denied(pid, signal):
        raise PermissionError

    monkeypatch.setattr(os, "kill", denied)
    plan = build_retention_plan(tmp_path, now=NOW, older_than_days=30)

    assert candidate_ids(plan) == []
    assert skip_reasons(plan) == {run.run_id: "process liveness could not be verified"}


@pytest.mark.parametrize(
    ("corruption", "reason_part"),
    [
        ("manifest-bytes", "manifest.json"),
        ("manifest-missing", "manifest.json"),
        ("status-bytes", "status.json"),
        ("status-non-dict", "status.json"),
        ("started-at-invalid", "started_at"),
    ],
)
def test_corrupted_records_are_skipped_with_reason(tmp_path, corruption, reason_part):
    run = make_run(tmp_path, "test", age_days=40)
    if corruption == "manifest-bytes":
        run.manifest.write_bytes(b"\xff")
    elif corruption == "manifest-missing":
        run.manifest.unlink()
    elif corruption == "status-bytes":
        run.status.write_bytes(b"\xff")
    elif corruption == "status-non-dict":
        run.status.write_text("[]\n", encoding="utf-8")
    elif corruption == "started-at-invalid":
        manifest = read_json(run.manifest)
        manifest["started_at"] = "not-a-date"
        write_json_atomic(run.manifest, manifest)

    plan = build_retention_plan(tmp_path, now=NOW, older_than_days=30)

    assert candidate_ids(plan) == []
    reasons = skip_reasons(plan)
    assert run.run_id in reasons
    assert reason_part in reasons[run.run_id]


def test_symlinked_run_directory_is_skipped(tmp_path):
    external = tmp_path / "outside"
    external.mkdir()
    target = external / "data.bin"
    target.write_bytes(b"x" * 4096)
    run = make_run(tmp_path, "test", age_days=40)
    shutil.rmtree(run.root)
    run.root.symlink_to(external, target_is_directory=True)

    plan = build_retention_plan(tmp_path, now=NOW, older_than_days=30)

    assert candidate_ids(plan) == []
    assert skip_reasons(plan) == {run.run_id: "run directory is a symlink"}
    assert target.read_bytes() == b"x" * 4096
    assert run.root.is_symlink()


def test_size_accounting_does_not_follow_symlinks_inside_run(tmp_path):
    external = tmp_path / "outside.bin"
    external.write_bytes(b"y" * 10000)
    run = make_run(tmp_path, "test", age_days=40)
    (run.root / "external.lnk").symlink_to(external)

    plan = build_retention_plan(tmp_path, now=NOW, older_than_days=30)

    assert candidate_ids(plan) == [run.run_id]
    assert 0 < plan.candidates[0].size_bytes < 10000
    assert external.read_bytes() == b"y" * 10000


def test_plan_building_does_not_modify_any_files(tmp_path):
    make_run(tmp_path, "alpha", age_days=40)
    make_run(tmp_path, "alpha", age_days=5, status="running", exit_code=None, pid=os.getpid())
    make_run(tmp_path, "beta", age_days=40, status="failed", exit_code=1)

    def snapshot():
        return sorted(
            (str(path.relative_to(tmp_path)), path.lstat().st_size, path.lstat().st_mtime_ns)
            for path in tmp_path.rglob("*")
            if path.is_file() or path.is_symlink()
        )

    before = snapshot()
    plan = build_retention_plan(tmp_path, now=NOW, older_than_days=30, keep_last=1)
    after = snapshot()

    assert before == after
    assert plan.candidates


def test_pipeline_filter_limits_plan_scope(tmp_path):
    alpha = make_run(tmp_path, "alpha", age_days=40)
    make_run(tmp_path, "beta", age_days=40)

    plan = build_retention_plan(tmp_path, now=NOW, older_than_days=30, pipeline="alpha")

    assert candidate_ids(plan) == [alpha.run_id]
    assert kept_ids(plan) == []
    assert skip_reasons(plan) == {}
    assert plan.filters["pipeline"] == "alpha"


def test_empty_project_yields_empty_plan(tmp_path):
    plan = build_retention_plan(tmp_path, now=NOW, older_than_days=30)

    assert isinstance(plan, RetentionPlan)
    assert plan.candidates == []
    assert plan.kept == []
    assert plan.skipped == []
    assert plan.total_size_bytes == 0


def test_plan_requires_at_least_one_filter(tmp_path):
    with pytest.raises(ValueError, match="age or count limit"):
        build_retention_plan(tmp_path, now=NOW)


@pytest.mark.parametrize(
    ("older_than_days", "keep_last"),
    [(0, None), (-5, None), (None, -1), ("30", None)],
)
def test_invalid_filter_values_are_rejected(tmp_path, older_than_days, keep_last):
    with pytest.raises(ValueError):
        build_retention_plan(tmp_path, now=NOW, older_than_days=older_than_days, keep_last=keep_last)


@pytest.mark.parametrize(("value", "expected"), [("7d", 7), ("7D", 7), (" 7 ", 7), (7, 7)])
def test_parse_older_than_days_accepts_day_counts(value, expected):
    assert parse_older_than_days(value) == expected


@pytest.mark.parametrize("value", ["0d", "-3", "abc", "", "1w", "d7"])
def test_parse_older_than_days_rejects_invalid_values(value):
    with pytest.raises(ValueError, match="positive integer number of days"):
        parse_older_than_days(value)


def test_list_all_runs_returns_full_history_without_limit(tmp_path):
    for _ in range(25):
        create_run(tmp_path, "test")

    everything = list_all_runs(tmp_path)
    bounded = list_runs(tmp_path)

    assert len(everything) == 25
    assert len(bounded) == 20  # default history limit stays intact
    assert set(everything[0]) == set(bounded[0])

    def ordering(item):
        return (str(item["started_at"] or ""), item["run_id"])

    assert everything == sorted(everything, key=ordering, reverse=True)


def test_removable_statuses_cover_terminal_and_stale():
    assert REMOVABLE_RUN_STATUSES == {"success", "failed", "cancelled", "blocked", "stale"}


def test_run_paths_round_trip_for_plan_entries(tmp_path):
    run = make_run(tmp_path, "test", age_days=40)
    plan = build_retention_plan(tmp_path, now=NOW, older_than_days=30)

    assert candidate_ids(plan) == [run.run_id]
    assert run_paths(tmp_path, plan.candidates[0].run_id).root == run.root
