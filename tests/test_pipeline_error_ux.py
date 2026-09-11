import json
import re
import sys
import threading
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from cdt.cli import app
from cdt.pipeline import ParallelStepGroup, PipelineContext, PipelineExecutor
from cdt.pipeline.config import ConfiguredStep, load_pipeline_config
from cdt.pipeline.executor import PipelineExecutionError
from cdt.pipeline.registry import _clear_steps_for_tests, register_step
from cdt.runner import CommandExecutionError, CommandRunner
from cdt.runs import list_runs, run_paths


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


cli_runner = CliRunner()


class _SyncFakeRunner:
    """Fake command runner: the iOS build fails first, the sibling waits for that failure."""

    def __init__(self, ios_exit_code: int = 74):
        self.ios_exit_code = ios_exit_code
        self.ios_failed = threading.Event()
        self.commands: list[list[str]] = []
        self._lock = threading.Lock()

    def run(self, command: list[str], *, cwd: Path) -> int:
        with self._lock:
            self.commands.append(list(command))
        if command[:3] == ["flutter", "build", "ipa"]:
            self.ios_failed.set()
            return self.ios_exit_code
        assert self.ios_failed.wait(timeout=10), "sibling finished before the iOS failure"
        return 0


class _OkFakeRunner:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def run(self, command: list[str], *, cwd: Path) -> int:
        self.commands.append(list(command))
        return 0


def setup_function():
    _clear_steps_for_tests()
    sys.modules.pop("cdt_steps.demo", None)
    sys.modules.pop("cdt_steps", None)


def teardown_function():
    _clear_steps_for_tests()
    sys.modules.pop("cdt_steps.demo", None)
    sys.modules.pop("cdt_steps", None)


