import sys
from pathlib import Path

from cdt.pipeline.builtins import register_builtin_steps
from cdt.pipeline.config import load_pipeline_config, load_plugins
from cdt.pipeline.registry import _clear_steps_for_tests, list_steps
from cdt.pipeline.validation import validate_pipeline


def setup_function():
    _clear_steps_for_tests()
    sys.modules.pop("cdt_steps.offline", None)
    sys.modules.pop("cdt_steps", None)


def teardown_function():
    _clear_steps_for_tests()
    sys.modules.pop("cdt_steps.offline", None)
    sys.modules.pop("cdt_steps", None)


def test_example_cdt_yaml_parses_and_validates(monkeypatch):
    examples = Path(__file__).resolve().parents[1] / "examples"
    monkeypatch.syspath_prepend(str(examples))
    sys.modules.pop("cdt_steps.offline", None)
    sys.modules.pop("cdt_steps", None)

    config = load_pipeline_config(examples)
    register_builtin_steps()
    load_plugins(config.plugins)

    assert validate_pipeline(config) == []
    assert "offline.fetch_config" in list_steps()
