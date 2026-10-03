import json
import sys
import threading
import time

import pytest
import typer
from typer.testing import CliRunner

from cdt.artifacts import ArtifactKind, BuildArtifact
from cdt.cli import app
from cdt.pipeline import ParallelStepGroup, PipelineContext, PipelineExecutor, SequentialStepGroup
from cdt.pipeline.config import ConfiguredStep
from cdt.pipeline.executor import PipelineExecutionError
from cdt.pipeline.policy import RetryableStepError, RetryPolicy
from cdt.pipeline.registry import StepMetadata, _clear_steps_for_tests, register_step
from cdt.runner import CommandExecutionError, CommandRunner
from cdt.sdk import step as sdk_step
from cdt.services import webhook

runner = CliRunner()


def setup_function():
    _clear_steps_for_tests()
    sys.modules.pop("cdt_steps.demo", None)
    sys.modules.pop("cdt_steps", None)


def teardown_function():
    _clear_steps_for_tests()
    sys.modules.pop("cdt_steps.demo", None)
    sys.modules.pop("cdt_steps", None)


def _register_flaky_step(calls, *, fail_times, error="transient", name="demo.flaky"):
    """SDK fixture: raises RetryableStepError for the first `fail_times` attempts."""

    @sdk_step(name, retry_safe=True)
    def flaky(ctx):
        calls.append("run")
        if len(calls) <= fail_times:
            raise RetryableStepError(error)

    return flaky


def test_retry_safe_step_retries_until_success(tmp_path, monkeypatch):
    calls = []
    sleeps = []
    monkeypatch.setattr("cdt.pipeline.config.time.sleep", lambda s: sleeps.append(s))
    _register_flaky_step(calls, fail_times=1)
    ctx = PipelineContext(cwd=tmp_path, env={}, runner=CommandRunner())
    leaf = ConfiguredStep("demo.flaky", {}, "0", None, RetryPolicy(max_attempts=3, delay_seconds=1.5))

    PipelineExecutor().run([leaf], ctx)

    assert calls == ["run", "run"]
    assert sleeps == [1.5]
    assert ctx.completed_steps == ["0"]
    assert "0" not in ctx.running_steps
    assert ctx.step_attempts == {"0": {"attempts": 1, "last_error": "transient"}}


def test_each_retry_constructs_a_fresh_runtime_instance(tmp_path):
    constructions = []

    class CountingStep:
        name = "counting"

        def __init__(self):
            constructions.append(object())

        def run(self, ctx):
            if len(constructions) == 1:
                raise RetryableStepError("transient")

    register_step("demo.counting", CountingStep, metadata=StepMetadata(name="demo.counting", retry_safe=True))
    ctx = PipelineContext(cwd=tmp_path, env={}, runner=CommandRunner())
    leaf = ConfiguredStep("demo.counting", {}, "0", None, RetryPolicy(max_attempts=3, delay_seconds=0))

    PipelineExecutor().run([leaf], ctx)

    assert len(constructions) == 2
    assert constructions[0] is not constructions[1]
    assert ctx.completed_steps == ["0"]


def test_retry_attempts_are_bounded_and_exhaustion_is_terminal(tmp_path):
    calls = []
    _register_flaky_step(calls, fail_times=99)
    ctx = PipelineContext(cwd=tmp_path, env={}, runner=CommandRunner())
    leaf = ConfiguredStep("demo.flaky", {}, "0", None, RetryPolicy(max_attempts=2, delay_seconds=0))

    with pytest.raises(RetryableStepError, match="transient"):
        PipelineExecutor().run([leaf], ctx)

    assert calls == ["run", "run"]
    assert ctx.failed_step == "0"
    assert ctx.completed_steps == []
    assert ctx.step_attempts == {"0": {"attempts": 2, "last_error": "transient"}}


