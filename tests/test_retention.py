import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cdt import retention as retention_module
from cdt.cli import app
from cdt.pipeline.registry import _clear_steps_for_tests
from cdt.retention import (
    AUTO_PRUNE_KEEP_LAST,
    AUTO_PRUNE_OLDER_THAN_DAYS,
    REMOVABLE_RUN_STATUSES,
    RetentionError,
    RetentionFailure,
    RetentionPlan,
    RetentionResult,
    auto_prune_after_run,
    build_retention_plan,
    execute_retention,
    parse_older_than_days,
    run_auto_prune,
)
from cdt.runs import (
    RUN_SCHEMA_VERSION,
    RunPaths,
    acquire_run_lock,
    create_run,
    latest_marker,
    list_all_runs,
    list_runs,
    project_lock,
    read_json,
    release_run_lock,
    resolve_run,
    run_paths,
    write_exit_code,
    write_json_atomic,
)
from cdt.user_settings import RUN_RETENTION, set_experimental_flag
from tests.test_agent_first import _write_project

NOW = datetime(2026, 10, 22, 12, 0, 0, tzinfo=timezone.utc)

FINISHED_STATUSES = {"success", "failed", "cancelled", "blocked"}


def teardown_function():
    # Keep the shared cdt_steps package namespace clean for other test modules.
    _clear_steps_for_tests()
    sys.modules.pop("cdt_steps.demo", None)
    sys.modules.pop("cdt_steps", None)


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


def execute_plan(tmp_path: Path, **filters):
    """Build a plan with the fixed NOW and execute it in the same project."""
    plan = build_retention_plan(tmp_path, now=NOW, **filters)
    return plan, execute_retention(tmp_path, plan)


def test_execute_deletes_candidates_and_moves_latest_marker(tmp_path):
    old = make_run(tmp_path, "test", age_days=40)
    fresh = make_run(tmp_path, "test", age_days=1)

    plan, result = execute_plan(tmp_path, older_than_days=30)

    assert result.schema_version == RUN_SCHEMA_VERSION
    assert result.mode == "delete"
    assert [record.run_id for record in result.deleted] == [old.run_id]
    assert result.skipped == []
    assert result.failed == []
    assert result.freed_bytes == result.deleted[0].size_bytes > 0
    assert not old.root.exists()
    assert fresh.root.exists()
    assert latest_marker(tmp_path, "test").read_text(encoding="utf-8").strip() == fresh.run_id
    assert resolve_run(tmp_path, pipeline="test") == fresh
    assert plan.candidates  # the executed plan is the one built above


def test_execute_drops_latest_marker_when_pipeline_has_no_runs_left(tmp_path):
    run = make_run(tmp_path, "test", age_days=40)

    _, result = execute_plan(tmp_path, older_than_days=30)

    assert [record.run_id for record in result.deleted] == [run.run_id]
    assert not latest_marker(tmp_path, "test").exists()
    assert resolve_run(tmp_path, pipeline="test") is None
    assert resolve_run(tmp_path) is None


def test_execute_keeps_shared_slug_marker_of_surviving_pipeline(tmp_path):
    kept = make_run(tmp_path, "test/ios", age_days=40)
    removed = make_run(tmp_path, "test-ios", age_days=40)
    # Both pipeline names share the latest-test-ios marker slug.
    assert latest_marker(tmp_path, "test/ios") == latest_marker(tmp_path, "test-ios")

    _, result = execute_plan(tmp_path, older_than_days=30, pipeline="test-ios")

    assert [record.run_id for record in result.deleted] == [removed.run_id]
    assert kept.root.exists()
    # The dangling pointer is dropped instead of stealing the marker from the
    # surviving pipeline; resolve_run falls back to the recorded history.
    assert not latest_marker(tmp_path, "test-ios").exists()
    assert resolve_run(tmp_path, pipeline="test/ios") == kept
    assert resolve_run(tmp_path, pipeline="test-ios") is None


def test_execute_skips_record_with_active_lifecycle_lock(tmp_path):
    run = make_run(tmp_path, "test", age_days=40, status="success", exit_code=0)
    owner = acquire_run_lock(run)
    try:
        _, result = execute_plan(tmp_path, older_than_days=30)
    finally:
        release_run_lock(owner)

    assert result.deleted == []
    assert result.failed == []
    assert skip_reasons(result) == {run.run_id: "run is active (lifecycle lock held by its owner)"}
    assert run.root.exists()

    _, retry = execute_plan(tmp_path, older_than_days=30)
    assert [record.run_id for record in retry.deleted] == [run.run_id]
    assert not run.root.exists()


