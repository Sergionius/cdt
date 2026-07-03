import pytest
import typer

from cdt.pipeline.registry import _clear_steps_for_tests, get_step_factory, list_steps, register_step


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


def test_duplicate_step_registration_errors():
    register_step("demo.step", DummyStep)

    with pytest.raises(typer.BadParameter, match="already registered"):
        register_step("demo.step", DummyStep)


def test_unknown_step_error_lists_available_steps():
    register_step("demo.step", DummyStep)

    with pytest.raises(typer.BadParameter, match="Unknown pipeline step: missing.step"):
        get_step_factory("missing.step")
