"""Tests for the notify.webhook pipeline step and its preflight/plan behaviour.

All HTTP traffic is mocked; no test performs real network calls.
"""

import json
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from cdt.cli import app
from cdt.pipeline.config import load_pipeline_config
from cdt.pipeline.context import PipelineContext
from cdt.pipeline.registry import get_step_metadata
from cdt.pipeline.validation import validate_pipeline
from cdt.runner import CommandRunner
from cdt.runs import list_runs
from cdt.services import webhook
from cdt.services.webhook import WebhookError
from cdt.steps.notify import NotifyWebhookStep

runner = CliRunner()


@pytest.fixture(autouse=True)
def _register_builtins():
    from cdt.pipeline.builtins import register_builtin_steps

    register_builtin_steps()
    yield


class FakeResponse:
    def __init__(self, status: int = 200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def make_ctx(tmp_path, env=None):
    return PipelineContext(cwd=tmp_path, env=env or {}, runner=CommandRunner())


def patch_transport(monkeypatch, handler=None):
    calls = []

    def fake_open(request, timeout):
        calls.append((request, timeout))
        if handler is None:
            return FakeResponse()
        return handler(request, timeout)

    monkeypatch.setattr(webhook, "_open_webhook_response", fake_open)
    return calls


# --- constructor validation ---------------------------------------------


def test_step_rejects_invalid_url_env_name(tmp_path):
    with pytest.raises(typer.BadParameter, match="url_env"):
        NotifyWebhookStep("MY URL", payload={"text": "hi"})
    with pytest.raises(typer.BadParameter, match="url_env"):
        NotifyWebhookStep("${inputs.hook_env}", payload={"text": "hi"})


def test_step_rejects_empty_or_non_object_payload(tmp_path):
    with pytest.raises(typer.BadParameter, match="payload"):
        NotifyWebhookStep("WEBHOOK_URL", payload={})
    with pytest.raises(typer.BadParameter, match="payload"):
        NotifyWebhookStep("WEBHOOK_URL", payload=["text"])


def test_step_rejects_bad_timeout_and_fail_on_error(tmp_path):
    with pytest.raises(typer.BadParameter, match="timeout_seconds"):
        NotifyWebhookStep("WEBHOOK_URL", payload={"t": 1}, timeout_seconds=0)
    with pytest.raises(typer.BadParameter, match="timeout_seconds"):
        NotifyWebhookStep("WEBHOOK_URL", payload={"t": 1}, timeout_seconds="ten")
    with pytest.raises(typer.BadParameter, match="fail_on_error"):
        NotifyWebhookStep("WEBHOOK_URL", payload={"t": 1}, fail_on_error="yes")


# --- delivery and fail_on_error semantics -------------------------------


def test_step_success_reports_status_without_url_or_payload(tmp_path, monkeypatch, capsys):
    patch_transport(monkeypatch)
    step = NotifyWebhookStep("WEBHOOK_URL", payload={"text": "release 1.2.3 is out"})

    step.run(make_ctx(tmp_path, {"WEBHOOK_URL": "https://hooks.example/abc123"}))

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "Webhook notification delivered (HTTP 200)" in output
    assert "https://hooks.example" not in output
    assert "release 1.2.3 is out" not in output


def test_step_strict_mode_turns_delivery_failure_into_error_without_details(tmp_path, monkeypatch, capsys):
    def handler(request, timeout):
        raise WebhookError("http_error", 500)

    patch_transport(monkeypatch, handler=handler)
    step = NotifyWebhookStep("WEBHOOK_URL", payload={"text": "hello"})

    with pytest.raises(typer.BadParameter, match=r"notify\.webhook failed: http_error \(HTTP 500\)"):
        step.run(make_ctx(tmp_path, {"WEBHOOK_URL": "https://hooks.example/abc123"}))

    captured = capsys.readouterr()
    assert "https://hooks.example" not in captured.out
    assert "hello" not in captured.out


def test_step_warning_mode_reports_failure_and_does_not_raise(tmp_path, monkeypatch, capsys):
    def handler(request, timeout):
        raise WebhookError("timeout")

    patch_transport(monkeypatch, handler=handler)
    step = NotifyWebhookStep("WEBHOOK_URL", payload={"text": "hello"}, fail_on_error=False)

    step.run(make_ctx(tmp_path, {"WEBHOOK_URL": "https://hooks.example/abc123"}))

    captured = capsys.readouterr()
    assert captured.err is not None
    combined = captured.out + captured.err
    assert "notify.webhook failed: timeout" in combined
    assert "https://hooks.example" not in combined


def test_step_configuration_errors_are_never_downgraded_to_warnings(tmp_path, monkeypatch):
    patch_transport(monkeypatch, handler=lambda request, timeout: pytest.fail("unexpected network call"))
    step = NotifyWebhookStep("WEBHOOK_URL", payload={"text": "token supersecret1"}, fail_on_error=False)

    # Missing destination env and a secret in the payload are configuration
    # errors even in warning mode.
    with pytest.raises(typer.BadParameter, match="WEBHOOK_URL"):
        step.run(make_ctx(tmp_path, {}))

    with pytest.raises(typer.BadParameter, match="known context secret"):
        step.run(make_ctx(tmp_path, {"WEBHOOK_URL": "https://hooks.example/abc", "NOTIFY_TOKEN": "supersecret1"}))


# --- registration, retries, plan and preflight ---------------------------


def test_webhook_metadata_declares_no_retries_but_native_timeout_capability():
    metadata = get_step_metadata("notify.webhook")

    assert metadata.retry_safe is False
    assert metadata.timeout_option == "timeout_seconds"
    assert metadata.category == "notify"


def test_automatic_retries_are_rejected_for_notify_webhook(tmp_path):
    (tmp_path / "cdt.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "pipelines:",
                "  hook:",
                "    steps:",
                "      - step: notify.webhook",
                "        with: {url_env: WEBHOOK_URL, payload: {text: hi}}",
                "        retry: {max_attempts: 3, delay_seconds: 0}",
            ]
        ),
        encoding="utf-8",
    )
    config = load_pipeline_config(tmp_path)

    errors = validate_pipeline(config, "hook")

    assert any(error["code"] == "retry_requires_capability" for error in errors)