def test_execute_rechecks_live_process_before_deletion(tmp_path):
    run = make_run(tmp_path, "test", age_days=40, status="success", exit_code=0, pid=999999)
    plan = build_retention_plan(tmp_path, now=NOW, older_than_days=30)
    assert candidate_ids(plan) == [run.run_id]
    # A live process appears after planning; the state is re-read and the
    # process re-checked immediately before deletion.
    run.pid.write_text(f"{os.getpid()}\n", encoding="utf-8")

    result = execute_retention(tmp_path, plan)

    assert result.deleted == []
    assert skip_reasons(result) == {run.run_id: "the recorded process is still running"}
    assert run.root.exists()


def test_execute_skips_when_pid_cannot_be_checked(tmp_path, monkeypatch):
    run = make_run(tmp_path, "test", age_days=40, status="success", exit_code=0, pid=12345)
    plan = build_retention_plan(tmp_path, now=NOW, older_than_days=30)
    assert candidate_ids(plan) == [run.run_id]
    monkeypatch.setattr(os, "kill", lambda pid, signal: (_ for _ in ()).throw(PermissionError))

    result = execute_retention(tmp_path, plan)

    assert result.deleted == []
    assert skip_reasons(result) == {run.run_id: "process liveness could not be verified"}
    assert run.root.exists()


def test_execute_reports_partial_failure_and_continues(tmp_path, monkeypatch):
    first = make_run(tmp_path, "alpha", age_days=40)
    second = make_run(tmp_path, "beta", age_days=40)
    real_rmtree = shutil.rmtree

    def flaky_rmtree(path, *args, **kwargs):
        if Path(path).name == first.run_id:
            (Path(path) / "manifest.json").unlink()  # partial progress
            raise OSError("disk busy")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(retention_module.shutil, "rmtree", flaky_rmtree)

    _, result = execute_plan(tmp_path, older_than_days=30)

    assert [record.run_id for record in result.deleted] == [second.run_id]
    assert result.failed == [
        RetentionFailure(run_id=first.run_id, error="could not fully remove the run directory: disk busy")
    ]
    assert first.root.exists()
    assert (first.root / "status.json").exists()
    assert not second.root.exists()


def test_execute_refuses_symlinked_runs_directory(tmp_path):
    run = make_run(tmp_path, "test", age_days=40)
    plan = build_retention_plan(tmp_path, now=NOW, older_than_days=30)
    runs_dir = tmp_path / ".cdt" / "runs"
    external = tmp_path / "outside-runs"
    shutil.move(str(runs_dir), str(external))
    runs_dir.symlink_to(external, target_is_directory=True)

    assert candidate_ids(plan) == [run.run_id]
    with pytest.raises(RetentionError, match="symbolic link"):
        execute_retention(tmp_path, plan)

    assert run.root.exists()
    assert run.root.is_dir()


def test_execute_skips_run_directory_replaced_by_symlink(tmp_path):
    run = make_run(tmp_path, "test", age_days=40)
    plan = build_retention_plan(tmp_path, now=NOW, older_than_days=30)
    external = tmp_path / "outside"
    external.mkdir()
    (external / "keep.txt").write_text("data", encoding="utf-8")
    shutil.rmtree(run.root)
    run.root.symlink_to(external, target_is_directory=True)

    result = execute_retention(tmp_path, plan)

    assert result.deleted == []
    assert skip_reasons(result) == {run.run_id: "run directory is a symlink"}
    assert (external / "keep.txt").read_text(encoding="utf-8") == "data"
    # The lifecycle lock is never created behind a symlinked run directory.
    assert not (external / ".lock").exists()


def test_execute_reports_record_missing_between_plan_and_execution(tmp_path):
    run = make_run(tmp_path, "test", age_days=40)
    plan = build_retention_plan(tmp_path, now=NOW, older_than_days=30)
    shutil.rmtree(run.root)

    result = execute_retention(tmp_path, plan)

    assert result.deleted == []
    assert result.failed == []
    assert skip_reasons(result) == {run.run_id: "run directory is no longer present"}


