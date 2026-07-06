import pytest
import typer

from cdt.pipeline.builtins import register_builtin_steps
from cdt.pipeline.registry import (
    StepMetadata,
    _clear_steps_for_tests,
    get_step_factory,
    get_step_metadata,
    list_step_metadata,
    list_steps,
    register_step,
)


class DummyStep:
    name = "dummy"

    def run(self, ctx):
        return None


def setup_function():
    _clear_steps_for_tests()


def teardown_function():
    _clear_steps_for_tests()


def test_register_and_get_step_factory():
    register_step("demo.step", DummyStep)

    assert get_step_factory("demo.step") is DummyStep
    assert list_steps() == ["demo.step"]


def test_register_step_metadata():
    metadata = StepMetadata(
        name="demo.step",
        description="Demo step",
        category="demo",
        risk="safe",
        external_tools=("demo",),
    )
    register_step("demo.step", DummyStep, metadata=metadata)

    registered = get_step_metadata("demo.step")
    assert registered.description == "Demo step"
    assert registered.category == "demo"
    assert registered.risk == "safe"
    assert registered.external_tools == ("demo",)
    assert list_step_metadata() == [registered]


def test_builtin_metadata_registration():
    register_builtin_steps()

    flutter = get_step_metadata("flutter.pub_get")
    appstore = get_step_metadata("appstore.upload_testflight")

    assert flutter.category == "flutter"
    assert flutter.risk == "safe"
    assert flutter.external_tools == ("flutter",)
    assert appstore.category == "appstore"
    assert appstore.risk == "upload"
    assert appstore.requires_artifacts == ("artifact",)


def test_duplicate_step_registration_errors():
    register_step("demo.step", DummyStep)

    with pytest.raises(typer.BadParameter, match="already registered"):
        register_step("demo.step", DummyStep)


def test_unknown_step_error_lists_available_steps():
    register_step("demo.step", DummyStep)

    with pytest.raises(typer.BadParameter, match="Unknown pipeline step: missing.step"):
        get_step_factory("missing.step")
