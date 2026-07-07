import json
import threading
import time

import pytest

from cdt.pipeline import ParallelStepGroup, PipelineContext, PipelineExecutor
from cdt.runner import CommandRunner


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


def test_parallel_group_reports_failure_after_all_children_finish(tmp_path):
    events: list[str] = []
    ctx = PipelineContext(cwd=tmp_path, env={}, runner=CommandRunner())

    with pytest.raises(Exception, match="Parallel group failed"):
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