def test_concurrent_prune_waits_for_project_lock(tmp_path):
    run = make_run(tmp_path, "test", age_days=40)
    plan = build_retention_plan(tmp_path, now=NOW, older_than_days=30)
    guard = project_lock(tmp_path)
    guard.acquire()
    try:
        with pytest.raises(RetentionError, match="in progress"):
            execute_retention(tmp_path, plan, lock_timeout=0.05)
    finally:
        release_run_lock(guard)

    assert run.root.exists()
    result = execute_retention(tmp_path, plan)
    assert [record.run_id for record in result.deleted] == [run.run_id]


def test_execute_empty_plan_changes_nothing(tmp_path):
    fresh = make_run(tmp_path, "test", age_days=1)

    _, result = execute_plan(tmp_path, older_than_days=30)

    assert isinstance(result, RetentionResult)
    assert result.deleted == []
    assert result.skipped == []
    assert result.failed == []
    assert result.freed_bytes == 0
    assert fresh.root.exists()
    assert latest_marker(tmp_path, "test").read_text(encoding="utf-8").strip() == fresh.run_id


def test_cli_prune_confirm_updates_latest_marker_and_resolve_run(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from cdt.cli import app

    monkeypatch.chdir(tmp_path)
    old = make_run(tmp_path, "test", age_days=60)
    fresh = make_run(tmp_path, "test", age_days=1)
    assert latest_marker(tmp_path, "test").read_text(encoding="utf-8").strip() == fresh.run_id

    result = CliRunner().invoke(app, ["history", "prune", "--older-than", "30d", "--confirm", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["mode"] == "delete"
    assert [record["run_id"] for record in payload["deleted"]] == [old.run_id]
    assert not old.root.exists()
    assert fresh.root.exists()
    assert latest_marker(tmp_path, "test").read_text(encoding="utf-8").strip() == fresh.run_id
    assert resolve_run(tmp_path, pipeline="test") == fresh


def test_cli_prune_drops_latest_marker_when_pipeline_has_no_runs_left(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from cdt.cli import app

    monkeypatch.chdir(tmp_path)
    only = make_run(tmp_path, "test", age_days=60)

    result = CliRunner().invoke(app, ["history", "prune", "--older-than", "30d", "--confirm"])

    assert result.exit_code == 0
    assert not only.root.exists()
    assert not latest_marker(tmp_path, "test").exists()
    assert resolve_run(tmp_path, pipeline="test") is None


# -- Experimental auto-prune -------------------------------------------------------


def enable_auto_prune(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    set_experimental_flag(RUN_RETENTION, True)


def spy_auto_prune(monkeypatch):
    """Count run_auto_prune invocations while keeping the real behaviour."""
    calls = []
    original = retention_module.run_auto_prune

    def spy(cwd, **kwargs):
        calls.append(dict(kwargs))
        return original(cwd, **kwargs)

    monkeypatch.setattr(retention_module, "run_auto_prune", spy)
    return calls


def seed_old_runs(root: Path, pipeline: str, *, count: int, age_days: float = 40) -> list[RunPaths]:
    started = datetime.now(timezone.utc) - timedelta(days=age_days)
    return [make_run(root, pipeline, started=started) for _ in range(count)]


@pytest.mark.parametrize("linked_component", [".cdt", "runs"])
def test_retention_rejects_linked_ancestors_before_reading_or_deleting(tmp_path, monkeypatch, linked_component):
    run = make_run(tmp_path, "test", age_days=60)
    plan = build_retention_plan(tmp_path, now=NOW, older_than_days=30)
    source = tmp_path / ".cdt" if linked_component == ".cdt" else tmp_path / ".cdt" / "runs"
    external = tmp_path / "external"
    source.rename(external)
    source.symlink_to(external, target_is_directory=True)
    before = {p.relative_to(external): p.read_bytes() for p in external.rglob("*") if p.is_file()}

    with pytest.raises(RetentionError, match="symbolic link"):
        build_retention_plan(tmp_path, now=NOW, older_than_days=30)
    with pytest.raises(RetentionError, match="symbolic link"):
        execute_retention(tmp_path, plan)
    monkeypatch.chdir(tmp_path)
    for flags in (["--dry-run"], ["--confirm"]):
        result = CliRunner().invoke(app, ["history", "prune", "--older-than", "30d", "--json", *flags])
        assert result.exit_code == 1
        assert "symbolic link" in json.loads(result.stdout)["errors"][0]["error"]
    assert run.root.exists()
    assert {p.relative_to(external): p.read_bytes() for p in external.rglob("*") if p.is_file()} == before


def test_auto_prune_reports_partial_failures_on_stderr(tmp_path, monkeypatch, capsys):
    enable_auto_prune(monkeypatch, tmp_path)
    seed_old_runs(tmp_path, "test", count=AUTO_PRUNE_KEEP_LAST + 2)
    original = retention_module.shutil.rmtree
    calls = []

    def fail_first(path):
        calls.append(path)
        if len(calls) == 1:
            raise OSError("disk busy")
        return original(path)

    monkeypatch.setattr(retention_module.shutil, "rmtree", fail_first)
    result = auto_prune_after_run(tmp_path)
    assert len(result.failed) == 1
    assert len(result.deleted) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert result.failed[0].run_id in captured.err
    assert "disk busy" in captured.err
    assert "experimental run retention failed" in captured.err


def test_auto_prune_policy_is_fixed():
    assert AUTO_PRUNE_OLDER_THAN_DAYS == 30
    assert AUTO_PRUNE_KEEP_LAST == 10


def test_auto_prune_disabled_by_default_touches_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    (old,) = seed_old_runs(tmp_path, "test", count=1)

    result = run_auto_prune(tmp_path)

    assert result is None
    assert old.root.exists()


def test_auto_prune_applies_age_and_keep_last_together(tmp_path, monkeypatch):
    enable_auto_prune(monkeypatch, tmp_path)
    # The fresh run is the newest record, so it consumes one keep-last slot.
    protected = seed_old_runs(tmp_path, "test", count=AUTO_PRUNE_KEEP_LAST - 1)
    middle = make_run(tmp_path, "test", started=datetime.now(timezone.utc) - timedelta(days=41))
    oldest = make_run(tmp_path, "test", started=datetime.now(timezone.utc) - timedelta(days=45))
    fresh = make_run(tmp_path, "test", started=datetime.now(timezone.utc) - timedelta(days=1))

    result = run_auto_prune(tmp_path)

    assert isinstance(result, RetentionResult)
    assert [record.run_id for record in result.deleted] == sorted([middle.run_id, oldest.run_id])
    assert not middle.root.exists()
    assert not oldest.root.exists()
    for run in [*protected, fresh]:
        assert run.root.exists()


def test_auto_prune_keeps_newest_per_pipeline_across_pipelines(tmp_path, monkeypatch):
    enable_auto_prune(monkeypatch, tmp_path)
    alpha = seed_old_runs(tmp_path, "alpha", count=AUTO_PRUNE_KEEP_LAST + 1)
    beta = seed_old_runs(tmp_path, "beta", count=AUTO_PRUNE_KEEP_LAST + 1)

    result = run_auto_prune(tmp_path)

    # keep-last protects the newest records of every pipeline independently,
    # so exactly one old record beyond the window is deleted per pipeline.
    assert result is not None
    assert len(result.deleted) == 2
    by_pipeline: dict[str, list[str]] = {}
    for record in result.deleted:
        by_pipeline.setdefault(record.pipeline or "", []).append(record.run_id)
    assert sorted(by_pipeline) == ["alpha", "beta"]
    assert all(len(ids) == 1 for ids in by_pipeline.values())
    deleted_ids = {record.run_id for record in result.deleted}
    assert all(run.root.exists() for run in alpha + beta if run.run_id not in deleted_ids)
    assert len(list_runs(tmp_path, pipeline="alpha")) == AUTO_PRUNE_KEEP_LAST
    assert len(list_runs(tmp_path, pipeline="beta")) == AUTO_PRUNE_KEEP_LAST


def test_auto_prune_excludes_the_just_finished_run_from_its_own_pass(tmp_path, monkeypatch):
    enable_auto_prune(monkeypatch, tmp_path)
    (finished,) = seed_old_runs(tmp_path, "test", count=1)

    result = run_auto_prune(tmp_path, exclude_run_id=finished.run_id)

    # The excluded run would otherwise be the only candidate.
    assert result is None
    assert finished.root.exists()


def test_auto_prune_without_candidates_is_a_noop(tmp_path, monkeypatch):
    enable_auto_prune(monkeypatch, tmp_path)
    fresh = make_run(tmp_path, "test", started=datetime.now(timezone.utc) - timedelta(days=1))

    assert run_auto_prune(tmp_path) is None
    assert fresh.root.exists()


def test_auto_prune_is_scoped_to_the_current_project(tmp_path, monkeypatch):
    enable_auto_prune(monkeypatch, tmp_path)
    other = tmp_path / "other-project"
    other.mkdir()
    foreign = seed_old_runs(other, "test", count=AUTO_PRUNE_KEEP_LAST + 1)
    seed_old_runs(tmp_path, "test", count=AUTO_PRUNE_KEEP_LAST + 1)

    result = run_auto_prune(tmp_path)

    # Exactly one record beyond the protected window per pipeline is deleted,
    # and only in the current project; the other project stays untouched.
    assert result is not None and len(result.deleted) == 1
    assert len(list_runs(tmp_path)) == AUTO_PRUNE_KEEP_LAST
    assert len(list_runs(other)) == AUTO_PRUNE_KEEP_LAST + 1
    assert all(run.root.exists() for run in foreign)


def test_auto_prune_raises_on_unusable_project_and_helper_warns_on_stderr(tmp_path, monkeypatch, capsys):
    enable_auto_prune(monkeypatch, tmp_path)
    old = seed_old_runs(tmp_path, "test", count=AUTO_PRUNE_KEEP_LAST + 1)
    runs_root = tmp_path / ".cdt" / "runs"
    external = tmp_path / "outside-runs"
    shutil.move(str(runs_root), str(external))
    runs_root.symlink_to(external, target_is_directory=True)

    with pytest.raises(RetentionError, match="symbolic link"):
        run_auto_prune(tmp_path)

    result = auto_prune_after_run(tmp_path)

    assert result is None
    captured = capsys.readouterr()
    assert "experimental run retention failed" in captured.err
    assert captured.out == ""
    assert all(run.root.exists() for run in old)


def test_run_modes_trigger_auto_prune_only_for_the_direct_owner(tmp_path, monkeypatch):
    _write_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    enable_auto_prune(monkeypatch, tmp_path)
    calls = spy_auto_prune(monkeypatch)
    runner = CliRunner()

    # Dry-run never creates a record and never prunes.
    assert runner.invoke(app, ["run", "test", "--dry-run"]).exit_code == 0
    assert calls == []

    # The detached worker child is not the owner: its worker prunes instead.
    detached = create_run(tmp_path, "test", run_id="detached-child", detached=True)
    assert runner.invoke(app, ["run", "test", "--run-id", "detached-child"]).exit_code == 0
    assert calls == []
    assert detached.root.exists()

    # The supervised capture child is not the owner either: its supervisor prunes.
    capture = create_run(tmp_path, "test", run_id="capture-child")
    assert runner.invoke(app, ["run", "test", "--run-id", "capture-child", "--capture-child"]).exit_code == 0
    assert calls == []
    assert capture.root.exists()

    # The direct owner prunes exactly once, excluding its own record.
    assert runner.invoke(app, ["run", "test"]).exit_code == 0
    assert len(calls) == 1
    runs = list_runs(tmp_path)
    assert calls[0] == {"exclude_run_id": runs[0]["run_id"]}


def test_direct_run_auto_prune_keeps_fixed_number_of_records(tmp_path, monkeypatch):
    _write_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    enable_auto_prune(monkeypatch, tmp_path)
    seed_old_runs(tmp_path, "test", count=AUTO_PRUNE_KEEP_LAST + 3)

    result = CliRunner().invoke(app, ["run", "test"])

    assert result.exit_code == 0, result.output
    runs = list_runs(tmp_path)
    assert len(runs) == AUTO_PRUNE_KEEP_LAST
    assert runs[0]["status"] == "success"
    resolved = resolve_run(tmp_path, pipeline="test")
    assert resolved is not None and resolved.run_id == runs[0]["run_id"]


def test_retention_failure_never_changes_the_pipeline_outcome(tmp_path, monkeypatch):
    _write_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    enable_auto_prune(monkeypatch, tmp_path)
    seed_old_runs(tmp_path, "test", count=AUTO_PRUNE_KEEP_LAST + 1)
    runs_root = tmp_path / ".cdt" / "runs"
    external = tmp_path / "outside-runs"
    shutil.move(str(runs_root), str(external))
    runs_root.symlink_to(external, target_is_directory=True)

    result = CliRunner().invoke(app, ["run", "test"])

    assert result.exit_code == 0, result.output
    runs = list_runs(tmp_path)
    assert len(runs) == AUTO_PRUNE_KEEP_LAST + 2  # nothing was pruned: retention failed harmlessly
    assert runs[0]["status"] == "success"
    warnings_text = result.output
    try:
        warnings_text += result.stderr
    except ValueError:  # pragma: no cover - CliRunner mixing variants
        pass
    assert "experimental run retention failed" in warnings_text