def test_non_retryable_exception_is_never_retried(tmp_path):
    calls = []

    @sdk_step("demo.broken", retry_safe=True)
    def broken(ctx):
        calls.append("run")
        raise ValueError("boom")

    ctx = PipelineContext(cwd=tmp_path, env={}, runner=CommandRunner())
    leaf = ConfiguredStep("demo.broken", {}, "0", None, RetryPolicy(max_attempts=3, delay_seconds=0))

    with pytest.raises(ValueError, match="boom"):
        PipelineExecutor().run([leaf], ctx)

    assert calls == ["run"]
    assert ctx.failed_step == "0"
    assert ctx.step_attempts == {}


def test_base_exception_is_not_treated_as_retryable(tmp_path):
    calls = []

    @sdk_step("demo.interrupt", retry_safe=True)
    def interrupt(ctx):
        calls.append("run")
        raise KeyboardInterrupt

    ctx = PipelineContext(cwd=tmp_path, env={}, runner=CommandRunner())
    leaf = ConfiguredStep("demo.interrupt", {}, "0", None, RetryPolicy(max_attempts=3, delay_seconds=0))

    with pytest.raises(KeyboardInterrupt):
        PipelineExecutor().run([leaf], ctx)

    assert calls == ["run"]


def test_unsafe_step_rejects_multiple_attempts_before_running(tmp_path):
    calls = []

    @sdk_step("demo.unsafe")
    def unsafe(ctx):
        calls.append("run")

    ctx = PipelineContext(cwd=tmp_path, env={}, runner=CommandRunner())
    leaf = ConfiguredStep("demo.unsafe", {}, "0", None, RetryPolicy(max_attempts=2, delay_seconds=0))

    with pytest.raises(PipelineExecutionError, match="not declared retry_safe"):
        PipelineExecutor().run([leaf], ctx)

    assert calls == []
    assert ctx.failed_step == "0"


def test_retry_policy_applies_alike_to_sequence_and_parallel_leaves(tmp_path):
    sequential_calls = []
    parallel_calls = []
    _register_flaky_step(sequential_calls, fail_times=1, error="seq-transient", name="demo.seq_flaky")
    _register_flaky_step(parallel_calls, fail_times=2, error="par-transient", name="demo.par_flaky")
    ctx = PipelineContext(cwd=tmp_path, env={}, runner=CommandRunner())
    steps = [
        SequentialStepGroup(
            [ConfiguredStep("demo.seq_flaky", {}, "0/0", None, RetryPolicy(max_attempts=3, delay_seconds=0))],
            step_id="0",
        ),
        ParallelStepGroup(
            [ConfiguredStep("demo.par_flaky", {}, "1/0", None, RetryPolicy(max_attempts=3, delay_seconds=0))],
            step_id="1",
        ),
    ]

    PipelineExecutor().run(steps, ctx)

    assert len(sequential_calls) == 2
    assert len(parallel_calls) == 3
    assert sorted(ctx.completed_steps) == ["0", "0/0", "1", "1/0"]
    assert ctx.step_attempts["0/0"] == {"attempts": 1, "last_error": "seq-transient"}
    assert ctx.step_attempts["1/0"] == {"attempts": 2, "last_error": "par-transient"}


def test_skipped_retry_step_is_never_attempted(tmp_path):
    calls = []
    _register_flaky_step(calls, fail_times=99)
    ctx = PipelineContext(cwd=tmp_path, env={}, runner=CommandRunner(), inputs={})
    leaf = ConfiguredStep(
        "demo.flaky",
        {},
        "0",
        {"input": "deploy", "present": True},
        RetryPolicy(max_attempts=3, delay_seconds=0),
    )

    PipelineExecutor().run([leaf], ctx)

    assert calls == []
    assert ctx.skipped_steps == ["0"]
    assert ctx.step_attempts == {}


def test_condition_decisions_are_frozen_before_first_step(tmp_path):
    events = []

    class MutateInputs:
        name = "mutate"

        def run(self, ctx):
            ctx.inputs["deploy"] = "yes"

    leaf = RecordingStep("conditional", events)
    leaf.when = {"input": "deploy", "equals": "yes"}
    ctx = PipelineContext(cwd=tmp_path, env={}, runner=CommandRunner())
    PipelineExecutor().run([MutateInputs(), leaf], ctx)
    assert events == []
    assert ctx.skipped_steps == ["conditional"]
    assert ctx.completed_steps == ["mutate"]


