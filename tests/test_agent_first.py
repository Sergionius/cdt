import json
import os
import re
import subprocess
import sys
import time
import urllib.error
from pathlib import Path
from typing import Any

import yaml
from typer.testing import CliRunner

import cdt.services.appstore as appstore_service
import cdt.steps.google_play as google_play_step
from cdt.agent_release import release_status, stop_release
from cdt.cli import app
from cdt.pipeline.builtins import register_builtin_steps
from cdt.pipeline.registry import _clear_steps_for_tests, list_step_metadata
from cdt.runs import create_run, list_runs, read_json, run_paths, write_exit_code
from cdt.schema import bundled_schema_path, schema_payload
from cdt.services.appstore_state import load_upload_record, save_upload_record
from cdt.services.google_play_state import PublishOutcome
from tests.test_services_appstore_state import FakeAsc, _stub_client

runner = CliRunner()
ROOT = Path(__file__).resolve().parents[1]
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


def test_skipped_production_leaf_keeps_risk_validation_and_confirmation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "cdt.yaml"
    text = (
        "version: 1\npipelines:\n  demo:\n    inputs: {deploy: {}}\n    steps:\n"
        "      - parallel:\n          steps:\n            - sequence:\n                steps:\n"
        "                  - step: appstore.submit_review\n"
        "                    when: {input: deploy, present: true}\n"
    )
    config.write_text(text)
    invalid = runner.invoke(app, ["pipeline", "plan", "demo", "--json", "--input", "deploy="])
    assert invalid.exit_code != 0
    assert any(e["code"] == "production_risk_required" for e in json.loads(invalid.output)["errors"])
    config.write_text(text.replace("    inputs:", "    risk: production\n    inputs:"))
    rejected = runner.invoke(app, ["run", "demo", "--confirm", "wrong"])
    assert rejected.exit_code != 0
    assert not (tmp_path / ".cdt").exists()
    accepted = runner.invoke(app, ["run", "demo", "--confirm", "demo"])
    assert accepted.exit_code == 0, accepted.output


def test_extended_schema_is_unambiguous_and_bundled():
    schema = schema_payload()
    assert json.loads(bundled_schema_path().read_text()) == schema
    extended = schema["$defs"]["extendedStep"]
    assert extended["required"] == ["step"]
    assert not extended["additionalProperties"]
    assert extended["properties"]["retry"] == {"$ref": "#/$defs/retryPolicy"}
    retry = schema["$defs"]["retryPolicy"]
    assert retry["additionalProperties"] is False
    assert retry["properties"]["max_attempts"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 5,
        "default": 1,
    }
    assert retry["properties"]["delay_seconds"] == {
        "type": "number",
        "minimum": 0,
        "maximum": 60,
        "default": 0,
    }
    plugin = next(item for item in schema["$defs"]["step"]["oneOf"] if item.get("description") == "Project plugin step")
    assert re.fullmatch(plugin["propertyNames"]["pattern"], "step") is None
    assert re.fullmatch(plugin["propertyNames"]["pattern"], "retry") is None
    assert schema["$defs"]["condition"]["oneOf"] == [
        {"required": ["equals"]},
        {"required": ["not_equals"]},
        {"required": ["present"]},
    ]


def test_builtin_steps_do_not_declare_automatic_retries():
    from cdt.pipeline.builtins import _BUILTIN_METADATA

    retriable = [name for name, metadata in _BUILTIN_METADATA.items() if metadata.retry_safe]
    assert retriable == []


