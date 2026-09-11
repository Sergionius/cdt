import json
import threading
import time

import pytest

from cdt.artifacts import ArtifactKind, BuildArtifact
from cdt.pipeline import ParallelStepGroup, PipelineContext, PipelineExecutor, SequentialStepGroup
from cdt.pipeline.executor import PipelineExecutionError
from cdt.runner import CommandExecutionError, CommandRunner


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
