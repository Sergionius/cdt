import json
import sys

from typer.testing import CliRunner

from cdt.cli import app
from cdt.pipeline.registry import _clear_steps_for_tests

runner = CliRunner()


def setup_function():
    _clear_steps_for_tests()
    sys.modules.pop("cdt_steps.side_effect", None)
    sys.modules.pop("cdt_steps", None)


def teardown_function():
    _clear_steps_for_tests()
    sys.modules.pop("cdt_steps.side_effect", None)
    sys.modules.pop("cdt_steps", None)


def test_pipeline_plan_json_includes_risks_and_parallel_steps(tmp_path, monkeypatch):
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
                "            - ios.flutter_build_ipa:",
                "                artifact: ios_ipa",
                "            - android.build_aab:",
                "                artifact: android_aab",
                "      - appstore.upload_testflight:",
                "          artifact: ios_ipa",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["pipeline", "plan", "demo", "--json"])
    payload = json.loads(result.output)

    assert result.exit_code == 0
    assert payload["schema_version"] == 1
    assert payload["pipeline"] == "demo"
    assert payload["overall_risk"] == "upload"
    assert payload["errors"] == []
    assert payload["steps"][0]["name"] == "flutter.pub_get"
    assert payload["steps"][0]["risk"] == "safe"
    assert payload["steps"][1]["type"] == "parallel"
    assert payload["steps"][1]["risk"] == "build"
    assert payload["steps"][2]["risk"] == "upload"


def test_pipeline_plan_json_reports_unknown_step(tmp_path, monkeypatch):
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

    result = runner.invoke(app, ["pipeline", "plan", "demo", "--json"])
    payload = json.loads(result.output)

    assert result.exit_code == 1
    assert payload["overall_risk"] == "custom"
    assert payload["errors"][0]["code"] == "unknown_step"
    assert payload["steps"][0]["name"] == "missing.step"
    assert payload["steps"][0]["risk"] == "custom"


def test_pipeline_plan_marks_plugin_step_as_custom(tmp_path, monkeypatch):
    package = tmp_path / "cdt_steps"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "side_effect.py").write_text(
        "\n".join(
            [
                "from pathlib import Path",
                "from cdt.sdk import step",
                "",
                "@step('demo.touch')",
                "def touch(ctx, output: str):",
                "    Path(output).write_text('executed', encoding='utf-8')",
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
                "  - cdt_steps.side_effect",
                "pipelines:",
                "  demo:",
                "    steps:",
                "      - demo.touch:",
                "          output: touched.txt",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))

    result = runner.invoke(app, ["pipeline", "plan", "demo", "--json"])
    payload = json.loads(result.output)

    assert result.exit_code == 0
    assert payload["overall_risk"] == "custom"
    assert payload["steps"][0]["name"] == "demo.touch"
    assert payload["steps"][0]["metadata"]["plugin"] is True
    assert payload["warnings"][0]["code"] == "custom_step_risk"


def test_run_executes_steps_without_dry_run(tmp_path, monkeypatch):
    package = tmp_path / "cdt_steps"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "side_effect.py").write_text(
        "\n".join(
            [
                "from pathlib import Path",
                "from cdt.sdk import step",
                "",
                "@step('demo.touch')",
                "def touch(ctx, output: str):",
                "    Path(output).write_text('executed', encoding='utf-8')",
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
                "  - cdt_steps.side_effect",
                "pipelines:",
                "  demo:",
                "    steps:",
                "      - demo.touch:",
                "          output: touched.txt",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))

    result = runner.invoke(app, ["run", "demo"])

    assert result.exit_code == 0
    assert (tmp_path / "touched.txt").read_text(encoding="utf-8") == "executed"


def test_run_dry_run_does_not_execute_steps(tmp_path, monkeypatch):
    package = tmp_path / "cdt_steps"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "side_effect.py").write_text(
        "\n".join(
            [
                "from pathlib import Path",
                "from cdt.sdk import step",
                "",
                "@step('demo.touch')",
                "def touch(ctx, output: str):",
                "    Path(output).write_text('executed', encoding='utf-8')",
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
                "  - cdt_steps.side_effect",
                "pipelines:",
                "  demo:",
                "    steps:",
                "      - demo.touch:",
                "          output: touched.txt",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))

    result = runner.invoke(app, ["run", "demo", "--dry-run"])

    assert result.exit_code == 0
    assert "Pipeline: demo" in result.output
    assert "demo.touch [custom]" in result.output
    assert not (tmp_path / "touched.txt").exists()