def _write_ios_cli_project(tmp_path: Path) -> None:
    aab = tmp_path / "build" / "app" / "outputs" / "bundle" / "release" / "app.aab"
    aab.parent.mkdir(parents=True)
    aab.write_text("aab", encoding="utf-8")
    ipa = tmp_path / "build" / "ios" / "ipa" / "Runner.ipa"
    ipa.parent.mkdir(parents=True)
    ipa.write_text("ipa", encoding="utf-8")
    (tmp_path / "cdt.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "pipelines:",
                "  iosapp:",
                "    steps:",
                "      - parallel:",
                "          steps:",
                "            - ios.flutter_build_ipa",
                "            - android.build_aab:",
                "                artifact: android_aab",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _write_secret_cli_project(tmp_path: Path) -> None:
    package = tmp_path / "cdt_steps"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "demo.py").write_text(
        "\n".join(
            [
                "import typer",
                "from cdt.sdk import step",
                "",
                "@step('demo.secret_fail')",
                "def secret_fail(ctx):",
                "    token = ctx.env['API_TOKEN']",
                "    raise typer.BadParameter('provider rejected ' + token)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("API_TOKEN=provider-secret-value\n", encoding="utf-8")
    (tmp_path / "cdt.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "plugins:",
                "  - cdt_steps.demo",
                "pipelines:",
                "  iosapp:",
                "    steps:",
                "      - parallel:",
                "          steps:",
                "            - ios.flutter_build_ipa:",
                "                dart_defines:",
                "                  - API_TOKEN=provider-secret-value",
                "            - demo.secret_fail",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


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


def test_cli_parallel_ios_failure_renders_readable_summary(tmp_path, monkeypatch):
    _write_ios_cli_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    sounds: list[str] = []
    monkeypatch.setattr("cdt.steps.ios._play_fail_sound", lambda env, cwd: sounds.append("fail"))
    monkeypatch.setattr("cdt.steps.android._play_fail_sound", lambda env, cwd: sounds.append("fail"))
    fake_runner = _SyncFakeRunner(ios_exit_code=74)
    monkeypatch.setattr("cdt.pipeline.runner.CommandRunner", lambda: fake_runner)

    result = cli_runner.invoke(app, ["run", "iosapp"])

    assert result.exit_code == 1
    output = result.output
    assert "Pipeline failed at step 0/0 (ios.flutter_build_ipa)." in output
    assert "iOS IPA build failed. Check the Flutter/Xcode output above for details." in output
    assert "Command: flutter build ipa --obfuscate --split-debug-info=obfsymbols --no-pub" in output
    assert "Exit code: 74" in output
    assert "Other parallel steps were allowed to finish." in output
    assert "Artifacts produced: android_aab" in output
    assert "Invalid value" not in output
    assert "Usage:" not in output
    assert "Parallel group failed after all steps finished" not in output
    assert "Failed step:" not in output
    assert "unknown" not in output
    assert "not applicable" not in output
    assert sounds == ["fail"]

    runs = list_runs(tmp_path)
    assert len(runs) == 1
    assert runs[0]["status"] == "failed"
    paths = run_paths(tmp_path, runs[0]["run_id"])
    payload = json.loads(paths.status.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["failed_step"] == "0/0"
    assert payload["parallel_completed"] == ["0/1"]
    assert payload["completed_steps"] == ["0/1"]
    assert [artifact["name"] for artifact in payload["artifacts"]] == ["android_aab"]
    assert paths.exit.read_text(encoding="utf-8") == "1\n"
    log = paths.log.read_text(encoding="utf-8")
    assert log.strip() != ""
    assert "Pipeline failed at step 0/0 (ios.flutter_build_ipa)." in log
    assert "Exit code: 74" in log


def test_cli_run_without_config_keeps_validation_ux(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = cli_runner.invoke(app, ["run", "anything"])

    assert result.exit_code != 0
    assert "Invalid value" in result.output
    assert "Pipeline config not found" in result.output
    assert "Usage:" in result.output


def test_cli_run_unknown_pipeline_keeps_validation_ux(tmp_path, monkeypatch):
    _write_ios_cli_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = cli_runner.invoke(app, ["run", "missing"])

    assert result.exit_code != 0
    assert "Invalid value" in result.output
    assert "Unknown pipeline: missing" in result.output
    assert "Usage:" in result.output


def test_cli_successful_run_keeps_existing_output(tmp_path, monkeypatch):
    _write_ios_cli_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("cdt.pipeline.runner.CommandRunner", lambda: _OkFakeRunner())

    result = cli_runner.invoke(app, ["run", "iosapp"])

    assert result.exit_code == 0
    assert re.fullmatch(r"Run: \S+\n", result.output)
    assert "Invalid value" not in result.output
    assert "Usage:" not in result.output


def test_cli_ios_failure_redacts_secret_from_terminal_status_and_log(tmp_path, monkeypatch):
    _write_secret_cli_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr("cdt.steps.ios._play_fail_sound", lambda env, cwd: None)
    monkeypatch.setattr("cdt.steps.android._play_fail_sound", lambda env, cwd: None)
    fake_runner = _SyncFakeRunner(ios_exit_code=1)
    monkeypatch.setattr("cdt.pipeline.runner.CommandRunner", lambda: fake_runner)

    result = cli_runner.invoke(app, ["run", "iosapp"])

    assert result.exit_code == 1
    assert "provider-secret-value" not in result.output
    assert "--dart-define=API_TOKEN=***" in result.output
    assert "provider rejected ***" in result.output

    runs = list_runs(tmp_path)
    assert runs[0]["status"] == "failed"
    paths = run_paths(tmp_path, runs[0]["run_id"])
    status_json = paths.status.read_text(encoding="utf-8")
    log = paths.log.read_text(encoding="utf-8")
    assert "provider-secret-value" not in status_json
    assert "--dart-define=API_TOKEN=***" in status_json
    assert "provider-secret-value" not in log
    assert "--dart-define=API_TOKEN=***" in log
    assert "provider rejected ***" in log