def test_plan_and_inspect_expose_retry_policy_and_capability(tmp_path, monkeypatch):
    package = tmp_path / "cdt_steps"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "demo.py").write_text(
        "from cdt.sdk import step\n\n@step('demo.flaky', retry_safe=True)\ndef flaky(ctx):\n    pass\n",
        encoding="utf-8",
    )
    (tmp_path / "cdt.yaml").write_text(
        "version: 1\nplugins:\n  - cdt_steps.demo\npipelines:\n  test:\n    steps:\n"
        "      - step: demo.flaky\n        retry: {max_attempts: 3, delay_seconds: 2}\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))

    plan = json.loads(runner.invoke(app, ["pipeline", "plan", "test", "--json"]).output)
    node = plan["steps"][0]
    assert node["retry"] == {"max_attempts": 3, "delay_seconds": 2}
    assert node["metadata"]["retry_safe"] is True

    inspect = json.loads(runner.invoke(app, ["pipeline", "inspect", "test", "--json"]).output)
    assert inspect["steps"][0]["retry"] == {"max_attempts": 3, "delay_seconds": 2}


def test_plan_flags_retry_without_capability(tmp_path, monkeypatch):
    package = tmp_path / "cdt_steps"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "demo.py").write_text(
        "from cdt.sdk import step\n\n@step('demo.risky')\ndef risky(ctx):\n    pass\n",
        encoding="utf-8",
    )
    (tmp_path / "cdt.yaml").write_text(
        "version: 1\nplugins:\n  - cdt_steps.demo\npipelines:\n  test:\n    steps:\n"
        "      - step: demo.risky\n        retry: {max_attempts: 2}\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))

    result = runner.invoke(app, ["pipeline", "plan", "test", "--json"])

    assert result.exit_code != 0
    payload = json.loads(result.output)
    assert payload["errors"][0]["code"] == "retry_requires_capability"


def setup_function():
    _clear_steps_for_tests()
    sys.modules.pop("cdt_steps.demo", None)
    sys.modules.pop("cdt_steps.play", None)
    sys.modules.pop("cdt_steps", None)


def teardown_function():
    _clear_steps_for_tests()
    sys.modules.pop("cdt_steps.demo", None)
    sys.modules.pop("cdt_steps.play", None)
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


# -- Google Play production confirmation gates -------------------------------------


def _write_play_project(path: Path) -> None:
    """Production pipeline that registers an AAB and uploads it to Google Play."""
    package = path / "cdt_steps"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "play.py").write_text(
        "\n".join(
            [
                "from cdt.artifacts import ArtifactKind, BuildArtifact",
                "from cdt.sdk import step",
                "",
                "@step('demo.aab')",
                "def make_aab(ctx, output: str):",
                "    aab = ctx.cwd / output",
                "    aab.write_bytes(b'android-app-bundle-bytes')",
                "    ctx.register_artifact('aab', BuildArtifact(ArtifactKind.AAB, aab, 'Android AAB'))",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (path / "cdt.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "plugins:",
                "  - cdt_steps.play",
                "pipelines:",
                "  play:",
                "    risk: production",
                "    steps:",
                "      - demo.aab: {output: app-release.aab}",
                "      - google_play.upload_aab:",
                "          artifact: aab",
                "          package_name: com.example.app",
                "          track: internal",
                "          release_status: completed",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _patch_publishing_api(
    monkeypatch: Any,
    *,
    outcome: PublishOutcome | None = None,
    error: Exception | None = None,
    captured: dict[str, Any] | None = None,
) -> dict[str, int]:
    """Stub the Android Publisher client and operation, recording each construction."""
    calls = {"client": 0, "operation": 0}
    captured = captured if captured is not None else {}

    def fake_client(env: dict[str, str], cwd: Path):
        calls["client"] += 1
        captured["env"] = env
        captured["cwd"] = cwd
        return ("fake-play-client",)

    class FakeOperation:
        def __init__(self, client: Any, cwd: Path, intent: Any, aab_path: Path):
            calls["operation"] += 1
            captured["intent"] = intent
            captured["aab_path"] = aab_path

        def run(self) -> PublishOutcome:
            if error is not None:
                raise error
            assert outcome is not None
            return outcome

    monkeypatch.setattr(google_play_step, "GooglePlayClient", fake_client)
    monkeypatch.setattr(google_play_step, "GooglePlayPublishOperation", FakeOperation)
    return calls


def _confirmed_outcome() -> PublishOutcome:
    return PublishOutcome(
        operation_id="op",
        package_name="com.example.app",
        track="internal",
        release_status="completed",
        version_code=40,
        aab_sha256="a" * 64,
        changes_sent_for_review=True,
    )


def test_play_run_without_or_wrong_confirmation_never_calls_publishing_api(tmp_path, monkeypatch):
    _write_play_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    calls = _patch_publishing_api(monkeypatch)

    missing = runner.invoke(app, ["run", "play"])
    wrong = runner.invoke(app, ["run", "play", "--confirm", "wrong"])

    # Without --confirm the interactive prompt is offered and never satisfied;
    # with a wrong value the exact-confirmation requirement fails the run.
    missing_visible = " ".join(re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", missing.output).split())
    assert "Enter the pipeline name to continue" in missing_visible
    wrong_visible = " ".join(re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", wrong.output).split())
    assert "requires --confirm play" in wrong_visible
    for rejected in (missing, wrong):
        assert rejected.exit_code != 0
    assert calls == {"client": 0, "operation": 0}
    assert not (tmp_path / ".cdt" / "runs").exists()
    assert not (tmp_path / ".cdt" / "google-play").exists()


def test_play_run_with_exact_confirmation_allows_the_publishing_step(tmp_path, monkeypatch):
    _write_play_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    captured: dict[str, Any] = {}
    calls = _patch_publishing_api(monkeypatch, outcome=_confirmed_outcome(), captured=captured)

    result = runner.invoke(app, ["run", "play", "--confirm", "play"])

    assert result.exit_code == 0, result.output
    assert calls["client"] == 1
    assert calls["operation"] == 1
    assert "commit: confirmed" in result.output
    assert captured["intent"].package_name == "com.example.app"
    assert captured["intent"].track == "internal"
    assert captured["intent"].release_status == "completed"
    runs = list_runs(tmp_path)
    assert len(runs) == 1
    assert read_json(run_paths(tmp_path, runs[0]["run_id"]).status)["status"] == "success"


def test_detached_play_start_without_exact_confirmation_requests_it_before_any_run(tmp_path, monkeypatch):
    _write_play_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    calls = _patch_publishing_api(monkeypatch)

    missing = runner.invoke(app, ["agent-release", "start", "play", "--json"])
    wrong = runner.invoke(app, ["agent-release", "start", "play", "--confirm", "wrong", "--json"])

    for rejected in (missing, wrong):
        payload = json.loads(rejected.output)
        assert rejected.exit_code == 2
        assert payload["status"] == "confirmation_required"
        assert payload["required_confirmation"] == "play"
    assert calls == {"client": 0, "operation": 0}
    assert not (tmp_path / ".cdt" / "runs").exists()
    assert not (tmp_path / ".cdt" / "google-play").exists()


def test_detached_play_start_with_exact_confirmation_runs_the_step_offline_of_credentials(tmp_path, monkeypatch):
    _write_play_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    # The ADC file is missing, so the detached worker can only fail at credential
    # loading: reaching that failure proves the step was allowed to run while the
    # publishing API stays unreachable without real credentials.
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(tmp_path / "missing-adc.json"))

    started = runner.invoke(app, ["agent-release", "start", "play", "--confirm", "play", "--json"])

    payload = json.loads(started.output)
    assert started.exit_code == 0, started.output
    run_id = payload["run_id"]
    paths = run_paths(tmp_path, run_id)
    deadline = time.time() + 60
    while not paths.exit.exists() and time.time() < deadline:
        time.sleep(0.1)
    assert paths.exit.exists(), "detached Google Play worker did not finish"

    status = read_json(paths.status)
    assert status["status"] == "failed"
    assert "GOOGLE_APPLICATION_CREDENTIALS file not found" in status["error"]
    # The publication never got far enough to open an edit or save a checkpoint.
    assert not (tmp_path / ".cdt" / "google-play" / "operations").exists()


# -- appstore.submit_review: exact confirmation and offline planning -------------


def _write_submit_project(path: Path) -> None:
    (path / "cdt.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "pipelines:",
                "  submit:",
                "    risk: production",
                "    inputs:",
                "      whats_new:",
                "        required: true",
                "    steps:",
                "      - appstore.submit_review:",
                "          whats_new:",
                '            ru: "${inputs.whats_new}"',
                "          release_mode: manual",
                "          phased_release: true",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (path / ".env").write_text("IOS_BUNDLE_ID=com.example.app\n", encoding="utf-8")


def test_submit_run_without_or_wrong_confirmation_never_contacts_apple(tmp_path, monkeypatch):
    _write_submit_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    def fail(method, path, client, payload=None, retry_ambiguous=True):
        raise AssertionError("Apple must not be contacted before the exact confirmation")

    monkeypatch.setattr(appstore_service, "_asc_request", fail)

    missing = runner.invoke(app, ["run", "submit", "--input", "whats_new=Исправления"])
    wrong = runner.invoke(app, ["run", "submit", "--input", "whats_new=Исправления", "--confirm", "wrong"])

    # Without --confirm the interactive prompt is offered and never satisfied;
    # with a wrong value the exact-confirmation requirement fails the run.
    assert missing.exit_code != 0
    assert "Enter the pipeline name to continue" in missing.output
    assert wrong.exit_code != 0
    wrong_visible = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", wrong.output).replace("│", " ")
    assert "requires --confirm submit" in " ".join(wrong_visible.split())
    assert not (tmp_path / ".cdt" / "appstore").exists()
    assert not (tmp_path / ".cdt" / "runs").exists()


def test_submit_run_with_exact_confirmation_submits_with_interpolated_whats_new(tmp_path, monkeypatch):
    _write_submit_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    # A previous full TestFlight completion recorded the build for this app;
    # the standalone submit pipeline must use it without rebuilding or uploading.
    save_upload_record(tmp_path, "com.example.app", "1.2.3+5")
    FakeAsc(monkeypatch)
    _stub_client(monkeypatch)

    result = runner.invoke(
        app,
        ["run", "submit", "--input", "whats_new=Исправления и улучшения", "--confirm", "submit"],
    )

    assert result.exit_code == 0, result.output
    assert "Submitted for App Store review" in result.output
    assert "does NOT mean approval or user availability" in result.output
    runs = list_runs(tmp_path)
    assert len(runs) == 1
    status = read_json(run_paths(tmp_path, runs[0]["run_id"]).status)
    assert status["status"] == "success"
    release_results = dict(status["release_results"])
    submission_id = release_results.pop("appstore_review_submission_id")
    assert submission_id
    assert release_results == {
        "appstore_review_bundle_id": "com.example.app",
        "appstore_review_marketing_version": "1.2.3",
        "appstore_review_build_number": "5",
        "appstore_review_submission_state": "WAITING_FOR_REVIEW",
        "appstore_review_release_mode": "manual",
        "appstore_review_phased_release": "true",
    }
    # The localized text was interpolated from the pipeline input into the intent.
    checkpoints = list((tmp_path / ".cdt" / "appstore" / "operations").glob("*.json"))
    assert len(checkpoints) == 1
    checkpoint = json.loads(checkpoints[0].read_text(encoding="utf-8"))
    assert checkpoint["whats_new"] == {"ru": "Исправления и улучшения"}
    assert checkpoint["submission_id"] == submission_id
    assert checkpoint["phase"] == "confirmed"


def test_standalone_submit_without_record_fails_without_scanning_apple(tmp_path, monkeypatch):
    _write_submit_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    asc = FakeAsc(monkeypatch)
    _stub_client(monkeypatch)

    result = runner.invoke(app, ["run", "submit", "--input", "whats_new=Исправления", "--confirm", "submit"])

    assert result.exit_code != 0
    assert "No recorded TestFlight build" in result.output
    assert "appstore.upload_testflight" in result.output
    assert "does not pick an arbitrary" in result.output
    # Apple was never contacted: no app lookup and no unfiltered latest-build
    # scan — the refusal happened entirely from the missing local record.
    assert asc.calls == []
    assert not (tmp_path / ".cdt" / "appstore").exists()


def test_detached_submit_start_without_exact_confirmation_requests_it_before_any_run(tmp_path, monkeypatch):
    _write_submit_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    def fail(method, path, client, payload=None, retry_ambiguous=True):
        raise AssertionError("Apple must not be contacted before the exact confirmation")

    monkeypatch.setattr(appstore_service, "_asc_request", fail)

    missing = runner.invoke(app, ["agent-release", "start", "submit", "--input", "whats_new=Исправления", "--json"])
    wrong = runner.invoke(
        app,
        ["agent-release", "start", "submit", "--input", "whats_new=Исправления", "--confirm", "wrong", "--json"],
    )

    for rejected in (missing, wrong):
        payload = json.loads(rejected.output)
        assert rejected.exit_code == 2
        assert payload["status"] == "confirmation_required"
        assert payload["required_confirmation"] == "submit"
    assert not (tmp_path / ".cdt" / "runs").exists()
    assert not (tmp_path / ".cdt" / "appstore").exists()


def test_detached_submit_start_with_exact_confirmation_runs_offline_of_apple(tmp_path, monkeypatch):
    _write_submit_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    save_upload_record(tmp_path, "com.example.app", "1.2.3+5")

    started = runner.invoke(
        app,
        ["agent-release", "start", "submit", "--input", "whats_new=Исправления", "--confirm", "submit", "--json"],
    )

    payload = json.loads(started.output)
    assert started.exit_code == 0, started.output
    run_id = payload["run_id"]
    paths = run_paths(tmp_path, run_id)
    deadline = time.time() + 60
    while not paths.exit.exists() and time.time() < deadline:
        time.sleep(0.1)
    assert paths.exit.exists(), "detached submit worker did not finish"

    status = read_json(paths.status)
    # The worker runs without ASC credentials, so the submission fails at the
    # credential boundary: reaching it proves the step was allowed to run while
    # Apple stays unreachable without real credentials.
    assert status["status"] == "failed"
    assert "Missing ASC credentials" in status["error"]
    # The submission never got far enough to save a review checkpoint.
    assert not (tmp_path / ".cdt" / "appstore" / "operations").exists()


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
    assert "appstore.upload_testflight_ipa" in serialized
    assert "appstore.complete_testflight" in serialized
    assert "artifact" in serialized
    assert payload == schema_payload()
    assert payload == json.loads(bundled_schema_path().read_text(encoding="utf-8"))


def test_schema_exposes_split_testflight_step_options():
    payload = schema_payload()
    step_schemas = [obj for obj in payload["$defs"]["step"]["oneOf"] if isinstance(obj, dict) and obj.get("properties")]
    options_by_name = {next(iter(obj["properties"])): next(iter(obj["properties"].values())) for obj in step_schemas}

    upload_options = options_by_name["appstore.upload_testflight_ipa"]
    assert sorted(upload_options["properties"]) == ["artifact"]

    complete_options = options_by_name["appstore.complete_testflight"]
    assert sorted(complete_options["properties"]) == ["changelog"]


def test_schema_exposes_python_release_and_git_release_step_options():
    payload = schema_payload()
    step_schemas = [obj for obj in payload["$defs"]["step"]["oneOf"] if isinstance(obj, dict) and obj.get("properties")]
    options_by_name = {next(iter(obj["properties"])): next(iter(obj["properties"].values())) for obj in step_schemas}

    prepare_options = options_by_name["python.prepare_release"]
    assert sorted(prepare_options["properties"]) == [
        "changelog",
        "pyproject",
        "tag_reference_files",
        "tag_reference_regex",
        "version",
        "version_file",
    ]

    build_options = options_by_name["python.build_distribution"]
    assert sorted(build_options["properties"]) == ["dist_dir", "python", "sdist_artifact", "wheel_artifact"]

    commit_options = options_by_name["git.release_commit"]
    assert commit_options["required"] == ["files"]
    assert sorted(commit_options["properties"]) == ["files", "message"]

    tag_push_options = options_by_name["git.release_tag_push"]
    assert sorted(tag_push_options["properties"]) == ["branch", "message", "remote", "tag"]

    sync_options = options_by_name["git.require_synced_main"]
    assert sorted(sync_options["properties"]) == ["branch", "remote"]

    available_options = options_by_name["release.require_version_available"]
    assert sorted(available_options["properties"]) == [
        "changelog",
        "github_repo",
        "pypi_package",
        "pyproject",
        "remote",
        "tag_prefix",
        "version",
    ]

    ruff_options = options_by_name["python.ruff_check"]
    assert ruff_options["properties"] == {}
    pytest_options = options_by_name["python.pytest"]
    assert pytest_options["properties"] == {}


def test_schema_exposes_github_wait_release_step_options():
    register_builtin_steps()
    payload = schema_payload()
    step_schemas = [obj for obj in payload["$defs"]["step"]["oneOf"] if isinstance(obj, dict) and obj.get("properties")]
    options_by_name = {next(iter(obj["properties"])): next(iter(obj["properties"].values())) for obj in step_schemas}

    wait_options = options_by_name["github.wait_release"]
    assert "required" not in wait_options
    assert sorted(wait_options["properties"]) == [
        "package",
        "poll_interval",
        "pyproject",
        "remote",
        "repository",
        "tag_prefix",
        "timeout",
        "version",
        "workflow",
    ]
    wait_metadata = next(metadata for metadata in list_step_metadata() if metadata.name == "github.wait_release")
    assert wait_metadata.external_tools == ("gh",)
    assert wait_metadata.risk == "safe"


def test_schema_exposes_google_play_upload_aab_required_options():
    payload = schema_payload()
    step_schemas = [obj for obj in payload["$defs"]["step"]["oneOf"] if isinstance(obj, dict) and obj.get("properties")]
    options_by_name = {next(iter(obj["properties"])): next(iter(obj["properties"].values())) for obj in step_schemas}

    upload_options = options_by_name["google_play.upload_aab"]
    assert upload_options["required"] == ["artifact", "package_name", "track", "release_status"]
    assert sorted(upload_options["properties"]) == [
        "artifact",
        "package_name",
        "release_name",
        "release_notes",
        "release_status",
        "track",
        "user_fraction",
    ]
    serialized = json.dumps(payload)
    assert "google_play.upload_aab" in serialized
    assert payload == json.loads(bundled_schema_path().read_text(encoding="utf-8"))


def test_schema_exposes_appstore_submit_review_required_options():
    payload = schema_payload()
    step_schemas = [obj for obj in payload["$defs"]["step"]["oneOf"] if isinstance(obj, dict) and obj.get("properties")]
    options_by_name = {next(iter(obj["properties"])): next(iter(obj["properties"].values())) for obj in step_schemas}

    submit_options = options_by_name["appstore.submit_review"]
    assert submit_options["required"] == ["whats_new", "release_mode", "phased_release"]
    assert sorted(submit_options["properties"]) == ["phased_release", "release_mode", "whats_new"]
    assert submit_options["properties"]["phased_release"]["type"] == "boolean"
    assert submit_options["properties"]["whats_new"] == {
        "type": "object",
        "additionalProperties": {"type": "string"},
    }
    serialized = json.dumps(payload)
    assert "appstore.submit_review" in serialized
    assert payload == json.loads(bundled_schema_path().read_text(encoding="utf-8"))


def test_release_summary_includes_release_results_without_reading_the_log(tmp_path, monkeypatch):
    package = tmp_path / "cdt_steps"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "demo.py").write_text(
        "from cdt.sdk import step\n\n"
        "@step('demo.confirm')\n"
        "def confirm(ctx):\n"
        "    ctx.register_release_results({\n"
        "        'github_release_url': 'https://github.com/example/cdt/releases/tag/v0.5.2',\n"
        "        'pypi_release_url': 'https://pypi.org/project/cdt-release/0.5.2/',\n"
        "    })\n",
        encoding="utf-8",
    )
    (tmp_path / "cdt.yaml").write_text(
        "version: 1\nplugins:\n  - cdt_steps.demo\npipelines:\n  release:\n    steps:\n      - demo.confirm\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))

    result = runner.invoke(app, ["run", "release"])
    payload = release_status("release")

    assert result.exit_code == 0, result.output
    assert payload["status"] == "success"
    assert payload["release_results"] == {
        "github_release_url": "https://github.com/example/cdt/releases/tag/v0.5.2",
        "pypi_release_url": "https://pypi.org/project/cdt-release/0.5.2/",
    }


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


def test_status_resolves_latest_global_and_pipeline_runs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    first = create_run(tmp_path, "test", run_id="first-run")
    second = create_run(tmp_path, "other", run_id="second-run")
    first_status = read_json(first.status)
    second_status = read_json(second.status)
    first_status["started_at"] = "2026-07-28T10:00:00+00:00"
    second_status["started_at"] = "2026-07-28T11:00:00+00:00"
    first.status.write_text(json.dumps(first_status), encoding="utf-8")
    second.status.write_text(json.dumps(second_status), encoding="utf-8")
    write_exit_code(first.exit, 0)
    write_exit_code(second.exit, 1)

    latest = runner.invoke(app, ["status", "--json"])
    pipeline = runner.invoke(app, ["status", "--pipeline", "test", "--json"])
    explicit = runner.invoke(app, ["status", first.run_id, "--pipeline", "other", "--json"])

    assert json.loads(latest.output)["run_id"] == second.run_id
    assert json.loads(pipeline.output)["run_id"] == first.run_id
    assert json.loads(explicit.output)["run_id"] == first.run_id


def test_logs_resolve_latest_run_apply_tail_and_redaction(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEMO_TOKEN", "persisted-secret")
    paths = create_run(tmp_path, "test")
    paths.log.write_text("first\nsecond persisted-secret\nthird\n", encoding="utf-8")

    result = runner.invoke(app, ["logs", "--pipeline", "test", "--tail", "2"])

    assert result.exit_code == 0
    assert result.output == "second ***\nthird\n"
    assert "persisted-secret" not in result.output


def test_history_combines_filters_before_limit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    wanted = create_run(tmp_path, "test", run_id="wanted-run")
    ignored_pipeline = create_run(tmp_path, "other", run_id="ignored-pipeline")
    ignored_status = create_run(tmp_path, "test", run_id="ignored-status")
    write_exit_code(wanted.exit, 1)
    write_exit_code(ignored_pipeline.exit, 1)
    write_exit_code(ignored_status.exit, 0)

    result = runner.invoke(
        app,
        ["history", "--pipeline", "test", "--status", "failed", "--limit", "1", "--json"],
    )
    payload = json.loads(result.output)

    assert result.exit_code == 0
    assert payload["filters"] == {"limit": 1, "pipeline": "test", "status": "failed"}
    assert [item["run_id"] for item in payload["runs"]] == [wanted.run_id]


def test_run_navigation_tolerates_invalid_and_corrupt_run_directories(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    healthy = create_run(tmp_path, "test")
    write_exit_code(healthy.exit, 0)
    invalid = tmp_path / ".cdt" / "runs" / "..invalid"
    invalid.mkdir()
    corrupt = create_run(tmp_path, "other")
    corrupt.manifest.write_text("{broken", encoding="utf-8")
    corrupt.status.write_text("{broken", encoding="utf-8")

    result = runner.invoke(app, ["history", "--json"])
    payload = json.loads(result.output)

    assert result.exit_code == 0
    assert healthy.run_id in {item["run_id"] for item in payload["runs"]}
    assert "..invalid" not in {item["run_id"] for item in payload["runs"]}


def test_status_without_recorded_runs_returns_unknown(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["status", "--json"])
    payload = json.loads(result.output)

    assert result.exit_code == 1
    assert payload["status"] == "unknown"


def _write_echo_project(path: Path, *, failing: bool = False) -> None:
    package = path / "cdt_steps"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "demo.py").write_text(
        "\n".join(
            [
                "import typer",
                "from cdt.sdk import step",
                "",
                "@step('demo.echo')",
                "def echo(ctx):",
                "    typer.echo('demo diagnostics line')",
                "",
                "@step('demo.fail')",
                "def fail(ctx):",
                "    raise typer.BadParameter('boom')",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    steps = ["demo.echo", "demo.fail"] if failing else ["demo.echo"]
    (path / "cdt.yaml").write_text(
        "version: 1\nplugins:\n  - cdt_steps.demo\npipelines:\n  test:\n    steps:\n"
        + "".join(f"      - {step}\n" for step in steps),
        encoding="utf-8",
    )


def _read_run_log(cwd: Path, run_id: str) -> str:
    return run_paths(cwd, run_id).log.read_text(encoding="utf-8")


def test_direct_run_writes_nonempty_success_log(tmp_path, monkeypatch):
    _write_echo_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))

    result = runner.invoke(app, ["run", "test"])
    runs = list_runs(tmp_path)
    log = _read_run_log(tmp_path, runs[0]["run_id"])

    assert result.exit_code == 0
    assert runs[0]["status"] == "success"
    assert "demo diagnostics line" in log
    assert "demo diagnostics line" in result.output


def test_direct_run_failure_log_keeps_terminal_summary(tmp_path, monkeypatch):
    _write_echo_project(tmp_path, failing=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))

    result = runner.invoke(app, ["run", "test"])
    runs = list_runs(tmp_path)
    log = _read_run_log(tmp_path, runs[0]["run_id"])
    exit_code = run_paths(tmp_path, runs[0]["run_id"]).exit.read_text(encoding="utf-8")

    assert result.exit_code != 0
    assert runs[0]["status"] == "failed"
    assert exit_code == "1\n"
    assert "demo diagnostics line" in log
    assert "PipelineExecutionError" not in log
    assert "boom" in log
    assert "Pipeline failed at step" in log
    assert "Artifacts produced: none" in log


def test_direct_run_log_captures_asc_retry_diagnostics(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    (tmp_path / "cdt.yaml").write_text(
        "version: 1\npipelines:\n  iosapp:\n    steps:\n      - appstore.complete_testflight\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    key = ec.generate_private_key(ec.SECP256R1())
    key_path = tmp_path / "AuthKey.p8"
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    monkeypatch.setenv("ASC_KEY_ID", "key-id")
    monkeypatch.setenv("ASC_ISSUER_ID", "issuer-id")
    monkeypatch.setenv("ASC_PRIVATE_KEY_PATH", str(key_path))
    monkeypatch.setenv("IOS_BUNDLE_ID", "com.example.app")
    monkeypatch.setattr("time.sleep", lambda seconds: None)

    def always_transient(request, timeout=None):
        raise urllib.error.URLError(TimeoutError("connection timed out"))

    monkeypatch.setattr("urllib.request.urlopen", always_transient)

    resume_status = tmp_path / "resume.json"
    resume_status.write_text(
        json.dumps({"completed_steps": [], "new_version": "1.2.3+7"}),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        ["run", "iosapp", "--resume-status-file", str(resume_status), "--skip-completed"],
    )
    runs = list_runs(tmp_path)
    log = _read_run_log(tmp_path, runs[0]["run_id"])

    assert result.exit_code != 0
    assert "==> Completing TestFlight upload for build 7" in log
    assert "ASC transient failure, attempt 1/4" in log
    assert "App Store Connect request failed after 4 attempts" in log
    assert "PipelineExecutionError" not in log
    assert "Pipeline failed at step" in log
    assert "connection timed out" in log


def test_detached_execution_does_not_duplicate_output_lines(tmp_path):
    package = tmp_path / "cdt_steps"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "demo.py").write_text(
        "from cdt.sdk import step\n\n"
        "@step('demo.output')\n"
        "def output(ctx):\n"
        "    print('detached marker line', flush=True)\n",
        encoding="utf-8",
    )
    (tmp_path / "cdt.yaml").write_text(
        "version: 1\nplugins:\n  - cdt_steps.demo\npipelines:\n  test:\n    steps:\n      - demo.output\n",
        encoding="utf-8",
    )
    run_dir = tmp_path / ".cdt" / "runs" / "marker-run"
    run_dir.mkdir(parents=True)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(tmp_path), env.get("PYTHONPATH")]))

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "cdt.agent_release_worker",
            "--pipeline",
            "test",
            "--run-id",
            "marker-run",
            "--log",
            str(run_dir / "output.log"),
            "--exit-file",
            str(run_dir / "exit-code"),
            "--status-file",
            str(run_dir / "status.json"),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    log = (run_dir / "output.log").read_text(encoding="utf-8")
    assert log.count("detached marker line") == 1


def test_existing_testflight_pipeline_external_actions_unchanged_without_submit_step(tmp_path, monkeypatch):
    """Without the new step an existing TestFlight pipeline behaves as before:

    one build, one upload, one completion — and no App Review activity at all.
    """

    (tmp_path / "cdt.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "pipelines:",
                "  iosapp:",
                "    steps:",
                "      - ios.bump_xcode_build_number",
                "      - ios.xcode_build_ipa",
                "      - appstore.upload_testflight",
                "      - appstore.complete_testflight",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "IOS_BUNDLE_ID=com.example.app\nIOS_TEST_SCHEME=Runner\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    def fail(method, path, client, payload=None, retry_ambiguous=True):
        raise AssertionError(f"existing TestFlight pipeline must not contact the ASC API: {method} {path}")

    monkeypatch.setattr(appstore_service, "_asc_request", fail)

    from cdt.steps import appstore as appstore_steps
    from cdt.steps import ios as ios_steps

    uploads, completions = [], []
    ipa = tmp_path / "App.ipa"

    def build_ipa(cwd, env, scheme):
        ipa.write_bytes(b"ipa")
        return ipa

    monkeypatch.setattr(ios_steps, "_increment_ios_build_number", lambda cwd, env, scheme: ("1.2.3+4", "1.2.3+5"))
    monkeypatch.setattr(ios_steps, "_ios_xcode_build_ipa", build_ipa)
    monkeypatch.setattr(
        appstore_steps,
        "_upload_testflight",
        lambda path, env, changelog, new_version: uploads.append((changelog, new_version)) or 0,
    )
    monkeypatch.setattr(
        appstore_steps,
        "_complete_testflight_after_upload",
        lambda env, changelog, new_version: completions.append(new_version) or 0,
    )

    result = runner.invoke(app, ["run", "iosapp"])

    assert result.exit_code == 0, result.output
    assert uploads == [("dev build", "1.2.3+5")]  # exactly one upload, same build number
    assert completions == ["1.2.3+5"]
    # No App Review activity: no ASC API call, no submission message, no review checkpoint.
    assert "Submitted for App Store review" not in result.output
    assert not (tmp_path / ".cdt" / "appstore" / "operations").exists()
    # The only new local effect is the additive upload record; Apple-facing
    # actions are unchanged.
    record = load_upload_record(tmp_path, "com.example.app")
    assert (record.marketing_version, record.build_number) == ("1.2.3", "5")


def test_resume_skips_finished_upload_and_reruns_only_testflight_completion(tmp_path, monkeypatch):
    (tmp_path / "cdt.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "pipelines:",
                "  iosapp:",
                "    steps:",
                "      - ios.bump_xcode_build_number",
                "      - appstore.upload_testflight_ipa",
                "      - appstore.complete_testflight",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    from cdt.steps import appstore as appstore_steps
    from cdt.steps import ios as ios_steps

    def forbidden(step):
        def fail(*args, **kwargs):
            raise AssertionError(f"resume must not run {step}")

        return fail

    completions = []

    def fake_complete(env, changelog, new_version):
        completions.append((changelog, new_version))
        return 0

    monkeypatch.setattr(ios_steps, "_increment_ios_build_number", forbidden("increment"))
    monkeypatch.setattr(appstore_steps, "_upload_testflight", forbidden("appstore.upload_testflight"))
    monkeypatch.setattr(appstore_steps, "_upload_testflight_ipa", forbidden("transporter upload"))
    monkeypatch.setattr(appstore_steps, "_complete_testflight_after_upload", fake_complete)

    resume_status = tmp_path / "failed-run.json"
    resume_status.write_text(
        json.dumps(
            {
                "completed_steps": ["0", "1"],
                "old_version": "1.2.3+4",
                "new_version": "1.2.3+5",
                "artifacts": [],
            }
        ),
        encoding="utf-8",
    )
    output_status = tmp_path / "out" / "status.json"

    result = runner.invoke(
        app,
        [
            "run",
            "iosapp",
            "--resume-status-file",
            str(resume_status),
            "--status-file",
            str(output_status),
            "--skip-completed",
        ],
    )

    assert result.exit_code == 0, result.output
    assert completions == [("dev build", "1.2.3+5")]
    status = json.loads(output_status.read_text(encoding="utf-8"))
    assert status["status"] == "success"
    assert status["completed_steps"] == ["0", "1", "2"]
    assert status["new_version"] == "1.2.3+5"


def _release_workflow_config():
    return yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))


def test_release_workflow_splits_into_dependency_ordered_jobs():
    jobs = _release_workflow_config()["jobs"]

    assert list(jobs) == ["validate-build", "pypi-publish", "github-release", "tag-smoke"]
    assert "needs" not in jobs["validate-build"]
    assert jobs["pypi-publish"]["needs"] == "validate-build"
    assert jobs["github-release"]["needs"] == "pypi-publish"
    assert jobs["tag-smoke"]["needs"] == "github-release"


def test_release_workflow_scopes_permissions_per_job():
    config = _release_workflow_config()
    jobs = config["jobs"]

    assert config["permissions"] == {"contents": "read"}
    assert jobs["validate-build"]["permissions"] == {"contents": "read"}
    assert jobs["pypi-publish"]["permissions"] == {"id-token": "write"}
    assert jobs["github-release"]["permissions"] == {"contents": "write"}
    assert jobs["tag-smoke"]["permissions"] == {"contents": "read"}


def test_release_workflow_validate_job_checks_exact_tag_and_uploads_distributions():
    job = _release_workflow_config()["jobs"]["validate-build"]

    checkout = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/checkout"))
    assert checkout["with"]["ref"] == "${{ github.ref }}"
    runs = {step["name"]: step["run"] for step in job["steps"] if "run" in step}
    assert runs["Install dev dependencies"] == "python -m pip install -e '.[dev]'"
    assert runs["Ruff"] == "ruff check ."
    assert runs["Tests"] == "pytest -q"
    assert runs["Build"] == "rm -rf dist\npython -m build\n"
    assert runs["Twine check"] == "twine check dist/*"
    upload = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/upload-artifact"))
    assert upload["with"]["name"] == "dist"
    assert upload["with"]["path"] == "dist/*"
    assert upload["with"]["if-no-files-found"] == "error"


def test_release_workflow_hands_off_artifacts_to_publish_and_release_jobs():
    jobs = _release_workflow_config()["jobs"]

    for job_name in ("pypi-publish", "github-release"):
        downloads = [
            step for step in jobs[job_name]["steps"] if step.get("uses", "").startswith("actions/download-artifact")
        ]
        assert len(downloads) == 1
        assert downloads[0]["with"]["name"] == "dist"


def test_release_workflow_publishes_to_pypi_with_trusted_publishing_as_last_step():
    job = _release_workflow_config()["jobs"]["pypi-publish"]

    assert job["permissions"] == {"id-token": "write"}
    publish_steps = [step for step in job["steps"] if step.get("uses", "").startswith("pypa/gh-action-pypi-publish")]
    assert len(publish_steps) == 1
    assert job["steps"][-1] is publish_steps[0]
    assert not any("run" in step for step in job["steps"])


def test_release_workflow_creates_github_release_with_checksums_after_pypi():
    job = _release_workflow_config()["jobs"]["github-release"]

    assert job["needs"] == "pypi-publish"
    runs = {step["name"]: step["run"] for step in job["steps"] if "run" in step}
    assert "sha256sum *.whl *.tar.gz > SHA256SUMS" in runs["Generate SHA-256 checksums"]
    release_step = next(step for step in job["steps"] if step.get("uses", "").startswith("softprops/action-gh-release"))
    assert "dist/*.whl" in release_step["with"]["files"]
    assert "dist/*.tar.gz" in release_step["with"]["files"]
    assert "dist/SHA256SUMS" in release_step["with"]["files"]


def test_release_workflow_smoke_job_installs_exact_tag_and_checks_version():
    job = _release_workflow_config()["jobs"]["tag-smoke"]

    assert job["needs"] == "github-release"
    script = job["steps"][-1]["run"]
    assert "pip install 'git+https://github.com/Sergionius/cdt.git@${{ github.ref_name }}'" in script
    assert "/tmp/cdt-tag-smoke/bin/cdt --version" in script
    assert 'expected_version="${GITHUB_REF_NAME#v}"' in script
    assert '[ "$installed_version" != "cdt $expected_version" ]' in script


def _write_release_project(path: Path) -> None:
    package = path / "cdt_steps"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "demo.py").write_text(
        "from cdt.sdk import step\n\n@step('demo.ok')\ndef ok(ctx):\n    ctx.values['ok'] = '1'\n",
        encoding="utf-8",
    )
    (path / "cdt.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "plugins:",
                "  - cdt_steps.demo",
                "pipelines:",
                "  release:",
                "    risk: production",
                "    inputs:",
                "      version:",
                "        required: true",
                "        pattern: '^\\d+\\.\\d+\\.\\d+$'",
                "    steps:",
                "      - demo.ok",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def test_production_pipeline_requires_and_accepts_declared_input(tmp_path, monkeypatch):
    _write_release_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))

    missing = runner.invoke(app, ["run", "release", "--confirm", "release"])
    mismatch = runner.invoke(
        app,
        ["run", "release", "--input", "version=not-semver", "--confirm", "release"],
    )
    accepted = runner.invoke(
        app,
        ["run", "release", "--input", "version=0.5.2", "--confirm", "release"],
    )

    assert missing.exit_code != 0
    assert "Missing required pipeline input" in missing.output
    assert mismatch.exit_code != 0
    assert "does not match pattern" in mismatch.output
    assert accepted.exit_code == 0, accepted.output
    runs = list_runs(tmp_path)
    assert len(runs) == 1
    manifest = read_json(tmp_path / ".cdt" / "runs" / runs[0]["run_id"] / "manifest.json")
    assert manifest["command"] == ["cdt", "run", "release", "--input", "version=0.5.2"]
    assert manifest["inputs"] == {"version": "0.5.2"}


def test_schema_exposes_pipeline_inputs():
    payload = schema_payload()

    input_def = payload["$defs"]["input"]
    assert input_def["additionalProperties"] is False
    assert sorted(input_def["properties"]) == ["pattern", "required"]
    pipeline_inputs = payload["$defs"]["pipeline"]["properties"]["inputs"]
    assert pipeline_inputs["additionalProperties"] == {"$ref": "#/$defs/input"}
    assert payload == json.loads(bundled_schema_path().read_text(encoding="utf-8"))


def test_detached_execution_propagates_inputs(tmp_path):
    package = tmp_path / "cdt_steps"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "demo.py").write_text(
        "from cdt.sdk import step\n\n"
        "@step('demo.echo')\n"
        "def echo(ctx, message: str):\n"
        "    (ctx.cwd / 'message.txt').write_text(message, encoding='utf-8')\n",
        encoding="utf-8",
    )
    (tmp_path / "cdt.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "plugins:",
                "  - cdt_steps.demo",
                "pipelines:",
                "  test:",
                "    inputs:",
                "      version:",
                "        required: true",
                "    steps:",
                "      - demo.echo:",
                "          message: ${inputs.version}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    run_dir = tmp_path / ".cdt" / "runs" / "input-run"
    run_dir.mkdir(parents=True)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(tmp_path), env.get("PYTHONPATH")]))

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "cdt.agent_release_worker",
            "--pipeline",
            "test",
            "--run-id",
            "input-run",
            "--log",
            str(run_dir / "output.log"),
            "--exit-file",
            str(run_dir / "exit-code"),
            "--status-file",
            str(run_dir / "status.json"),
            "--input",
            "version=0.5.2",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "message.txt").read_text(encoding="utf-8") == "0.5.2"
    status = json.loads((run_dir / "status.json").read_text(encoding="utf-8"))
    assert status["inputs"] == {"version": "0.5.2"}