@pytest.mark.parametrize("nested", [False, True])
def test_all_skipped_parallel_never_constructs_pool_or_runtime_step(tmp_path, monkeypatch, nested):
    from cdt.pipeline.config import ConfiguredStep

    def unexpected(*args, **kwargs):
        raise AssertionError("Skipped group must not create a thread pool")

    monkeypatch.setattr("cdt.pipeline.executor.ThreadPoolExecutor", unexpected)
    leaf = ConfiguredStep(
        "unregistered.step",
        {"option": "${MISSING}"},
        "0/0/0" if nested else "0/0",
        {"input": "deploy", "present": True},
    )
    children = [SequentialStepGroup([leaf], "0/0")] if nested else [leaf]
    ctx = PipelineContext(cwd=tmp_path, env={}, runner=CommandRunner())
    PipelineExecutor().run([ParallelStepGroup(children, "0")], ctx)
    assert ctx.skipped_steps == [leaf.step_id]
    assert ctx.completed_steps == []
    assert ctx.artifacts == {}


def test_mixed_sequence_skips_before_runtime_construction(tmp_path):
    from cdt.pipeline.config import ConfiguredStep

    events = []
    active = RecordingStep("active", events)
    skipped = ConfiguredStep("unregistered.step", {"option": "${MISSING}"}, "0/0", {"input": "deploy", "present": True})
    ctx = PipelineContext(cwd=tmp_path, env={}, runner=CommandRunner())
    PipelineExecutor().run([SequentialStepGroup([skipped, active], "0")], ctx)
    assert events == ["active"]
    assert "0/0" not in ctx.completed_steps


class CallbackStep:
    def __init__(self, step_id, callback):
        self.step_id = step_id
        self.name = "callback"
        self.callback = callback

    def run(self, ctx):
        self.callback(ctx)


def test_parallel_values_isolated_with_sequence_and_nested_mutation(tmp_path):
    barrier = threading.Barrier(2)
    ctx = PipelineContext(cwd=tmp_path, env={}, runner=CommandRunner(), values={"nested": []})

    def first(ctx):
        ctx.values["nested"].append("local")
        ctx.values["left"] = "yes"
        barrier.wait(timeout=2)

    def sibling(ctx):
        barrier.wait(timeout=2)
        assert ctx.values == {"nested": []}
        ctx.values["right"] = "yes"

    def following(ctx):
        assert ctx.values["left"] == "yes"
        assert "right" not in ctx.values

    PipelineExecutor().run(
        [
            ParallelStepGroup(
                [
                    SequentialStepGroup([CallbackStep("0/0/0", first), CallbackStep("0/0/1", following)], "0/0"),
                    CallbackStep("0/1", sibling),
                ],
                "0",
            )
        ],
        ctx,
    )
    assert ctx.values == {"nested": ["local"], "left": "yes", "right": "yes"}


@pytest.mark.parametrize("right,conflict", [("same", False), ("different", True), (None, True)])
def test_parallel_values_merge_conflicts_are_atomic(tmp_path, right, conflict):
    ctx = PipelineContext(cwd=tmp_path, env={}, runner=CommandRunner(), values={"key": "old"})

    def left(ctx):
        ctx.values.update(key="same", independent="private")

    def sibling(ctx):
        if right is None:
            del ctx.values["key"]
        else:
            ctx.values["key"] = right

    steps = [ParallelStepGroup([CallbackStep("0/0", left), CallbackStep("0/1", sibling)], "0")]
    if conflict:
        with pytest.raises(PipelineExecutionError, match="key.*0/0.*0/1") as error:
            PipelineExecutor().run(steps, ctx)
        assert "private" not in str(error.value)
        assert "different" not in str(error.value)
        assert ctx.values == {"key": "old"}
    else:
        PipelineExecutor().run(steps, ctx)
        assert ctx.values == {"key": "same", "independent": "private"}


