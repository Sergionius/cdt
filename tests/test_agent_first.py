import json
import os
import re
import sys
from pathlib import Path

import yaml
from typer.testing import CliRunner

from cdt.agent_release import release_status, stop_release
from cdt.cli import app
from cdt.pipeline.registry import _clear_steps_for_tests
from cdt.runs import create_run, list_runs, read_json, write_exit_code
from cdt.schema import bundled_schema_path, schema_payload

runner = CliRunner()


def setup_function():
    _clear_steps_for_tests()
    sys.modules.pop("cdt_steps.demo", None)
    sys.modules.pop("cdt_steps", None)


def teardown_function():
    _clear_steps_for_tests()
    sys.modules.pop("cdt_steps.demo", None)
    sys.modules.pop("cdt_steps", None)


def _write_project(path: Path, *, risk: str = "standard") -> None:
    package = path / "cdt_steps"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "demo.py").write_text(
        "from cdt.sdk import step\n\n@step('demo.ok')\ndef ok(ctx):\n    ctx.values['ok'] = '1'\n",
        encoding="utf-8",
    )
    config = (
        "version: 1\nplugins:\n  - cdt_steps.demo\npipelines:\n"
        f"  test:\n    risk: {risk}\n    steps:\n      - demo.ok\n"
    )
    (path / "cdt.yaml").write_text(config, encoding="utf-8")


def test_direct_run_records_status_without_requiring_run_id(tmp_path, monkeypatch):
    _write_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))

    result = runner.invoke(app, ["run", "test"])
    runs = list_runs(tmp_path)

    assert result.exit_code == 0
    assert len(runs) == 1
    assert runs[0]["status"] == "success"
    assert f"Run: {runs[0]['run_id']}" in result.output
    status = read_json(tmp_path / ".cdt" / "runs" / runs[0]["run_id"] / "status.json")
    assert status["schema_version"] == 1
    assert status["run_id"] == runs[0]["run_id"]


def test_dry_run_does_not_create_run_record(tmp_path, monkeypatch):
    _write_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))

    result = runner.invoke(app, ["run", "test", "--dry-run"])

    assert result.exit_code == 0
    assert not (tmp_path / ".cdt" / "runs").exists()


def test_production_pipeline_requires_exact_confirmation(tmp_path, monkeypatch):
    _write_project(tmp_path, risk="production")
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))

    rejected = runner.invoke(app, ["run", "test", "--confirm", "wrong"])
    accepted = runner.invoke(app, ["run", "test", "--confirm", "test"])

    assert rejected.exit_code != 0
    visible_output = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", rejected.output)
    assert "requires --confirm test" in " ".join(visible_output.split())
    assert accepted.exit_code == 0


def test_production_pipeline_can_be_confirmed_interactively(tmp_path, monkeypatch):
    _write_project(tmp_path, risk="production")
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))

    result = runner.invoke(app, ["run", "test"], input="test\n")

    assert result.exit_code == 0
    assert "Enter the pipeline name" in result.output


def test_background_start_reports_config_errors_as_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["agent-release", "start", "test", "--json"])
    payload = json.loads(result.output)

    assert result.exit_code == 1
    assert payload["status"] == "error"
    assert payload["pipeline"] == "test"
    assert "Pipeline config not found" in payload["error"]


def test_background_production_run_returns_structured_confirmation_request(tmp_path, monkeypatch):
    _write_project(tmp_path, risk="production")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["agent-release", "start", "test", "--json"])
    payload = json.loads(result.output)

    assert result.exit_code == 2
    assert payload == {
        "pipeline": "test",
        "required_confirmation": "test",
        "schema_version": 1,
        "status": "confirmation_required",
    }
    assert not (tmp_path / ".cdt" / "runs").exists()


def test_init_generates_reviewable_flutter_pipeline(tmp_path, monkeypatch):
    (tmp_path / "pubspec.yaml").write_text("name: demo\n", encoding="utf-8")
    (tmp_path / "ios").mkdir()
    (tmp_path / "android").mkdir()
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "main_test.dart").write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init"])
    config = yaml.safe_load((tmp_path / "cdt.yaml").read_text(encoding="utf-8"))

    assert result.exit_code == 0
    assert "yaml-language-server" in (tmp_path / "cdt.yaml").read_text(encoding="utf-8")
    assert config["pipelines"]["test"]["risk"] == "standard"
    assert config["pipelines"]["test"]["steps"][0] == "flutter.pub_get"
    assert "Flavor candidates: test" in result.output
    assert "production" not in config["pipelines"]
    validation = runner.invoke(app, ["pipeline", "validate", "test"])
    assert validation.exit_code == 0


def test_schema_command_exposes_pipeline_risk_and_builtin_options():
    result = runner.invoke(app, ["schema"])
    payload = json.loads(result.output)

    assert result.exit_code == 0
    assert payload["properties"]["version"]["const"] == 1
    assert payload["$defs"]["pipeline"]["properties"]["risk"]["enum"] == ["standard", "production"]
    serialized = json.dumps(payload)
    assert "ios.flutter_build_ipa" in serialized
    assert "artifact" in serialized
    assert payload == schema_payload()
    assert payload == json.loads(bundled_schema_path().read_text(encoding="utf-8"))


def test_detached_stop_refuses_to_signal_direct_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    paths = create_run(tmp_path, "test", detached=False)
    paths.pid.write_text(f"{os.getpid()}\n", encoding="utf-8")

    payload = stop_release(run_id=paths.run_id)

    assert payload["stop_result"] == "not_detached"


def test_corrupt_status_and_stale_pid_are_reported_without_crashing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    paths = create_run(tmp_path, "test")
    paths.status.write_text("{broken", encoding="utf-8")
    paths.pid.write_text("999999\n", encoding="utf-8")

    payload = release_status(run_id=paths.run_id)

    assert payload["status"] == "stale"
    assert payload["run_id"] == paths.run_id


def test_exit_code_remains_authoritative_when_status_is_corrupt(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    paths = create_run(tmp_path, "test")
    paths.status.write_text("{broken", encoding="utf-8")
    write_exit_code(paths.exit, 1)

    payload = release_status(run_id=paths.run_id)

    assert payload["status"] == "failed"
    assert payload["exit_code"] == 1


def test_run_ids_are_unique_and_history_is_machine_readable(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    first = create_run(tmp_path, "test")
    second = create_run(tmp_path, "test")
    write_exit_code(first.exit, 0)
    write_exit_code(second.exit, 1)

    result = runner.invoke(app, ["history", "--json"])
    payload = json.loads(result.output)

    assert first.run_id != second.run_id
    assert result.exit_code == 0
    assert {item["run_id"] for item in payload["runs"]} == {first.run_id, second.run_id}
    statuses = {item["run_id"]: item["status"] for item in payload["runs"]}
    assert statuses == {first.run_id: "success", second.run_id: "failed"}
