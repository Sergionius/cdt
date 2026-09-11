import json

import pytest
import typer

from cdt.pipeline import ParallelStepGroup, PipelineContext, PipelineExecutor
from cdt.pipeline.config import ConfiguredStep, load_pipeline_config
from cdt.pipeline.registry import _clear_steps_for_tests, register_step
from cdt.runner import CommandRunner


class FailingStep:
    name = "demo.fail"

    def run(self, ctx):
        raise typer.BadParameter("boom")


class SecretFailingStep:
    name = "demo.secret_fail"

    def run(self, ctx):
        raise typer.BadParameter(f"provider rejected {ctx.env['API_TOKEN']}")


class OkStep:
    name = "demo.ok"

    def run(self, ctx):
        return None


def setup_function():
    _clear_steps_for_tests()


def teardown_function():
    _clear_steps_for_tests()


def test_yaml_parse_error_includes_path_line_column_and_example(tmp_path):
    (tmp_path / "cdt.yaml").write_text("version: [\n", encoding="utf-8")

    try:
        load_pipeline_config(tmp_path)
    except typer.BadParameter as exc:
        message = str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected BadParameter")

    assert str(tmp_path / "cdt.yaml") in message
    assert "line" in message
    assert "column" in message
    assert "Example:" in message


def test_failed_step_summary_includes_step_command_exit_and_artifacts(tmp_path):
    register_step("demo.fail", lambda **kwargs: FailingStep())
    ctx = PipelineContext(cwd=tmp_path, env={}, runner=CommandRunner())

    try:
        PipelineExecutor().run([ConfiguredStep("demo.fail", {"script": "scripts/fail.py"})], ctx)
    except typer.BadParameter as exc:
        message = str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected BadParameter")

    assert "Failed step: demo.fail" in message
    assert "command: scripts/fail.py" in message
    assert "exit code:" in message
    assert "artifacts produced:" in message


def test_failed_step_summary_redacts_known_secrets(tmp_path):
    register_step("demo.secret_fail", lambda **kwargs: SecretFailingStep())
    ctx = PipelineContext(cwd=tmp_path, env={"API_TOKEN": "provider-secret"}, runner=CommandRunner())

    with pytest.raises(typer.BadParameter) as exc_info:
        PipelineExecutor().run([ConfiguredStep("demo.secret_fail", {})], ctx)

    assert "provider rejected ***" in str(exc_info.value)
    assert "provider-secret" not in str(exc_info.value)


def test_parallel_failure_summary_uses_failed_child_metadata(tmp_path):
    register_step("demo.fail", lambda **kwargs: FailingStep())
    register_step("demo.ok", lambda **kwargs: OkStep())
    ctx = PipelineContext(cwd=tmp_path, env={}, runner=CommandRunner())
    group = ParallelStepGroup(
        [
            ConfiguredStep("demo.fail", {"script": "scripts/fail.py"}, "0/0"),
            ConfiguredStep("demo.ok", {}, "0/1"),
        ],
        step_id="0",
    )

    with pytest.raises(typer.BadParameter) as exc_info:
        PipelineExecutor().run([group], ctx)

    message = str(exc_info.value)
    assert "0/0 (demo.fail): boom" in message
    assert "Failed step: 0/0 (demo.fail)" in message
    assert "command: scripts/fail.py" in message
    assert "command: unknown" not in message


def test_parallel_failure_redacts_child_secrets_in_error_and_status(tmp_path):
    register_step("demo.secret_fail", lambda **kwargs: SecretFailingStep())
    register_step("demo.ok", lambda **kwargs: OkStep())
    status_file = tmp_path / "status.json"
    ctx = PipelineContext(
        cwd=tmp_path,
        env={"API_TOKEN": "provider-secret"},
        runner=CommandRunner(),
        status_file=status_file,
    )
    group = ParallelStepGroup(
        [
            ConfiguredStep("demo.secret_fail", {}, "0/0"),
            ConfiguredStep("demo.ok", {}, "0/1"),
        ],
        step_id="0",
    )

    with pytest.raises(typer.BadParameter) as exc_info:
        PipelineExecutor().run([group], ctx)

    assert "provider rejected ***" in str(exc_info.value)
    assert "provider-secret" not in str(exc_info.value)
    payload = json.loads(status_file.read_text(encoding="utf-8"))
    assert "provider-secret" not in json.dumps(payload)
    assert payload["failed_step"] == "0/0"
    assert payload["status"] == "failed"