def test_parallel_values_identical_deletions_and_skipped_leaf(tmp_path):
    ctx = PipelineContext(cwd=tmp_path, env={}, runner=CommandRunner(), values={"key": "old"})
    children = [CallbackStep(f"0/{i}", lambda ctx: ctx.values.pop("key")) for i in range(3)]
    children[-1].when = {"input": "absent", "present": True}
    PipelineExecutor().run([ParallelStepGroup(children, "0")], ctx)
    assert ctx.values == {}
    assert "0/2" not in ctx.completed_steps


class RecordingStep:
    def __init__(self, name: str, events: list[str], fail: bool = False):
        self.name = name
        self.events = events
        self.fail = fail

    def run(self, ctx: PipelineContext) -> None:
        self.events.append(self.name)
        if self.fail:
            raise RuntimeError(self.name)


def test_executor_runs_steps_in_order(tmp_path):
    events: list[str] = []
    ctx = PipelineContext(cwd=tmp_path, env={}, runner=CommandRunner())

    PipelineExecutor().run(
        [RecordingStep("first", events), RecordingStep("second", events)],
        ctx,
    )

    assert events == ["first", "second"]


def test_executor_stops_on_exception(tmp_path):
    events: list[str] = []
    ctx = PipelineContext(cwd=tmp_path, env={}, runner=CommandRunner())

    with pytest.raises(RuntimeError, match="boom"):
        PipelineExecutor().run(
            [
                RecordingStep("first", events),
                RecordingStep("boom", events, fail=True),
                RecordingStep("third", events),
            ],
            ctx,
        )

    assert events == ["first", "boom"]


class SleepingStep:
    def __init__(self, name: str, events: list[str], delay: float = 0.0, fail: bool = False):
        self.name = name
        self.events = events
        self.delay = delay
        self.fail = fail

    def run(self, ctx: PipelineContext) -> None:
        time.sleep(self.delay)
        self.events.append(self.name)
        if self.fail:
            raise RuntimeError(self.name)


class MessageFailingStep:
    def __init__(self, name: str, message: str, command: str | None = None):
        self.name = name
        self.message = message
        if command is not None:
            self.options = {"script": command}

    def run(self, ctx: PipelineContext) -> None:
        raise RuntimeError(self.message)


class DelayedCommandFailStep:
    def __init__(self, name: str, command: list[str], exit_code: int, delay: float = 0.0):
        self.name = name
        self.command = command
        self.exit_code = exit_code
        self.delay = delay

    def run(self, ctx: PipelineContext) -> None:
        time.sleep(self.delay)
        raise CommandExecutionError(f"{self.name} build failed", command=self.command, exit_code=self.exit_code)


class NetworkFailingStep:
    def __init__(self, name: str):
        self.name = name

    def run(self, ctx: PipelineContext) -> None:
        raise TimeoutError("SSL connection unexpectedly closed")


class ArtifactStep:
    def __init__(self, name: str, artifact: str):
        self.name = name
        self.artifact = artifact

    def run(self, ctx: PipelineContext) -> None:
        path = ctx.cwd / f"{self.artifact}.bin"
        path.write_text("artifact", encoding="utf-8")
        kind = ArtifactKind.AAB if "aab" in self.artifact else ArtifactKind.IPA
        ctx.register_artifact(self.artifact, BuildArtifact(kind, path, "Demo"))


class BarrierStep:
    def __init__(self, name: str, events: list[str], barrier: threading.Barrier):
        self.name = name
        self.events = events
        self.barrier = barrier

    def run(self, ctx: PipelineContext) -> None:
        self.events.append(f"{self.name}:started")
        self.barrier.wait(timeout=1.0)
        self.events.append(f"{self.name}:finished")


class ValueStep:
    def __init__(self, name: str, key: str, value: str):
        self.name = name
        self.key = key
        self.value = value

    def run(self, ctx: PipelineContext) -> None:
        ctx.values[self.key] = self.value


