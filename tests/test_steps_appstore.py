"""Contract tests for the appstore.submit_review pipeline step.

All scenarios run on the in-memory ASC double from the service-layer tests:
no credentials, no network and no real Apple endpoints. CLI-level confirmation
tests live in tests/test_agent_first.py.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from cdt.cli import app
from cdt.pipeline import PipelineContext
from cdt.pipeline.builtins import register_builtin_steps
from cdt.pipeline.config import resolve_value
from cdt.pipeline.registry import _clear_steps_for_tests
from cdt.runner import CommandRunner
from cdt.schema import bundled_schema_path, schema_payload
from cdt.services import appstore, appstore_state
from cdt.services.appstore_state import (
    PHASE_CONFIRMED,
    checkpoint_path,
    compute_operation_id,
    load_review_checkpoint,
    save_upload_record,
)
from cdt.steps.appstore import SUBMIT_REVIEW_STEP_NAME, SubmitReviewStep
from tests.test_services_appstore_state import FakeAsc, _stub_client

runner = CliRunner()

BUNDLE_ID = "com.example.app"


def setup_function():
    _clear_steps_for_tests()
    register_builtin_steps()


def teardown_function():
    _clear_steps_for_tests()


def _context(tmp_path: Path, *, env: dict[str, str] | None = None, new_version: str | None = None, inputs=None):
    return PipelineContext(
        cwd=tmp_path,
        env={"IOS_BUNDLE_ID": BUNDLE_ID, **(env or {})},
        runner=CommandRunner(),
        new_version=new_version,
        inputs=dict(inputs or {}),
    )


def _step(**overrides: Any) -> SubmitReviewStep:
    options: dict[str, Any] = {
        "whats_new": {"ru": "Исправления и улучшения"},
        "release_mode": "manual",
        "phased_release": True,
    }
    options.update(overrides)
    return SubmitReviewStep(**options)


def _confirmed_checkpoint(
    tmp_path: Path,
    *,
    new_version: str | None = "1.2.3+5",
    whats_new: dict[str, str] | None = None,
    release_mode: str = "manual",
    phased_release: bool = True,
):
    intent = appstore_state.ReviewIntent(
        bundle_id=BUNDLE_ID,
        marketing_version="1.2.3",
        build_number="5",
        whats_new=whats_new or {"ru": "Исправления и улучшения"},
        release_mode=release_mode,
        phased_release=phased_release,
    )
    return load_review_checkpoint(checkpoint_path(tmp_path, compute_operation_id(intent)))


# -- step contract -------------------------------------------------------------


def test_required_options_have_no_defaults():
    signature = inspect.signature(SubmitReviewStep.__init__)
    for option in ("whats_new", "release_mode", "phased_release"):
        assert signature.parameters[option].default is inspect.Parameter.empty, option


def test_schema_marks_options_required_and_boolean_typed():
    payload = schema_payload()
    step_schemas = [obj for obj in payload["$defs"]["step"]["oneOf"] if isinstance(obj, dict) and obj.get("properties")]
    options_by_name = {next(iter(obj["properties"])): next(iter(obj["properties"].values())) for obj in step_schemas}

    submit_options = options_by_name[SUBMIT_REVIEW_STEP_NAME]
    assert submit_options["required"] == ["whats_new", "release_mode", "phased_release"]
    assert submit_options["properties"]["whats_new"]["type"] == "object"
    assert submit_options["properties"]["release_mode"]["type"] == "string"
    assert submit_options["properties"]["phased_release"]["type"] == "boolean"
    assert payload == json.loads(bundled_schema_path().read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "overrides",
    [
        {"whats_new": "Исправления"},
        {"whats_new": None},
        {"whats_new": {}},
        {"whats_new": {"ru": ""}},
        {"whats_new": {"ru": "   "}},
        {"whats_new": {"": "Текст"}},
        {"whats_new": {"  ": "Текст"}},
        {"whats_new": {None: "Текст"}},
        {"whats_new": {"ru": 5}},
        {"release_mode": "Manual"},
        {"release_mode": "MANUAL"},
        {"release_mode": "auto"},
        {"release_mode": ""},
        {"release_mode": "  "},
        {"release_mode": None},
        {"phased_release": "false"},
        {"phased_release": "true"},
        {"phased_release": 1},
        {"phased_release": 0},
        {"phased_release": None},
    ],
)
def test_option_validation_rejects_invalid_values(overrides):
    with pytest.raises(typer.BadParameter):
        _step(**overrides)


@pytest.mark.parametrize("phased_release", [True, False])
@pytest.mark.parametrize("release_mode", ["manual", "automatic"])
def test_valid_boolean_and_mode_combinations_are_accepted(release_mode, phased_release):
    step = _step(release_mode=release_mode, phased_release=phased_release)

    assert step.release_mode == release_mode
    assert step.phased_release is phased_release


def test_whats_new_strips_locales_and_texts():
    step = _step(whats_new={"  ru  ": " Исправления "})

    assert step.whats_new == {"ru": "Исправления"}


# -- target selection without manual app/version/build input --------------------


def test_missing_ios_bundle_id_fails_before_any_asc_call(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("no ASC client may be created without IOS_BUNDLE_ID")

    monkeypatch.setattr(appstore, "_AscClient", fail)
    ctx = _context(tmp_path, env={"IOS_BUNDLE_ID": ""}, new_version="1.2.3+5")

    with pytest.raises(typer.BadParameter, match="IOS_BUNDLE_ID"):
        _step().run(ctx)


def test_current_pipeline_version_is_used_without_manual_input(tmp_path, monkeypatch, capsys):
    asc = FakeAsc(monkeypatch)
    _stub_client(monkeypatch)
    ctx = _context(tmp_path, new_version="1.2.3+5")

    _step().run(ctx)

    assert any(call["path"] == "/v1/builds" for call in asc.calls)  # exact build lookup happened
    checkpoint = _confirmed_checkpoint(tmp_path)
    assert checkpoint.phase == PHASE_CONFIRMED
    assert checkpoint.marketing_version == "1.2.3"
    assert checkpoint.build_number == "5"
    assert ctx.release_results["appstore_review_bundle_id"] == BUNDLE_ID
    output = capsys.readouterr().out
    assert "Submitting 1.2.3+5" in output
    assert "Submitted for App Store review" in output
    assert "does NOT mean approval or user availability" in output


def test_standalone_run_uses_recorded_completion_without_manual_input(tmp_path, monkeypatch, capsys):
    asc = FakeAsc(monkeypatch)
    _stub_client(monkeypatch)
    save_upload_record(tmp_path, BUNDLE_ID, "1.2.3+5")
    ctx = _context(tmp_path, new_version=None)

    _step().run(ctx)

    assert any(call["path"] == "/v1/builds" for call in asc.calls)  # exact build lookup happened
    checkpoint = _confirmed_checkpoint(tmp_path)
    assert checkpoint.phase == PHASE_CONFIRMED
    output = capsys.readouterr().out
    assert "Submitting 1.2.3+5" in output
    assert ctx.release_results["appstore_review_marketing_version"] == "1.2.3"
    assert ctx.release_results["appstore_review_build_number"] == "5"


def test_standalone_run_without_record_fails_clearly_without_touching_apple(tmp_path, monkeypatch):
    asc = FakeAsc(monkeypatch)
    _stub_client(monkeypatch)
    ctx = _context(tmp_path, new_version=None)

    with pytest.raises(typer.BadParameter, match="No recorded TestFlight build"):
        _step().run(ctx)

    assert asc.calls == []
    assert not (tmp_path / ".cdt" / "appstore").exists()


def test_invalid_current_version_does_not_fall_back_to_record(tmp_path, monkeypatch):
    FakeAsc(monkeypatch)
    _stub_client(monkeypatch)
    save_upload_record(tmp_path, BUNDLE_ID, "1.2.3+5")
    ctx = _context(tmp_path, new_version="broken")

    with pytest.raises(typer.BadParameter, match="Invalid pipeline version"):
        _step().run(ctx)

    assert not (tmp_path / ".cdt" / "appstore" / "operations").exists()


def test_selected_build_is_reverified_in_asc_before_submission(tmp_path, monkeypatch):
    asc = FakeAsc(monkeypatch)
    _stub_client(monkeypatch)
    asc.build["attributes"]["version"] = "9"  # ASC does not have build 5 anymore
    ctx = _context(tmp_path, new_version="1.2.3+5")

    with pytest.raises(typer.BadParameter, match="not found in App Store Connect"):
        _step().run(ctx)

    assert not (tmp_path / ".cdt" / "appstore").exists()


# -- standalone submit pipeline needs no build, IPA or artifacts -----------------


def test_standalone_submit_pipeline_needs_no_ipa_or_build_artifacts(tmp_path, monkeypatch):
    """A submit-only pipeline identifies app and build without any build inputs."""

    _write_submit_project(tmp_path)
    (tmp_path / ".env").write_text("IOS_BUNDLE_ID=com.example.app\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    save_upload_record(tmp_path, BUNDLE_ID, "1.2.3+5")
    asc = FakeAsc(monkeypatch)
    _stub_client(monkeypatch)
    status_file = tmp_path / "out" / "status.json"

    result = runner.invoke(
        app,
        ["run", "submit", "--input", "whats_new=Исправления", "--confirm", "submit",
         "--status-file", str(status_file)],
    )

    assert result.exit_code == 0, result.output
    assert "Submitting 1.2.3+5" in result.output
    # The pipeline declared no build steps and no IPA exists anywhere in the
    # project: the build came from the recorded completion only.
    assert not list(tmp_path.rglob("*.ipa"))
    payload = json.loads(status_file.read_text(encoding="utf-8"))
    assert payload["status"] == "success"
    assert payload["artifacts"] == []
    assert payload["new_version"] is None
    checkpoint = _confirmed_checkpoint(tmp_path, whats_new={"ru": "Исправления"})
    assert (checkpoint.marketing_version, checkpoint.build_number) == ("1.2.3", "5")
    assert any(call["path"] == "/v1/builds" for call in asc.calls)  # exact build lookup happened


# -- confirmed result, interpolation and registration ---------------------------


def test_confirmed_submission_registers_results_and_distinguishes_review_from_release(tmp_path, monkeypatch, capsys):
    FakeAsc(monkeypatch)
    _stub_client(monkeypatch)
    ctx = _context(tmp_path, new_version="1.2.3+5")

    _step(release_mode="automatic", phased_release=False).run(ctx)

    assert ctx.release_results == {
        "appstore_review_bundle_id": BUNDLE_ID,
        "appstore_review_marketing_version": "1.2.3",
        "appstore_review_build_number": "5",
        "appstore_review_submission_id": "rs-2",
        "appstore_review_submission_state": "WAITING_FOR_REVIEW",
        "appstore_review_release_mode": "automatic",
        "appstore_review_phased_release": "false",
    }
    output = capsys.readouterr().out
    assert "Submitted for App Store review" in output
    assert "does NOT mean approval or user availability" in output
    assert "available to users" not in output.split("Submitted for App Store review")[0]


def test_whats_new_supports_input_interpolation(tmp_path, monkeypatch):
    FakeAsc(monkeypatch)
    _stub_client(monkeypatch)
    ctx = _context(tmp_path, new_version="1.2.3+5", inputs={"whats_new": "Новый текст сборки"})
    resolved = resolve_value({"ru": "${inputs.whats_new}"}, ctx)

    SubmitReviewStep(whats_new=resolved, release_mode="manual", phased_release=True).run(ctx)

    checkpoint = _confirmed_checkpoint(tmp_path, whats_new={"ru": "Новый текст сборки"})
    assert checkpoint.whats_new == {"ru": "Новый текст сборки"}


def test_service_errors_fail_the_step_without_success_claims(tmp_path, monkeypatch, capsys):
    asc = FakeAsc(monkeypatch)
    _stub_client(monkeypatch)
    asc.fail_on("POST", "/v1/reviewSubmissions", appstore.AscHttpError("denied", code=403, method="POST",
                                                                     path="/v1/reviewSubmissions", body="denied"))
    ctx = _context(tmp_path, new_version="1.2.3+5")

    with pytest.raises(typer.BadParameter):
        _step().run(ctx)

    output = capsys.readouterr().out
    assert "Submitted for App Store review" not in output


# -- inspect, plan and dry-run stay fully offline -------------------------------


def _write_submit_project(tmp_path: Path) -> None:
    (tmp_path / "cdt.yaml").write_text(
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
                "            ru: \"${inputs.whats_new}\"",
                "          release_mode: manual",
                "          phased_release: true",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def test_inspect_plan_and_dry_run_do_not_touch_apple_or_create_checkpoints(tmp_path, monkeypatch):
    _write_submit_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    def fail(method, path, client, payload=None, retry_ambiguous=True):
        raise AssertionError(f"inspect/plan/dry-run must not contact App Store Connect: {method} {path}")

    monkeypatch.setattr(appstore, "_asc_request", fail)

    for argv in (
        ["pipeline", "inspect", "submit", "--json"],
        ["pipeline", "plan", "submit", "--json"],
        ["run", "submit", "--dry-run", "--input", "whats_new=Исправления"],
    ):
        result = runner.invoke(app, argv)
        assert result.exit_code == 0, (argv, result.output)

    assert not (tmp_path / ".cdt" / "appstore").exists()
    assert not (tmp_path / ".cdt" / "runs").exists()
