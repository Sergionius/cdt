import os
import sys

import pytest
import typer

from cdt.pipeline import PipelineContext, PipelineExecutor
from cdt.pipeline.builtins import register_builtin_steps
from cdt.pipeline.config import (
    ConfiguredStep,
    InputSpec,
    ParallelSpec,
    PipelineSpec,
    SequenceSpec,
    configured_steps,
    load_pipeline_config,
    load_plugins,
    parse_pipeline_inputs,
    validate_pipeline_inputs,
)
from cdt.pipeline.policy import RetryPolicy
from cdt.pipeline.registry import _clear_steps_for_tests
from cdt.pipeline.validation import validate_pipeline
from cdt.runner import CommandRunner
from cdt.sdk import step as sdk_step


@pytest.mark.parametrize(
    "when,inputs,expected",
    [
        ({"input": "deploy", "equals": "yes"}, {"deploy": "yes"}, "run"),
        ({"input": "deploy", "equals": "yes"}, {"deploy": "YES"}, "skip"),
        ({"input": "deploy", "equals": ""}, {}, "skip"),
        ({"input": "deploy", "equals": ""}, {"deploy": ""}, "run"),
        ({"input": "deploy", "not_equals": "yes"}, {}, "run"),
        ({"input": "deploy", "not_equals": "yes"}, {"deploy": "yes"}, "skip"),
        ({"input": "deploy", "present": True}, {}, "skip"),
        ({"input": "deploy", "present": True}, {"deploy": ""}, "skip"),
        ({"input": "deploy", "present": True}, {"deploy": " "}, "run"),
        ({"input": "deploy", "present": False}, {}, "run"),
        ({"input": "deploy", "present": False}, {"deploy": "yes"}, "skip"),
        ({"input": "deploy", "present": False}, None, "unknown"),
        (None, None, "run"),
    ],
)
def test_condition_evaluation(when, inputs, expected):
    from cdt.pipeline.config import evaluate_condition

    assert evaluate_condition(when, inputs) == expected


@pytest.mark.parametrize(
    "condition",
    [
        "null",
        "[]",
        "{input: deploy}",
        "{equals: yes}",
        "{input: deploy, equals: 'yes', present: true}",
        "{input: deploy, equals: true}",
        "{input: deploy, not_equals: 1}",
        "{input: deploy, present: 'true'}",
        "{input: deploy, unknown: 'yes'}",
        "{input: '${inputs.deploy}', present: true}",
        "{input: deploy, equals: '${ENV}'}",
    ],
)
def test_invalid_condition_rejected(tmp_path, condition):
    (tmp_path / "cdt.yaml").write_text(
        "version: 1\npipelines:\n  demo:\n    inputs: {deploy: {}}\n    steps:\n"
        f"      - step: flutter.pub_get\n        when: {condition}\n"
    )
    with pytest.raises(typer.BadParameter, match="when"):
        load_pipeline_config(tmp_path)


def test_extended_step_preserves_legacy_forms_and_ids(tmp_path):
    (tmp_path / "cdt.yaml").write_text(
        "version: 1\npipelines:\n  demo:\n    inputs: {deploy: {}}\n    steps:\n"
        "      - flutter.pub_get\n      - flutter.pub_get: {}\n"
        "      - parallel:\n          steps:\n            - sequence:\n                steps:\n"
        "                  - step: flutter.pub_get\n                    with: {}\n"
        "                    when: {input: deploy, present: true}\n"
    )
    register_builtin_steps()
    config = load_pipeline_config(tmp_path)
    assert validate_pipeline(config) == []
    steps = configured_steps(config.pipelines["demo"])
    assert steps[0].options == steps[1].options == {}
    leaf = steps[2].steps[0].steps[0]
    assert leaf.step_id == "2/0/0"
    assert leaf.options == {}
    assert leaf.when == {"input": "deploy", "present": True}