class AssertValuesStep:
    name = "assert.values"

    def run(self, ctx: PipelineContext) -> None:
        assert ctx.values["ios"] == "done"
        assert ctx.values["android"] == "done"


def test_parallel_group_runs_children_concurrently(tmp_path):
    events: list[str] = []
    barrier = threading.Barrier(2)
    ctx = PipelineContext(cwd=tmp_path, env={}, runner=CommandRunner())

    PipelineExecutor().run(
        [
            ParallelStepGroup(
                [
                    BarrierStep("first", events, barrier),
                    BarrierStep("second", events, barrier),
                ]
            )
        ],
        ctx,
    )

    assert sorted(events) == [
        "first:finished",
        "first:started",
        "second:finished",
        "second:started",
    ]


def test_parallel_sequence_runs_android_aab_then_apk_without_waiting_for_ios(tmp_path):
    events: list[str] = []
    ctx = PipelineContext(cwd=tmp_path, env={}, runner=CommandRunner())

    PipelineExecutor().run(
        [
            ParallelStepGroup(
                [
                    SleepingStep("ios", events, delay=0.15),
                    SequentialStepGroup(
                        [SleepingStep("aab", events, delay=0.01), SleepingStep("apk", events)],
                        step_id="0/1",
                    ),
                ],
                step_id="0",
            )
        ],
        ctx,
    )

    assert events.index("aab") < events.index("apk") < events.index("ios")


def test_parallel_sequence_stops_after_failed_step_but_other_branch_finishes(tmp_path):
    events: list[str] = []
    status_file = tmp_path / "status.json"
    ctx = PipelineContext(cwd=tmp_path, env={}, runner=CommandRunner(), status_file=status_file)
    ios = SleepingStep("ios", events, delay=0.05)
    ios.step_id = "0/0"
    aab = SleepingStep("aab", events, fail=True)
    aab.step_id = "0/1/0"
    apk = SleepingStep("apk", events)
    apk.step_id = "0/1/1"

    with pytest.raises(PipelineExecutionError, match="Pipeline failed at step 0/1/0"):
        PipelineExecutor().run(
            [
                ParallelStepGroup(
                    [ios, SequentialStepGroup([aab, apk], step_id="0/1")],
                    step_id="0",
                )
            ],
            ctx,
        )

    payload = json.loads(status_file.read_text(encoding="utf-8"))
    assert sorted(events) == ["aab", "ios"]
    assert payload["failed_step"] == "0/1/0"
    assert "0/1/0: aab" in payload["parallel_failed"]


def test_parallel_group_reports_failure_after_all_children_finish(tmp_path):
    events: list[str] = []
    ctx = PipelineContext(cwd=tmp_path, env={}, runner=CommandRunner())

    with pytest.raises(PipelineExecutionError, match="Other parallel steps were allowed to finish."):
        PipelineExecutor().run(
            [
                ParallelStepGroup(
                    [
                        SleepingStep("failed", events, fail=True),
                        SleepingStep("slow", events, delay=0.15),
                    ]
                )
            ],
            ctx,
        )

    assert sorted(events) == ["failed", "slow"]


def test_parallel_group_preserves_all_child_failures_in_status(tmp_path):
    events: list[str] = []
    status_file = tmp_path / "status.json"
    ctx = PipelineContext(cwd=tmp_path, env={}, runner=CommandRunner(), status_file=status_file)
    first = SleepingStep("first", events, fail=True)
    first.step_id = "0/0"
    second = SleepingStep("second", events, fail=True)
    second.step_id = "0/1"

    with pytest.raises(PipelineExecutionError):
        PipelineExecutor().run([ParallelStepGroup([first, second], step_id="0")], ctx)

    payload = json.loads(status_file.read_text(encoding="utf-8"))
    assert sorted(payload["parallel_failed"]) == ["0/0: first", "0/1: second"]
    assert payload["status"] == "failed"


