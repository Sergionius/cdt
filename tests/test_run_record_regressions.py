import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import typer

import cdt.pipeline.runner as runner_module
import cdt.runs as runs_module
from cdt.pipeline.runner import run_configured_pipeline
from cdt.retention import build_retention_plan, execute_retention
from cdt.runs import acquire_run_lock, create_run, list_runs, read_json, release_run_lock, resolve_run, run_paths


def test_generated_run_id_collision_retries_without_overwriting(tmp_path, monkeypatch):
    ids = iter(["duplicate", "duplicate", "replacement"])
    monkeypatch.setattr(runs_module, "generate_run_id", lambda pipeline: next(ids))

    first = create_run(tmp_path, "test")
    second = create_run(tmp_path, "test")

    assert (first.run_id, second.run_id) == ("duplicate", "replacement")
    assert read_json(first.manifest)["run_id"] == "duplicate"
    assert read_json(second.manifest)["run_id"] == "replacement"
    assert resolve_run(tmp_path, pipeline="test") == second


def test_explicit_run_id_collision_is_not_retried(tmp_path, monkeypatch):
    original = create_run(tmp_path, "test", run_id="reserved")
    monkeypatch.setattr(runs_module, "generate_run_id", lambda pipeline: pytest.fail("unexpected ID generation"))

    with pytest.raises(FileExistsError):
        create_run(tmp_path, "test", run_id="reserved")

    assert read_json(original.manifest)["run_id"] == "reserved"


def test_pipeline_lookup_does_not_follow_colliding_latest_marker(tmp_path):
    wanted = create_run(tmp_path, "test/ios")
    other = create_run(tmp_path, "test-ios")

    assert resolve_run(tmp_path, pipeline="test/ios") == wanted
    assert resolve_run(tmp_path, pipeline="test-ios") == other
    assert resolve_run(tmp_path, pipeline="test ios") is None


def test_history_tolerates_non_utf8_run_metadata(tmp_path):
    paths = create_run(tmp_path, "test")
    paths.status.write_bytes(b"\xff")
    paths.manifest.write_bytes(b"\xff")

    assert read_json(paths.status) is None
    assert list_runs(tmp_path)[0]["status"] == "unknown"


@pytest.mark.parametrize("prior", [None, "missing.json", "invalid.json"])
def test_rejected_resume_has_terminal_run_record(tmp_path, prior):
    (tmp_path / "cdt.yaml").write_text("version: 1\npipelines:\n  test:\n    steps: []\n")
    (tmp_path / "invalid.json").write_text(json.dumps({"completed_steps": "invalid"}))
    stdout, stderr = sys.stdout, sys.stderr

    with pytest.raises(typer.BadParameter):
        run_configured_pipeline(
            tmp_path, {}, "test", skip_completed=True,
            resume_status_file=tmp_path / prior if prior else None,
        )

    records = list_runs(tmp_path)
    assert len(records) == 1
    assert records[0]["status"] == "failed"
    paths = run_paths(tmp_path, records[0]["run_id"])
    status = read_json(paths.status)
    assert status["status"] == "failed"
    assert status["error"]
    assert status["finished_at"]
    assert status["failed_step"] is None
    assert paths.exit.read_text() == "1\n"
    assert "CDT run failed" in paths.log.read_text()
    assert sys.stdout is stdout
    assert sys.stderr is stderr


def test_direct_run_owns_lifecycle_lock_until_record_is_final(tmp_path, monkeypatch):
    (tmp_path / "cdt.yaml").write_text("version: 1\npipelines:\n  test:\n    steps: []\n")
    # The recorded process is long gone: only the owner's lifecycle lock can
    # still protect the terminal-looking record from a concurrent prune.
    monkeypatch.setattr(os, "kill", lambda pid, signal: (_ for _ in ()).throw(ProcessLookupError))

    observed = {}
    original_release = runner_module.release_run_lock

    def spy_release(lock):
        if lock is not None and "run_id" not in observed:
            lock_file = Path(lock.lock_file)
            observed["run_id"] = lock_file.parent.name
            observed["lock_file"] = lock_file
            plan = build_retention_plan(tmp_path, now=datetime.now(timezone.utc), keep_last=0)
            result = execute_retention(tmp_path, plan)
            observed["deleted"] = [record.run_id for record in result.deleted]
            observed["skipped"] = {skip.run_id: skip.reason for skip in result.skipped}
        original_release(lock)

    monkeypatch.setattr(runner_module, "release_run_lock", spy_release)

    completed = run_configured_pipeline(tmp_path, {}, "test")

    assert completed == observed["run_id"]
    assert observed["lock_file"] == run_paths(tmp_path, completed).lock
    assert observed["lock_file"].is_file()
    assert observed["deleted"] == []
    assert observed["skipped"] == {completed: "run is active (lifecycle lock held by its owner)"}

    # The owner released the lock after finalizing the record.
    paths = run_paths(tmp_path, completed)
    owner = acquire_run_lock(paths)
    assert owner is not None
    release_run_lock(owner)

    plan = build_retention_plan(tmp_path, now=datetime.now(timezone.utc), keep_last=0)
    result = execute_retention(tmp_path, plan)
    assert [record.run_id for record in result.deleted] == [completed]
    assert not paths.root.exists()
    assert resolve_run(tmp_path, run_id=completed) is None


def test_detached_worker_child_does_not_reacquire_the_owner_lock(tmp_path, monkeypatch):
    (tmp_path / "cdt.yaml").write_text("version: 1\npipelines:\n  test:\n    steps: []\n")
    supervisor = create_run(tmp_path, "test", run_id="detached-run", detached=True)
    acquired = []
    original_acquire = runner_module.acquire_run_lock

    def spy_acquire(paths, **kwargs):
        acquired.append(paths.run_id)
        return original_acquire(paths, **kwargs)

    monkeypatch.setattr(runner_module, "acquire_run_lock", spy_acquire)

    run_configured_pipeline(tmp_path, {}, "test", run_id="detached-run", detached=True)

    assert acquired == []  # the owner is the detached worker, not its child
    assert not supervisor.lock.exists()
