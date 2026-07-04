import sys

import pytest
import typer

from cdt.pipeline import PipelineContext, PipelineExecutor
from cdt.pipeline.builtins import register_builtin_steps
from cdt.pipeline.config import ParallelSpec, configured_steps, load_pipeline_config, load_plugins
from cdt.pipeline.registry import _clear_steps_for_tests
from cdt.pipeline.validation import validate_pipeline
from cdt.runner import CommandRunner


def setup_function():
    _clear_steps_for_tests()


def teardown_function():
    _clear_steps_for_tests()


class RecordingRunner:
    def __init__(self):
        self.runs: list[tuple[list[str], object]] = []

    def run(self, cmd: list[str], *, cwd):
        self.runs.append((cmd, cwd))
        return 0


def test_yaml_plugin_function_step_runs_with_interpolated_options(tmp_path, monkeypatch):
    package = tmp_path / "cdt_steps"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "offline.py").write_text(
        "\n".join(
            [
                "from cdt.sdk import step",
                "",
                "@step('offline.fetch_config')",
                "def fetch_config(ctx, output: str):",
                "    ctx.values['offline_config_path'] = str(ctx.project_path(output))",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "cdt.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "plugins:",
                "  - cdt_steps.offline",
                "pipelines:",
                "  offline-test:",
                "    steps:",
                "      - offline.fetch_config:",
                "          output: ${OFFLINE_OUTPUT}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("cdt_steps.offline", None)

    config = load_pipeline_config(tmp_path)
    load_plugins(config.plugins)
    ctx = PipelineContext(
        cwd=tmp_path,
        env={"OFFLINE_OUTPUT": "assets/offline/config.json"},
        runner=CommandRunner(),
    )

    PipelineExecutor().run(configured_steps(config.pipelines["offline-test"]), ctx)

    assert ctx.values["offline_config_path"] == str(tmp_path / "assets" / "offline" / "config.json")


def test_runtime_interpolation_can_read_values_set_by_previous_step(tmp_path):
    from cdt.sdk import step

    events: list[str] = []

    @step("demo.produce")
    def produce(ctx):
        ctx.values["message"] = "done"

    @step("demo.consume")
    def consume(ctx, message: str):
        events.append(message)

    (tmp_path / "cdt.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "pipelines:",
                "  demo:",
                "    steps:",
                "      - demo.produce",
                "      - demo.consume:",
                "          message: ${values.message}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    config = load_pipeline_config(tmp_path)
    ctx = PipelineContext(cwd=tmp_path, env={}, runner=CommandRunner())

    PipelineExecutor().run(configured_steps(config.pipelines["demo"]), ctx)

    assert events == ["done"]


def test_parallel_group_parses_into_explicit_model(tmp_path):
    (tmp_path / "cdt.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "pipelines:",
                "  demo:",
                "    steps:",
                "      - parallel:",
                "          steps:",
                "            - demo.first",
                "            - demo.second:",
                "                value: ok",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    config = load_pipeline_config(tmp_path)
    group = config.pipelines["demo"].steps[0]

    assert isinstance(group, ParallelSpec)
    assert [step.name for step in group.steps] == ["demo.first", "demo.second"]
    assert group.steps[1].options == {"value": "ok"}


def test_nested_parallel_group_errors_clearly(tmp_path):
    (tmp_path / "cdt.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "pipelines:",
                "  demo:",
                "    steps:",
                "      - parallel:",
                "          steps:",
                "            - parallel:",
                "                steps:",
                "                  - demo.first",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(typer.BadParameter, match="nested parallel"):
        load_pipeline_config(tmp_path)


def test_invalid_parallel_shape_errors_clearly(tmp_path):
    (tmp_path / "cdt.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "pipelines:",
                "  demo:",
                "    steps:",
                "      - parallel:",
                "          items:",
                "            - demo.first",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(typer.BadParameter, match="only the steps key"):
        load_pipeline_config(tmp_path)


def test_yaml_flutter_build_options_are_passed_to_step(tmp_path):
    (tmp_path / "pubspec.yaml").write_text("name: app\nversion: 1.0.0+1\n", encoding="utf-8")
    aab = tmp_path / "build" / "app" / "outputs" / "bundle" / "release" / "app-release.aab"
    aab.parent.mkdir(parents=True)
    aab.write_text("aab", encoding="utf-8")
    (tmp_path / "cdt.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "pipelines:",
                "  demo:",
                "    steps:",
                "      - android.build_aab:",
                "          profile: qa",
                "          dart_defines:",
                "            API: mock",
                "          flavor: qa",
                "          target: lib/main_qa.dart",
                "          obfuscate: false",
                "          split_debug_info:",
                "          no_shrink: false",
                "          no_pub: false",
                "          extra_args:",
                "            - --build-name=1.2.3",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    register_builtin_steps()
    config = load_pipeline_config(tmp_path)
    runner = RecordingRunner()
    ctx = PipelineContext(cwd=tmp_path, env={}, runner=runner)

    PipelineExecutor().run(configured_steps(config.pipelines["demo"]), ctx)

    assert runner.runs == [
        (
            [
                "flutter",
                "build",
                "appbundle",
                "--flavor",
                "qa",
                "--target",
                "lib/main_qa.dart",
                "--dart-define=ENV=qa",
                "--dart-define=API=mock",
                "--build-name=1.2.3",
            ],
            tmp_path,
        )
    ]


def test_build_step_env_option_is_rejected_with_profile_hint(tmp_path):
    (tmp_path / "cdt.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "pipelines:",
                "  demo:",
                "    steps:",
                "      - android.build_aab:",
                "          env: prod",
                "      - android.build_apk:",
                "          env: prod",
                "      - ios.flutter_build_ipa:",
                "          env: test",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    register_builtin_steps()
    config = load_pipeline_config(tmp_path)

    errors = validate_pipeline(config, "demo")

    assert errors == [
        {
            "code": "unknown_step_option",
            "message": "Unknown option 'env' for step android.build_aab. Use 'profile' instead.",
            "path": "pipelines.demo.steps[0].env",
        },
        {
            "code": "unknown_step_option",
            "message": "Unknown option 'env' for step android.build_apk. Use 'profile' instead.",
            "path": "pipelines.demo.steps[1].env",
        },
        {
            "code": "unknown_step_option",
            "message": "Unknown option 'env' for step ios.flutter_build_ipa. Use 'profile' instead.",
            "path": "pipelines.demo.steps[2].env",
        },
    ]