def test_parallel_single_failure_names_child_id_name_and_cause(tmp_path):
    events: list[str] = []
    status_file = tmp_path / "status.json"
    ctx = PipelineContext(cwd=tmp_path, env={}, runner=CommandRunner(), status_file=status_file)
    failing = MessageFailingStep("ios.upload", "transporter hung up", command="xcrun iTMSTransporter")
    failing.step_id = "0/0"
    healthy = SleepingStep("android", events, delay=0.05)
    healthy.step_id = "0/1"

    with pytest.raises(PipelineExecutionError) as exc_info:
        PipelineExecutor().run([ParallelStepGroup([failing, healthy], step_id="0")], ctx)

    message = str(exc_info.value)
    lines = message.splitlines()
    assert lines[0] == "Pipeline failed at step 0/0 (ios.upload)."
    assert lines[1] == "transporter hung up"
    assert "Command: xcrun iTMSTransporter" in lines
    assert "Exit code:" not in message
    assert "Other parallel steps were allowed to finish." in lines
    assert "command: unknown" not in message
    assert "exit code: unknown" not in message
    assert "not applicable" not in message
    payload = json.loads(status_file.read_text(encoding="utf-8"))
    assert payload["failed_step"] == "0/0"
    assert payload["status"] == "failed"


def test_parallel_multiple_failures_list_each_child_cause(tmp_path):
    ctx = PipelineContext(cwd=tmp_path, env={}, runner=CommandRunner())
    first = MessageFailingStep("ios.upload", "transporter hung up")
    first.step_id = "0/0"
    second = MessageFailingStep("android.build", "gradle daemon died")
    second.step_id = "0/1"

    with pytest.raises(PipelineExecutionError) as exc_info:
        PipelineExecutor().run([ParallelStepGroup([first, second], step_id="0")], ctx)

    lines = str(exc_info.value).splitlines()
    assert lines[0] == "Pipeline failed at step 0/0 (ios.upload)."
    assert lines[1] == "transporter hung up"
    assert "0/1 (android.build):" in lines
    assert "gradle daemon died" in lines
    assert lines.index("transporter hung up") < lines.index("0/1 (android.build):")


def test_parallel_multiple_command_failures_order_children_by_config_and_exit_codes(tmp_path):
    ctx = PipelineContext(cwd=tmp_path, env={}, runner=CommandRunner())
    first = DelayedCommandFailStep("ios.build", ["flutter", "build", "ipa"], 74, delay=0.05)
    first.step_id = "0/0"
    second = DelayedCommandFailStep("android.build", ["flutter", "build", "appbundle"], 1)
    second.step_id = "0/1"

    with pytest.raises(PipelineExecutionError) as exc_info:
        PipelineExecutor().run([ParallelStepGroup([first, second], step_id="0")], ctx)

    lines = str(exc_info.value).splitlines()
    assert lines[0] == "Pipeline failed at step 0/0 (ios.build)."
    assert lines[1] == "ios.build build failed"
    assert "Command: flutter build ipa" in lines
    assert "Exit code: 74" in lines
    assert "0/1 (android.build):" in lines
    assert "android.build build failed" in lines
    assert "Command: flutter build appbundle" in lines
    assert "Exit code: 1" in lines
    assert lines.index("Exit code: 74") < lines.index("0/1 (android.build):")
    assert lines[-2] == "Other parallel steps were allowed to finish."
    assert lines[-1] == "Artifacts produced: none"


def test_parallel_failure_reports_successful_sibling_artifacts(tmp_path):
    status_file = tmp_path / "status.json"
    ctx = PipelineContext(cwd=tmp_path, env={}, runner=CommandRunner(), status_file=status_file)
    failing = MessageFailingStep("ios.build", "archive failed")
    failing.step_id = "0/0"
    android = ArtifactStep("android.build", "android_aab")
    android.step_id = "0/1"

    with pytest.raises(PipelineExecutionError) as exc_info:
        PipelineExecutor().run([ParallelStepGroup([failing, android], step_id="0")], ctx)

    lines = str(exc_info.value).splitlines()
    assert lines[-1] == "Artifacts produced: android_aab"
    assert "Other parallel steps were allowed to finish." in lines
    payload = json.loads(status_file.read_text(encoding="utf-8"))
    assert [artifact["name"] for artifact in payload["artifacts"]] == ["android_aab"]
    assert payload["failed_step"] == "0/0"


