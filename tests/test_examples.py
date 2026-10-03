import os
import subprocess
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


def test_reusable_plugin_example_runs_in_isolated_subprocess():
    """The reusable-plugin example is checked in an isolated subprocess with a
    local import path: nothing is installed globally and no package index is
    contacted."""
    repo_root = Path(__file__).resolve().parents[1]
    example_dir = repo_root / "examples" / "reusable-plugin"

    driver = "\n".join(
        [
            "import os",
            "from pathlib import Path",
            "from cdt.pipeline.builtins import register_builtin_steps",
            "from cdt.pipeline.config import load_pipeline_config, load_plugins",
            "from cdt.pipeline.registry import get_step_metadata",
            "from cdt.pipeline.runner import run_configured_pipeline",
            "from cdt.pipeline.validation import validate_pipeline",
            "config = load_pipeline_config(Path.cwd())",
            "register_builtin_steps()",
            "load_plugins(config.plugins)",
            "metadata = get_step_metadata('example.check_file')",
            "assert metadata.plugin and metadata.retry_safe and metadata.risk == 'safe'",
            "assert validate_pipeline(config) == []",
            "assert (",
            "    run_configured_pipeline(Path.cwd(), env=dict(os.environ), name='example', record_run=False)",
            "    is None",
            ")",
            "print('PLUGIN_EXAMPLE_OK')",
        ]
    )

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(repo_root), str(example_dir / "src"), env.get("PYTHONPATH")]))

    result = subprocess.run(
        [sys.executable, "-c", driver],
        cwd=example_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "PLUGIN_EXAMPLE_OK" in result.stdout
