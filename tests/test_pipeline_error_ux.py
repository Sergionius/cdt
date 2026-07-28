import pytest
import typer

from cdt.pipeline import PipelineContext, PipelineExecutor
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