def test_parallel_network_failure_reports_no_exit_code(tmp_path):
    events: list[str] = []
    status_file = tmp_path / "status.json"
    ctx = PipelineContext(cwd=tmp_path, env={}, runner=CommandRunner(), status_file=status_file)
    failing = NetworkFailingStep("asc.wait")
    failing.step_id = "0/0"
    healthy = SleepingStep("slow", events, delay=0.05)
    healthy.step_id = "0/1"

    with pytest.raises(PipelineExecutionError) as exc_info:
        PipelineExecutor().run([ParallelStepGroup([failing, healthy], step_id="0")], ctx)

    message = str(exc_info.value)
    lines = message.splitlines()
    assert lines[0] == "Pipeline failed at step 0/0 (asc.wait)."
    assert lines[1] == "SSL connection unexpectedly closed"
    assert "Command:" not in message
    assert "Exit code:" not in message
    assert "unknown" not in message
    assert "not applicable" not in message
    payload = json.loads(status_file.read_text(encoding="utf-8"))
    assert payload["failed_step"] == "0/0"


def test_parallel_sequence_failure_reports_deepest_failed_child(tmp_path):
    events: list[str] = []
    ctx = PipelineContext(cwd=tmp_path, env={}, runner=CommandRunner())
    aab = MessageFailingStep("android.build.aab", "gradle daemon died", command="./gradlew bundleReleaseAab")
    aab.step_id = "0/1/0"
    apk = SleepingStep("apk", events)
    apk.step_id = "0/1/1"
    branch = SequentialStepGroup([aab, apk], step_id="0/1")
    ios = SleepingStep("ios", events, delay=0.05)
    ios.step_id = "0/0"

    with pytest.raises(PipelineExecutionError) as exc_info:
        PipelineExecutor().run([ParallelStepGroup([ios, branch], step_id="0")], ctx)

    lines = str(exc_info.value).splitlines()
    assert lines[0] == "Pipeline failed at step 0/1/0 (android.build.aab)."
    assert lines[1] == "gradle daemon died"
    assert "Command: ./gradlew bundleReleaseAab" in lines
    assert "parallel" not in lines[0]
    assert "Other parallel steps were allowed to finish." in lines


def test_parallel_group_values_are_visible_to_later_steps(tmp_path):
    ctx = PipelineContext(cwd=tmp_path, env={}, runner=CommandRunner())

    PipelineExecutor().run(
        [
            ParallelStepGroup(
                [
                    ValueStep("ios", "ios", "done"),
                    ValueStep("android", "android", "done"),
                ]
            ),
            AssertValuesStep(),
        ],
        ctx,
    )


def test_parallel_group_writes_child_runtime_status(tmp_path):
    status_file = tmp_path / "status.json"
    ctx = PipelineContext(cwd=tmp_path, env={}, runner=CommandRunner(), status_file=status_file)

    PipelineExecutor().run(
        [ParallelStepGroup([ValueStep("ios", "ios", "done"), ValueStep("android", "android", "done")])],
        ctx,
    )

    payload = json.loads(status_file.read_text(encoding="utf-8"))
    assert payload["running_steps"] == []
    assert sorted(payload["parallel_completed"]) == ["android", "ios"]
    assert payload["parallel_failed"] == []