@pytest.mark.parametrize("extra", ["typo: true", "retries: {}"])
def test_extended_step_rejects_unknown_fields(tmp_path, extra):
    (tmp_path / "cdt.yaml").write_text(
        f"version: 1\npipelines:\n  demo:\n    steps:\n      - step: flutter.pub_get\n        {extra}\n"
    )
    with pytest.raises(typer.BadParameter, match="unsupported extended"):
        load_pipeline_config(tmp_path)


def test_condition_requires_declared_input_and_keeps_option_validation(tmp_path):
    (tmp_path / "cdt.yaml").write_text(
        "version: 1\npipelines:\n  demo:\n    steps:\n"
        "      - step: flutter.pub_get\n        with: {typo: true}\n"
        "        when: {input: missing, present: false}\n"
    )
    register_builtin_steps()
    errors = validate_pipeline(load_pipeline_config(tmp_path))
    assert {error["code"] for error in errors} == {"invalid_condition", "unknown_step_option"}


def test_extended_step_parses_retry_policy_separately_from_options(tmp_path):
    @sdk_step("demo.transient", retry_safe=True)
    def transient(ctx):
        pass

    (tmp_path / "cdt.yaml").write_text(
        "version: 1\npipelines:\n  demo:\n    steps:\n"
        "      - step: demo.transient\n        with: {}\n"
        "        retry: {max_attempts: 3, delay_seconds: 1}\n"
        "      - parallel:\n          steps:\n            - sequence:\n                steps:\n"
        "                  - step: demo.transient\n                    retry: {max_attempts: 5, delay_seconds: 60}\n"
    )
    config = load_pipeline_config(tmp_path)
    steps = configured_steps(config.pipelines["demo"])
    assert steps[0].retry == RetryPolicy(max_attempts=3, delay_seconds=1.0)
    assert steps[0].options == {}
    assert steps[1].steps[0].steps[0].retry == RetryPolicy(max_attempts=5, delay_seconds=60.0)
    assert validate_pipeline(config) == []


def test_retry_defaults_keep_single_attempt_and_zero_delay(tmp_path):
    @sdk_step("demo.transient", retry_safe=True)
    def transient(ctx):
        pass

    (tmp_path / "cdt.yaml").write_text(
        "version: 1\npipelines:\n  demo:\n    steps:\n      - step: demo.transient\n        retry: {}\n"
    )
    config = load_pipeline_config(tmp_path)
    leaf = configured_steps(config.pipelines["demo"])[0]
    assert leaf.retry == RetryPolicy(max_attempts=1, delay_seconds=0.0)
    assert not leaf.retry.enabled
    assert validate_pipeline(config) == []


@pytest.mark.parametrize(
    "retry_block",
    [
        "{max_attempts: 0}",
        "{max_attempts: 6}",
        "{max_attempts: true}",
        "{max_attempts: '3'}",
        "{max_attempts: 1.5}",
        "{delay_seconds: -1}",
        "{delay_seconds: 61}",
        "{delay_seconds: true}",
        "{delay_seconds: '1'}",
        "{delay_seconds: .inf}",
        "{delay_seconds: .nan}",
        "{unknown: 1}",
        "3",
    ],
)
def test_invalid_retry_rejected(tmp_path, retry_block):
    (tmp_path / "cdt.yaml").write_text(
        f"version: 1\npipelines:\n  demo:\n    steps:\n      - step: flutter.pub_get\n        retry: {retry_block}\n"
    )
    with pytest.raises(typer.BadParameter, match="retry"):
        load_pipeline_config(tmp_path)


def test_retry_above_single_attempt_requires_explicit_capability(tmp_path):
    (tmp_path / "cdt.yaml").write_text(
        "version: 1\npipelines:\n  demo:\n    steps:\n      - step: flutter.pub_get\n        retry: {max_attempts: 2}\n"
    )
    register_builtin_steps()
    errors = validate_pipeline(load_pipeline_config(tmp_path))
    assert {error["code"] for error in errors} == {"retry_requires_capability"}
    assert "cannot be inferred from risk" in errors[0]["message"]


