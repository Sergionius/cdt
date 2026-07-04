import json
import re
import sys

from typer.testing import CliRunner

from cdt.cli import app
from cdt.pipeline.registry import _clear_steps_for_tests

runner = CliRunner()
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _visible_text(output: str) -> str:
    return ANSI_RE.sub("", output)


def setup_function():
    _clear_steps_for_tests()
    sys.modules.pop("cdt_steps.offline", None)
    sys.modules.pop("cdt_steps", None)


def teardown_function():
    _clear_steps_for_tests()
    sys.modules.pop("cdt_steps.offline", None)
    sys.modules.pop("cdt_steps", None)


def test_root_help_lists_commands():
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    for command in (
        "run",
        "pipeline",
        "migrate",
    ):
        assert command in result.output


def test_root_version_flags():
    for flag in ("--version", "-V"):
        result = runner.invoke(app, [flag])

        assert result.exit_code == 0
        assert result.output == "cdt 0.2.0\n"


def test_command_help_lists_key_options():
    cases = {
        "run": ("--id",),
        "pipeline": (),
        "migrate": (),
    }

    for command, options in cases.items():
        result = runner.invoke(app, [command, "--help"])
        output = _visible_text(result.output)

        assert result.exit_code == 0
        for option in options:
            assert option in output


def test_pipeline_inspect_json_returns_step_tree(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "cdt.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "pipelines:",
                "  demo:",
                "    steps:",
                "      - flutter.pub_get",
                "      - parallel:",
                "          steps:",
                "            - web.build:",
                "                env: prod",
                "            - android.build_aab:",
                "                artifact: android_aab",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["pipeline", "inspect", "demo", "--json"])
    payload = json.loads(result.output)

    assert result.exit_code == 0
    assert payload["schema_version"] == 1
    assert payload["pipeline"] == "demo"
    assert payload["plugins"] == []
    assert payload["errors"] == []
    assert payload["steps"] == [
        {"type": "step", "name": "flutter.pub_get", "options": {}},
        {
            "type": "parallel",
            "steps": [
                {"type": "step", "name": "web.build", "options": {"env": "prod"}},
                {"type": "step", "name": "android.build_aab", "options": {"artifact": "android_aab"}},
            ],
        },
    ]


def test_pipeline_validate_json_reports_unknown_step(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "cdt.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "pipelines:",
                "  demo:",
                "    steps:",
                "      - missing.step",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["pipeline", "validate", "demo", "--json"])
    payload = json.loads(result.output)

    assert result.exit_code == 1
    assert payload["schema_version"] == 1
    assert payload["pipeline"] == "demo"
    assert payload["errors"][0]["code"] == "unknown_step"
    assert payload["errors"][0]["path"] == "pipelines.demo.steps[0]"


def test_pipeline_steps_json_includes_plugin_steps(tmp_path, monkeypatch):
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
                "    pass",
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
                "          output: build/offline/config.json",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("cdt_steps.offline", None)
    sys.modules.pop("cdt_steps", None)

    result = runner.invoke(app, ["pipeline", "steps", "--json"])
    payload = json.loads(result.output)

    assert result.exit_code == 0
    assert payload["schema_version"] == 1
    assert "flutter.pub_get" in payload["registered_steps"]
    assert "offline.fetch_config" in payload["registered_steps"]
