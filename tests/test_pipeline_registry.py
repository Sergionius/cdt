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
from cdt.sdk import step as sdk_step


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


def test_sdk_step_accepts_keyword_metadata():
    @sdk_step("demo.fetch", description="Fetch demo data", category="demo", risk="safe", produces=("json",))
    def fetch(ctx, output: str) -> None:
        pass

    metadata = get_step_metadata("demo.fetch")
    assert metadata.description == "Fetch demo data"
    assert metadata.category == "demo"
    assert metadata.risk == "safe"
    assert metadata.produces == ("json",)
    assert metadata.plugin is True


def test_sdk_step_accepts_metadata_object():
    given = StepMetadata(
        name="demo.fetch",
        description="Fetch via object",
        category="demo",
        risk="upload",
        external_tools=("curl",),
    )

    @sdk_step("demo.fetch", metadata=given)
    def fetch(ctx, output: str) -> None:
        pass

    metadata = get_step_metadata("demo.fetch")
    assert metadata.description == "Fetch via object"
    assert metadata.risk == "upload"
    assert metadata.external_tools == ("curl",)
    assert metadata.plugin is True


def test_sdk_step_defaults_category_from_name_and_custom_risk():
    @sdk_step("offline.sync")
    def sync(ctx, output: str) -> None:
        pass

    metadata = get_step_metadata("offline.sync")
    assert metadata.category == "offline"
    assert metadata.risk == "custom"
    assert metadata.plugin is True


def test_sdk_step_defaults_custom_category_for_flat_names():
    @sdk_step("sync")
    def sync(ctx, output: str) -> None:
        pass

    metadata = get_step_metadata("sync")
    assert metadata.category == "custom"
    assert metadata.risk == "custom"
    assert metadata.plugin is True