def test_retry_single_attempt_needs_no_capability(tmp_path):
    (tmp_path / "cdt.yaml").write_text(
        "version: 1\npipelines:\n  demo:\n    steps:\n"
        "      - step: flutter.pub_get\n        retry: {max_attempts: 1, delay_seconds: 2}\n"
    )
    register_builtin_steps()
    assert validate_pipeline(load_pipeline_config(tmp_path)) == []


def test_single_key_form_keeps_retry_out_of_constructor_options(tmp_path):
    (tmp_path / "cdt.yaml").write_text(
        "version: 1\npipelines:\n  demo:\n    steps:\n      - flutter.pub_get: {retry: {max_attempts: 2}}\n"
    )
    register_builtin_steps()
    config = load_pipeline_config(tmp_path)
    leaf = configured_steps(config.pipelines["demo"])[0]
    assert leaf.retry is None
    assert leaf.options == {"retry": {"max_attempts": 2}}
    errors = validate_pipeline(config)
    assert {error["code"] for error in errors} == {"unknown_step_option"}


def test_extended_step_parses_timeout_seconds_separately_from_options(tmp_path):
    register_builtin_steps()
    (tmp_path / "cdt.yaml").write_text(
        "version: 1\npipelines:\n  demo:\n    steps:\n"
        "      - step: hook.python_script\n        with: {script: hooks/x.py}\n        timeout_seconds: 12\n"
        "      - sequence:\n          steps:\n"
        "            - step: hook.python_script\n              with: {script: hooks/y.py}\n"
        "              timeout_seconds: 0.5\n"
    )
    config = load_pipeline_config(tmp_path)
    steps = configured_steps(config.pipelines["demo"])
    assert steps[0].timeout_seconds == 12.0
    assert steps[0].options == {"script": "hooks/x.py"}
    assert steps[1].steps[0].timeout_seconds == 0.5
    assert validate_pipeline(config) == []


@pytest.mark.parametrize(
    "timeout_value",
    [0, -1, "true", "'3'", ".inf", ".nan", "null", "{}"],
)
def test_invalid_timeout_seconds_rejected(tmp_path, timeout_value):
    register_builtin_steps()
    (tmp_path / "cdt.yaml").write_text(
        "version: 1\npipelines:\n  demo:\n    steps:\n"
        f"      - step: hook.python_script\n        timeout_seconds: {timeout_value}\n"
    )
    with pytest.raises(typer.BadParameter, match="timeout_seconds"):
        load_pipeline_config(tmp_path)


def test_timeout_seconds_requires_explicit_capability(tmp_path):
    register_builtin_steps()
    (tmp_path / "cdt.yaml").write_text(
        "version: 1\npipelines:\n  demo:\n    steps:\n      - step: flutter.pub_get\n        timeout_seconds: 30\n"
    )
    errors = validate_pipeline(load_pipeline_config(tmp_path))
    assert {error["code"] for error in errors} == {"timeout_requires_capability"}
    assert errors[0]["path"] == "pipelines.demo.steps[0].timeout_seconds"


