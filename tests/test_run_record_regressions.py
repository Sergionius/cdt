import json
import sys

import pytest
import typer

from cdt.pipeline.runner import run_configured_pipeline
from cdt.runs import create_run, list_runs, read_json, resolve_run, run_paths


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