def test_failed_branch_leaves_root_values_without_partial_merge(tmp_path):
    """A failed branch discards every branch delta: the root keeps its base values."""

    ctx = PipelineContext(cwd=tmp_path, env={}, runner=CommandRunner(), values={"key": "old"})

    def left(ctx):
        ctx.values["fresh"] = "private"
        raise typer.BadParameter("offline")

    steps = [
        ParallelStepGroup(
            [
                CallbackStep("0/0", left),
                CallbackStep("0/1", lambda ctx: ctx.values.update({"sibling": "yes"})),
            ],
            "0",
        )
    ]
    with pytest.raises(PipelineExecutionError, match="Pipeline failed at step"):
        PipelineExecutor().run(steps, ctx)
    assert ctx.values == {"key": "old"}
    assert "0" not in ctx.completed_steps


def test_end_to_end_offline_inputs_conditions_parallel_values_retry_webhook(tmp_path, monkeypatch):
    """One offline scenario wiring inputs, conditional leaves, parallel sequences,
    isolated values, a safe retry, and a mocked webhook through the real config
    and CLI path."""

    package = tmp_path / "cdt_steps"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "demo.py").write_text(
        "\n".join(
            [
                "from cdt.pipeline.policy import RetryableStepError",
                "from cdt.sdk import step",
                "",
                "calls = []",
                "",
                "@step('demo.flaky', retry_safe=True)",
                "def flaky(ctx):",
                "    calls.append('flaky')",
                "    if len(calls) == 1:",
                "        raise RetryableStepError('transient')",
                "    ctx.values['left'] = 'yes'",
                "",
                "@step('demo.skipped_check')",
                "def skipped_check(ctx):",
                "    calls.append('skipped_check')",
                "",
                "@step('demo.sibling')",
                "def sibling(ctx):",
                "    assert 'left' not in ctx.values, 'sibling must not see branch values'",
                "    ctx.values['right'] = 'yes'",
                "",
                "@step('demo.finish')",
                "def finish(ctx):",
                "    assert ctx.values.get('left') == 'yes' and ctx.values.get('right') == 'yes'",
                "    calls.append('finish')",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "cdt.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "plugins:",
                "  - cdt_steps.demo",
                "pipelines:",
                "  demo:",
                "    inputs:",
                "      deploy: {}",
                "    steps:",
                "      - parallel:",
                "          steps:",
                "            - sequence:",
                "                steps:",
                "                  - step: demo.flaky",
                "                    retry: {max_attempts: 3, delay_seconds: 0}",
                "                  - step: demo.skipped_check",
                "                    when: {input: deploy, equals: 'no'}",
                "            - sequence:",
                "                steps:",
                "                  - demo.sibling",
                "                  - step: notify.webhook",
                "                    when: {input: deploy, equals: 'yes'}",
                "                    with:",
                "                      url_env: WEBHOOK_URL",
                "                      payload:",
                '                        text: "released ${inputs.deploy}"',
                "      - demo.finish",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))

    sent = []

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_open(request, timeout):
        sent.append(request)
        return FakeResponse()

    monkeypatch.setattr(webhook, "_open_webhook_response", fake_open)
    monkeypatch.setenv("WEBHOOK_URL", "https://hooks.example/abc123")

    status_file = tmp_path / "status.json"
    result = runner.invoke(app, ["run", "demo", "--input", "deploy=yes", "--status-file", str(status_file)])

    assert result.exit_code == 0, result.output
    demo = sys.modules["cdt_steps.demo"]
    assert demo.calls.count("flaky") == 2  # one retryable failure, then success
    assert demo.calls[-1] == "finish"  # ran after the group merged branch values
    assert "skipped_check" not in demo.calls  # conditional leaf was skipped before running
    assert len(sent) == 1
    assert sent[0].full_url == "https://hooks.example/abc123"
    assert json.loads(sent[0].data) == {"text": "released yes"}

    status = json.loads(status_file.read_text(encoding="utf-8"))
    assert status["status"] == "success"
    assert status["skipped_steps"] == ["0/0/1"]
    assert status["step_decisions"]["0/0/1"] == "skip"
    assert status["step_decisions"]["0/1/1"] == "run"
    assert sorted(status["completed_steps"]) == ["0", "0/0", "0/0/0", "0/1", "0/1/0", "0/1/1", "1"]