def test_envelope_timeout_is_accepted_for_notify_webhook(tmp_path):
    (tmp_path / "cdt.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "pipelines:",
                "  hook:",
                "    steps:",
                "      - step: notify.webhook",
                "        with: {url_env: WEBHOOK_URL, payload: {text: hi}}",
                "        timeout_seconds: 15",
            ]
        ),
        encoding="utf-8",
    )
    config = load_pipeline_config(tmp_path)

    assert validate_pipeline(config, "hook") == []


def test_ambiguous_timeout_setting_is_rejected(tmp_path):
    (tmp_path / "cdt.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "pipelines:",
                "  hook:",
                "    steps:",
                "      - step: notify.webhook",
                "        with: {url_env: WEBHOOK_URL, payload: {text: hi}, timeout_seconds: 5}",
                "        timeout_seconds: 15",
            ]
        ),
        encoding="utf-8",
    )
    config = load_pipeline_config(tmp_path)

    errors = validate_pipeline(config, "hook")

    assert any(error["code"] == "ambiguous_step_timeout" for error in errors)


def test_plan_and_inspect_show_names_only_and_never_read_credentials_or_network(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBHOOK_URL", "https://hooks.example/abc123")
    monkeypatch.setenv("WEBHOOK_AUTH", "Bearer top-secret-token")
    patch_transport(monkeypatch, handler=lambda request, timeout: pytest.fail("unexpected network call"))
    (tmp_path / "cdt.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "pipelines:",
                "  hook:",
                "    steps:",
                "      - step: notify.webhook",
                "        with: {url_env: WEBHOOK_URL, payload: {text: hi}, authorization_env: WEBHOOK_AUTH}",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    plan = runner.invoke(app, ["pipeline", "plan", "hook", "--json"])
    inspect = runner.invoke(app, ["pipeline", "inspect", "hook", "--json"])

    assert plan.exit_code == 0, plan.output
    assert inspect.exit_code == 0, inspect.output
    plan_text = plan.output + inspect.output
    assert "WEBHOOK_URL" in plan_text  # names only
    assert "https://hooks.example" not in plan_text  # never the destination value
    assert "top-secret-token" not in plan_text  # never credentials


def test_preflight_checks_dynamic_webhook_env_keys_without_network(tmp_path, monkeypatch):
    patch_transport(monkeypatch, handler=lambda request, timeout: pytest.fail("unexpected network call"))
    (tmp_path / "cdt.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "pipelines:",
                "  hook:",
                "    steps:",
                "      - step: notify.webhook",
                "        with: {url_env: WEBHOOK_URL, payload: {text: hi}, authorization_env: WEBHOOK_AUTH}",
                "      - step: notify.webhook",
                "        with: {url_env: LITERAL_URL, payload: {text: hi}}",
                "      - step: notify.webhook",
                "        with: {url_env: SHARED_HOOK, payload: {text: hi}}",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WEBHOOK_URL", raising=False)
    monkeypatch.delenv("WEBHOOK_AUTH", raising=False)
    monkeypatch.setenv("LITERAL_URL", "https://hooks.example/one")
    # SHARED_HOOK is intentionally left unset, so the preflight reports an
    # error status; the JSON payload below is the assertion target either way.

    result = runner.invoke(app, ["pipeline", "preflight", "hook", "--json"])

    payload = json.loads(result.output)
    checked = {entry["name"]: entry["present"] for entry in payload["env"]}
    assert checked["WEBHOOK_URL"] is False
    assert checked["WEBHOOK_AUTH"] is False
    assert checked["LITERAL_URL"] is True
    assert "SHARED_HOOK" in checked  # literal name is checked even when missing
    assert payload["missing_env"] == ["SHARED_HOOK", "WEBHOOK_AUTH", "WEBHOOK_URL"]


def test_preflight_skips_interpolated_webhook_env_names(tmp_path, monkeypatch):
    patch_transport(monkeypatch, handler=lambda request, timeout: pytest.fail("unexpected network call"))
    (tmp_path / "cdt.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "pipelines:",
                "  hook:",
                "    inputs:",
                "      hook_env: {}",
                "    steps:",
                "      - step: notify.webhook",
                "        with:",
                "          url_env: '${inputs.hook_env}'",
                "          payload: {text: hi}",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["pipeline", "preflight", "hook", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    # Interpolated names cannot be checked statically; validation happens at run.
    assert all(not entry["name"].startswith("${") for entry in payload["env"])


# --- end-to-end runs: leaks in status/output.log --------------------------


def _write_webhook_project(tmp_path: Path) -> None:
    (tmp_path / "cdt.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "pipelines:",
                "  hook:",
                "    steps:",
                "      - step: notify.webhook",
                "        with:",
                "          url_env: WEBHOOK_URL",
                "          authorization_env: WEBHOOK_AUTH",
                "          payload: {text: release-1.2.3-finished}",
            ]
        ),
        encoding="utf-8",
    )


def test_cli_run_webhook_success_has_no_leaks_in_status_or_log(tmp_path, monkeypatch):
    calls = patch_transport(monkeypatch)
    _write_webhook_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WEBHOOK_URL", "https://hooks.example/abc123")
    monkeypatch.setenv("WEBHOOK_AUTH", "Bearer top-secret-token")

    result = runner.invoke(app, ["run", "hook"])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    runs = list_runs(tmp_path)
    log = (tmp_path / ".cdt" / "runs" / runs[0]["run_id"] / "output.log").read_text(encoding="utf-8")
    status = (tmp_path / ".cdt" / "runs" / runs[0]["run_id"] / "status.json").read_text(encoding="utf-8")
    combined = log + status
    assert "https://hooks.example" not in combined
    assert "top-secret-token" not in combined
    assert "release-1.2.3-finished" not in combined


def test_cli_run_webhook_strict_failure_hides_destination_and_payload(tmp_path, monkeypatch):
    def handler(request, timeout):
        raise WebhookError("http_error", 503)

    patch_transport(monkeypatch, handler=handler)
    _write_webhook_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WEBHOOK_URL", "https://hooks.example/abc123")
    monkeypatch.setenv("WEBHOOK_AUTH", "Bearer top-secret-token")

    result = runner.invoke(app, ["run", "hook"])

    assert result.exit_code != 0
    assert "notify.webhook failed: http_error (HTTP 503)" in result.output
    runs = list_runs(tmp_path)
    log = (tmp_path / ".cdt" / "runs" / runs[0]["run_id"] / "output.log").read_text(encoding="utf-8")
    status = (tmp_path / ".cdt" / "runs" / runs[0]["run_id"] / "status.json").read_text(encoding="utf-8")
    combined = log + status
    assert "https://hooks.example" not in combined
    assert "top-secret-token" not in combined
    assert "release-1.2.3-finished" not in combined
    assert "503" in status  # the safe status category is recorded


def test_cli_run_webhook_warning_mode_succeeds_and_records_no_secrets(tmp_path, monkeypatch):
    def handler(request, timeout):
        raise WebhookError("network_error")

    patch_transport(monkeypatch, handler=handler)
    (tmp_path / "cdt.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "pipelines:",
                "  hook:",
                "    steps:",
                "      - step: notify.webhook",
                "        with:",
                "          url_env: WEBHOOK_URL",
                "          payload: {text: release-1.2.3-finished}",
                "          fail_on_error: false",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WEBHOOK_URL", "https://hooks.example/abc123")

    result = runner.invoke(app, ["run", "hook"])

    assert result.exit_code == 0, result.output
    assert "notify.webhook failed: network_error" in result.output
    runs = list_runs(tmp_path)
    log = (tmp_path / ".cdt" / "runs" / runs[0]["run_id"] / "output.log").read_text(encoding="utf-8")
    status = (tmp_path / ".cdt" / "runs" / runs[0]["run_id"] / "status.json").read_text(encoding="utf-8")
    combined = log + status
    assert "https://hooks.example" not in combined
    assert "release-1.2.3-finished" not in combined


# --- interpolation of explicit payload fields ----------------------------


def test_payload_fields_interpolate_inputs_and_nothing_is_added(tmp_path, monkeypatch):
    calls = patch_transport(monkeypatch)
    (tmp_path / "cdt.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "pipelines:",
                "  hook:",
                "    inputs:",
                "      version:",
                "        required: true",
                "    steps:",
                "      - step: notify.webhook",
                "        with:",
                "          url_env: WEBHOOK_URL",
                "          payload:",
                "            text: released ${inputs.version}",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WEBHOOK_URL", "https://hooks.example/abc123")
    monkeypatch.delenv("EXTRA_ENV_VALUE", raising=False)

    result = runner.invoke(app, ["run", "hook", "--input", "version=1.2.3"])

    assert result.exit_code == 0, result.output
    request = calls[0][0]
    body = json.loads(request.data.decode("utf-8"))
    # Only the explicit payload field, interpolated; no env/inputs/artifacts/context added.
    assert body == {"text": "released 1.2.3"}
