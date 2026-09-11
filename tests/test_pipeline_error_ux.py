import json

import pytest
import typer

from cdt.pipeline import ParallelStepGroup, PipelineContext, PipelineExecutor
from cdt.pipeline.config import ConfiguredStep, load_pipeline_config
from cdt.pipeline.executor import PipelineExecutionError
from cdt.pipeline.registry import _clear_steps_for_tests, register_step
from cdt.runner import CommandExecutionError, CommandRunner


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


class ExitFailingStep:
    name = "demo.exit_fail"

    def __init__(self, code: int = 3):
        self.code = code

    def run(self, ctx):
        raise typer.Exit(code=self.code)


class EmptyFailingStep:
    name = "demo.empty_fail"

    def run(self, ctx):
        raise ValueError()


class CommandFailStep:
    name = "demo.command_fail"

    def __init__(self, command: list[str], exit_code: int, cause: str = "flutter build failed"):
        self.command = command
        self.exit_code = exit_code
        self.cause = cause
        self.options = {"script": "configured/option.sh"}

    def run(self, ctx):
        raise CommandExecutionError(self.cause, command=self.command, exit_code=self.exit_code)


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
    except PipelineExecutionError as exc:
        message = str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected PipelineExecutionError")

    lines = message.splitlines()
    assert lines[0] == "Pipeline failed at step demo.fail."
    assert lines[1] == "boom"
    assert "Command: scripts/fail.py" in lines
    assert "Exit code:" not in message
    assert lines[-1] == "Artifacts produced: none"
    assert "unknown" not in message
    assert "not applicable" not in message


def test_failed_step_summary_redacts_known_secrets(tmp_path):
    register_step("demo.secret_fail", lambda **kwargs: SecretFailingStep())
    ctx = PipelineContext(cwd=tmp_path, env={"API_TOKEN": "provider-secret"}, runner=CommandRunner())

    with pytest.raises(PipelineExecutionError) as exc_info:
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

    with pytest.raises(PipelineExecutionError) as exc_info:
        PipelineExecutor().run([group], ctx)

    message = str(exc_info.value)
    lines = message.splitlines()
    assert lines[0] == "Pipeline failed at step 0/0 (demo.fail)."
    assert lines[1] == "boom"
    assert "Command: scripts/fail.py" in lines
    assert "Other parallel steps were allowed to finish." in lines
    assert lines[-1] == "Artifacts produced: none"
    assert "unknown" not in message
    assert exc_info.value.failed_step_id == "0/0"


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

    with pytest.raises(PipelineExecutionError) as exc_info:
        PipelineExecutor().run([group], ctx)

    assert "provider rejected ***" in str(exc_info.value)
    assert "provider-secret" not in str(exc_info.value)
    payload = json.loads(status_file.read_text(encoding="utf-8"))
    assert "provider-secret" not in json.dumps(payload)
    assert payload["failed_step"] == "0/0"
    assert payload["status"] == "failed"


def test_parallel_empty_typer_exit_gets_readable_fallback(tmp_path):
    register_step("demo.exit_fail", lambda **kwargs: ExitFailingStep())
    register_step("demo.ok", lambda **kwargs: OkStep())
    ctx = PipelineContext(cwd=tmp_path, env={}, runner=CommandRunner())
    group = ParallelStepGroup(
        [
            ConfiguredStep("demo.exit_fail", {}, "0/0"),
            ConfiguredStep("demo.ok", {}, "0/1"),
        ],
        step_id="0",
    )

    with pytest.raises(PipelineExecutionError) as exc_info:
        PipelineExecutor().run([group], ctx)

    message = str(exc_info.value)
    lines = message.splitlines()
    assert lines[0] == "Pipeline failed at step 0/0 (demo.exit_fail)."
    assert lines[1] == "Step exited with code 3."
    assert "Exit code:" not in message
    assert "Command:" not in message


def test_parallel_empty_exception_gets_class_name_fallback(tmp_path):
    register_step("demo.empty_fail", lambda **kwargs: EmptyFailingStep())
    ctx = PipelineContext(cwd=tmp_path, env={}, runner=CommandRunner())
    group = ParallelStepGroup([ConfiguredStep("demo.empty_fail", {}, "0/0")], step_id="0")

    with pytest.raises(PipelineExecutionError) as exc_info:
        PipelineExecutor().run([group], ctx)

    lines = str(exc_info.value).splitlines()
    assert lines[0] == "Pipeline failed at step 0/0 (demo.empty_fail)."
    assert lines[1] == "ValueError"


def test_sequential_command_failure_uses_structured_command_and_exit_code(tmp_path):
    ctx = PipelineContext(cwd=tmp_path, env={}, runner=CommandRunner())
    step = CommandFailStep(
        command=["flutter", "build", "ipa", "--profile=test"],
        exit_code=74,
        cause="iOS IPA build failed. Check the Flutter/Xcode output above for details.",
    )

    with pytest.raises(PipelineExecutionError) as exc_info:
        PipelineExecutor().run([step], ctx)

    message = str(exc_info.value)
    lines = message.splitlines()
    assert lines[0] == "Pipeline failed at step demo.command_fail."
    assert lines[1] == "iOS IPA build failed. Check the Flutter/Xcode output above for details."
    assert "Command: flutter build ipa --profile=test" in lines
    assert "Exit code: 74" in lines
    assert "configured/option.sh" not in message


def test_parallel_command_failure_uses_structured_command_and_exit_code(tmp_path):
    ctx = PipelineContext(cwd=tmp_path, env={}, runner=CommandRunner())
    failing = CommandFailStep(command=["flutter", "build", "ipa"], exit_code=1)
    failing.step_id = "0/0"
    healthy = OkStep()
    healthy.step_id = "0/1"

    with pytest.raises(PipelineExecutionError) as exc_info:
        PipelineExecutor().run([ParallelStepGroup([failing, healthy], step_id="0")], ctx)

    message = str(exc_info.value)
    lines = message.splitlines()
    assert lines[0] == "Pipeline failed at step 0/0 (demo.command_fail)."
    assert "Command: flutter build ipa" in lines
    assert "Exit code: 1" in lines
    assert "Other parallel steps were allowed to finish." in lines


def test_parallel_command_arguments_are_redacted(tmp_path):
    ctx = PipelineContext(cwd=tmp_path, env={"API_TOKEN": "provider-secret"}, runner=CommandRunner())
    failing = CommandFailStep(
        command=["flutter", "build", "ipa", "--dart-define=API_TOKEN=provider-secret"],
        exit_code=1,
    )
    failing.step_id = "0/0"

    with pytest.raises(PipelineExecutionError) as exc_info:
        PipelineExecutor().run([ParallelStepGroup([failing], step_id="0")], ctx)

    message = str(exc_info.value)
    assert "provider-secret" not in message
    assert "--dart-define=API_TOKEN=***" in message