def test_timeout_seconds_with_same_option_is_ambiguous(tmp_path):
    register_builtin_steps()
    (tmp_path / "cdt.yaml").write_text(
        "version: 1\npipelines:\n  demo:\n    steps:\n"
        "      - step: hook.python_script\n        with: {script: hooks/x.py, timeout: 5}\n"
        "        timeout_seconds: 10\n"
    )
    errors = validate_pipeline(load_pipeline_config(tmp_path))
    assert {error["code"] for error in errors} == {"ambiguous_step_timeout"}
    assert "remove one" in errors[0]["message"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups are required")
def test_timeout_seconds_supported_for_hook_without_with_timeout(tmp_path):
    register_builtin_steps()
    (tmp_path / "cdt.yaml").write_text(
        "version: 1\npipelines:\n  demo:\n    steps:\n"
        "      - step: hook.python_script\n        with: {script: hooks/x.py}\n        timeout_seconds: 10\n"
    )
    assert validate_pipeline(load_pipeline_config(tmp_path)) == []


def test_timeout_seconds_rejected_on_platform_without_process_groups(tmp_path, monkeypatch):
    from cdt.pipeline import validation

    register_builtin_steps()
    (tmp_path / "cdt.yaml").write_text(
        "version: 1\npipelines:\n  demo:\n    steps:\n      - step: hook.python_script\n        timeout_seconds: 10\n"
    )
    monkeypatch.setattr(validation, "supports_process_groups", lambda: False)
    errors = validate_pipeline(load_pipeline_config(tmp_path))
    assert {error["code"] for error in errors} == {"timeout_unsupported_platform"}


def test_single_key_form_keeps_timeout_in_constructor_options(tmp_path):
    register_builtin_steps()
    (tmp_path / "cdt.yaml").write_text(
        "version: 1\npipelines:\n  demo:\n    steps:\n      - hook.python_script: {script: hooks/x.py, timeout: 5}\n"
    )
    config = load_pipeline_config(tmp_path)
    leaf = configured_steps(config.pipelines["demo"])[0]
    assert leaf.timeout_seconds is None
    assert leaf.options == {"script": "hooks/x.py", "timeout": 5}
    assert validate_pipeline(config) == []


def _record_timeout_step():
    received = []

    @sdk_step("demo.timed", timeout_option="timeout")
    def timed(ctx, timeout=None) -> None:
        received.append(timeout)

    return received


def _make_context(tmp_path):
    return PipelineContext(cwd=tmp_path, env={}, runner=CommandRunner())


def test_configured_step_injects_timeout_into_declared_option(tmp_path):
    received = _record_timeout_step()
    leaf = ConfiguredStep("demo.timed", {}, "0", None, None, 7)

    leaf.run(_make_context(tmp_path))

    assert received == [7]


def test_configured_step_rejects_timeout_without_capability_at_runtime(tmp_path):
    @sdk_step("demo.untimed")
    def untimed(ctx) -> None:
        pass

    leaf = ConfiguredStep("demo.untimed", {}, "0", None, None, 7)
    with pytest.raises(typer.BadParameter, match="does not declare a native timeout parameter"):
        leaf.run(_make_context(tmp_path))


def test_configured_step_rejects_ambiguous_timeout_at_runtime(tmp_path):
    _record_timeout_step()
    leaf = ConfiguredStep("demo.timed", {"timeout": 5}, "0", None, None, 7)
    with pytest.raises(typer.BadParameter, match="both timeout_seconds and with.timeout"):
        leaf.run(_make_context(tmp_path))


def test_configured_step_rejects_timeout_on_unsupported_platform_at_runtime(tmp_path, monkeypatch):
    from cdt.pipeline import config as pipeline_config

    _record_timeout_step()
    monkeypatch.setattr(pipeline_config, "supports_process_groups", lambda: False)
    leaf = ConfiguredStep("demo.timed", {}, "0", None, None, 7)
    with pytest.raises(typer.BadParameter, match="POSIX process groups"):
        leaf.run(_make_context(tmp_path))


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


def test_pipeline_inputs_declaration_is_parsed(tmp_path):
    (tmp_path / "cdt.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "pipelines:",
                "  demo:",
                "    inputs:",
                "      version:",
                "        required: true",
                "        pattern: '^\\d+\\.\\d+\\.\\d+$'",
                "      channel:",
                "    steps: []",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    config = load_pipeline_config(tmp_path)

    inputs = config.pipelines["demo"].inputs
    assert set(inputs) == {"version", "channel"}
    assert inputs["version"] == InputSpec(name="version", required=True, pattern=r"^\d+\.\d+\.\d+$")
    assert inputs["channel"] == InputSpec(name="channel")
    assert validate_pipeline(config, "demo") == []


def test_pipeline_inputs_reject_unknown_fields(tmp_path):
    (tmp_path / "cdt.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "pipelines:",
                "  demo:",
                "    inputs:",
                "      version:",
                "        required: true",
                "        secret: true",
                "    steps: []",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(typer.BadParameter, match="input 'version' has unsupported fields: secret"):
        load_pipeline_config(tmp_path)


def test_pipeline_inputs_reject_invalid_names(tmp_path):
    for name in ("1version", "bad name", "v.ersion"):
        (tmp_path / "cdt.yaml").write_text(
            f"version: 1\npipelines:\n  demo:\n    inputs:\n      {name}:\n    steps: []\n",
            encoding="utf-8",
        )

        with pytest.raises(typer.BadParameter, match="input name.*is invalid"):
            load_pipeline_config(tmp_path)


def test_pipeline_inputs_reject_non_boolean_required(tmp_path):
    (tmp_path / "cdt.yaml").write_text(
        "version: 1\npipelines:\n  demo:\n    inputs:\n      version:\n        required: always\n    steps: []\n",
        encoding="utf-8",
    )

    with pytest.raises(typer.BadParameter, match="input 'version' required must be a boolean"):
        load_pipeline_config(tmp_path)


def test_pipeline_inputs_reject_non_string_pattern(tmp_path):
    (tmp_path / "cdt.yaml").write_text(
        "version: 1\npipelines:\n  demo:\n    inputs:\n      version:\n        pattern: 5\n    steps: []\n",
        encoding="utf-8",
    )

    with pytest.raises(typer.BadParameter, match="input 'version' pattern must be a non-empty string"):
        load_pipeline_config(tmp_path)


def test_pipeline_inputs_reject_invalid_regex_pattern(tmp_path):
    (tmp_path / "cdt.yaml").write_text(
        "version: 1\npipelines:\n  demo:\n    inputs:\n      version:\n        pattern: '['\n    steps: []\n",
        encoding="utf-8",
    )

    with pytest.raises(typer.BadParameter, match="input 'version' pattern is not a valid regex"):
        load_pipeline_config(tmp_path)


def test_pipeline_inputs_must_be_mapping(tmp_path):
    (tmp_path / "cdt.yaml").write_text(
        "version: 1\npipelines:\n  demo:\n    inputs:\n      - version\n    steps: []\n",
        encoding="utf-8",
    )

    with pytest.raises(typer.BadParameter, match="inputs must be a mapping"):
        load_pipeline_config(tmp_path)


def test_parse_pipeline_inputs_preserves_order_and_values():
    inputs = parse_pipeline_inputs(["version=0.5.2", "channel=beta"])

    assert inputs == {"version": "0.5.2", "channel": "beta"}
    assert list(inputs) == ["version", "channel"]


def test_parse_pipeline_inputs_rejects_malformed_entries():
    with pytest.raises(typer.BadParameter, match="Use --input KEY=VALUE"):
        parse_pipeline_inputs(["version"])
    with pytest.raises(typer.BadParameter, match="key must not be empty"):
        parse_pipeline_inputs(["=0.5.2"])
    with pytest.raises(typer.BadParameter, match="Duplicate --input key: version"):
        parse_pipeline_inputs(["version=1", "version=2"])


def test_validate_pipeline_inputs_rejects_unknown_missing_and_pattern_mismatch():
    pipeline = PipelineSpec(
        name="demo",
        steps=[],
        inputs={
            "version": InputSpec(name="version", required=True, pattern=r"\d+\.\d+\.\d+"),
            "channel": InputSpec(name="channel"),
        },
    )

    with pytest.raises(
        typer.BadParameter,
        match="Unknown pipeline input for 'demo': oops. Declared inputs: channel, version",
    ):
        validate_pipeline_inputs(pipeline, {"oops": "1"})
    with pytest.raises(typer.BadParameter, match=r"Missing required pipeline input\(s\) for 'demo': version"):
        validate_pipeline_inputs(pipeline, {})
    with pytest.raises(typer.BadParameter, match="input 'version' does not match pattern"):
        validate_pipeline_inputs(pipeline, {"version": "abc"})
    validate_pipeline_inputs(pipeline, {"version": "0.5.2"})


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


def test_pipeline_risk_defaults_to_standard_and_accepts_production(tmp_path):
    (tmp_path / "cdt.yaml").write_text(
        "version: 1\npipelines:\n  test:\n    steps: []\n  prod:\n    risk: production\n    steps: []\n",
        encoding="utf-8",
    )

    config = load_pipeline_config(tmp_path)

    assert config.pipelines["test"].risk == "standard"
    assert config.pipelines["prod"].risk == "production"


def test_pipeline_rejects_unknown_risk(tmp_path):
    (tmp_path / "cdt.yaml").write_text(
        "version: 1\npipelines:\n  demo:\n    risk: dangerous\n    steps: []\n",
        encoding="utf-8",
    )

    with pytest.raises(typer.BadParameter, match="standard.*production"):
        load_pipeline_config(tmp_path)


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


def test_parallel_sequence_group_parses_with_stable_nested_steps(tmp_path):
    (tmp_path / "cdt.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "pipelines:",
                "  prod:",
                "    steps:",
                "      - parallel:",
                "          steps:",
                "            - demo.ios",
                "            - sequence:",
                "                steps:",
                "                  - demo.aab",
                "                  - demo.apk",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    config = load_pipeline_config(tmp_path)
    group = config.pipelines["prod"].steps[0]

    assert isinstance(group, ParallelSpec)
    assert isinstance(group.steps[1], SequenceSpec)
    assert [step.name for step in group.steps[1].steps] == ["demo.aab", "demo.apk"]
    configured = configured_steps(config.pipelines["prod"])[0]
    assert configured.step_id == "0"
    assert configured.steps[1].step_id == "0/1"
    assert [step.step_id for step in configured.steps[1].steps] == ["0/1/0", "0/1/1"]


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


def _google_play_pipeline(risk: str, steps_block: str) -> str:
    return f"version: 1\npipelines:\n  play:\n    risk: {risk}\n    steps:\n{steps_block}"


def _play_step_yaml(indent: str = "      ") -> str:
    return (
        f"{indent}- google_play.upload_aab:\n"
        f"{indent}    artifact: aab\n"
        f"{indent}    package_name: com.example.app\n"
        f"{indent}    track: internal\n"
        f"{indent}    release_status: draft\n"
    )


def _submit_review_pipeline(risk: str, steps_block: str) -> str:
    return f"version: 1\npipelines:\n  submit:\n    risk: {risk}\n    steps:\n{steps_block}"


def _submit_review_step_yaml(indent: str = "      ") -> str:
    return (
        f"{indent}- appstore.submit_review:\n"
        f"{indent}    whats_new:\n"
        f"{indent}      ru: Исправления и улучшения\n"
        f"{indent}    release_mode: manual\n"
        f"{indent}    phased_release: true\n"
    )


@pytest.mark.parametrize(
    "steps_block,expected_path",
    [
        (_play_step_yaml(), "pipelines.play.steps[0]"),
        (
            "      - sequence:\n" + "          steps:\n" + _play_step_yaml(indent="            "),
            "pipelines.play.steps[0].sequence.steps[0]",
        ),
        (
            "      - parallel:\n" + "          steps:\n" + _play_step_yaml(indent="            "),
            "pipelines.play.steps[0].parallel.steps[0]",
        ),
        (
            "      - parallel:\n"
            + "          steps:\n"
            + "            - sequence:\n"
            + "                steps:\n"
            + _play_step_yaml(indent="                  "),
            "pipelines.play.steps[0].parallel.steps[0].sequence.steps[0]",
        ),
    ],
)
@pytest.mark.parametrize("track", ["internal", "${inputs.track}"])
def test_google_play_step_requires_production_risk_recursively(tmp_path, steps_block, expected_path, track):
    (tmp_path / "cdt.yaml").write_text(
        _google_play_pipeline("standard", steps_block.replace("track: internal", f"track: {track}")),
        encoding="utf-8",
    )
    register_builtin_steps()
    config = load_pipeline_config(tmp_path)

    errors = validate_pipeline(config, "play")

    assert errors == [
        {
            "code": "production_risk_required",
            "message": (
                "Step google_play.upload_aab publishes to Google Play and requires pipeline risk: "
                "production (declared risk: 'standard')."
            ),
            "path": expected_path,
        }
    ]


@pytest.mark.parametrize(
    "steps_block",
    [
        _play_step_yaml(),
        "      - sequence:\n" + "          steps:\n" + _play_step_yaml(indent="            "),
        "      - parallel:\n" + "          steps:\n" + _play_step_yaml(indent="            "),
    ],
)
def test_google_play_step_passes_validation_under_production_risk(tmp_path, steps_block):
    (tmp_path / "cdt.yaml").write_text(_google_play_pipeline("production", steps_block), encoding="utf-8")
    register_builtin_steps()
    config = load_pipeline_config(tmp_path)

    assert validate_pipeline(config, "play") == []


def test_non_google_play_steps_do_not_require_production_risk(tmp_path):
    (tmp_path / "cdt.yaml").write_text(
        "version: 1\npipelines:\n  demo:\n    steps:\n      - flutter.pub_get\n",
        encoding="utf-8",
    )
    register_builtin_steps()
    config = load_pipeline_config(tmp_path)

    assert validate_pipeline(config, "demo") == []


@pytest.mark.parametrize(
    "steps_block,expected_path",
    [
        (_submit_review_step_yaml(), "pipelines.submit.steps[0]"),
        (
            "      - sequence:\n" + "          steps:\n" + _submit_review_step_yaml(indent="            "),
            "pipelines.submit.steps[0].sequence.steps[0]",
        ),
        (
            "      - parallel:\n" + "          steps:\n" + _submit_review_step_yaml(indent="            "),
            "pipelines.submit.steps[0].parallel.steps[0]",
        ),
        (
            "      - parallel:\n"
            + "          steps:\n"
            + "            - sequence:\n"
            + "                steps:\n"
            + _submit_review_step_yaml(indent="                  "),
            "pipelines.submit.steps[0].parallel.steps[0].sequence.steps[0]",
        ),
    ],
)
def test_submit_review_step_requires_production_risk_recursively(tmp_path, steps_block, expected_path):
    (tmp_path / "cdt.yaml").write_text(_submit_review_pipeline("standard", steps_block), encoding="utf-8")
    register_builtin_steps()
    config = load_pipeline_config(tmp_path)

    errors = validate_pipeline(config, "submit")

    assert errors == [
        {
            "code": "production_risk_required",
            "message": (
                "Step appstore.submit_review submits an app for App Store review and requires pipeline risk: "
                "production (declared risk: 'standard')."
            ),
            "path": expected_path,
        }
    ]


@pytest.mark.parametrize(
    "steps_block",
    [
        _submit_review_step_yaml(),
        "      - sequence:\n" + "          steps:\n" + _submit_review_step_yaml(indent="            "),
        "      - parallel:\n" + "          steps:\n" + _submit_review_step_yaml(indent="            "),
    ],
)
def test_submit_review_step_passes_validation_under_production_risk(tmp_path, steps_block):
    (tmp_path / "cdt.yaml").write_text(_submit_review_pipeline("production", steps_block), encoding="utf-8")
    register_builtin_steps()
    config = load_pipeline_config(tmp_path)

    assert validate_pipeline(config, "submit") == []


def test_testflight_steps_still_pass_validation_without_production_risk(tmp_path):
    (tmp_path / "cdt.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "pipelines:",
                "  test:",
                "    steps:",
                "      - appstore.upload_testflight_ipa",
                "      - appstore.complete_testflight:",
                "          changelog: dev build",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    register_builtin_steps()
    config = load_pipeline_config(tmp_path)

    assert validate_pipeline(config, "test") == []
