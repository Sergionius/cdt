import json
import sys

from typer.testing import CliRunner

from cdt.cli import app
from cdt.pipeline.registry import _clear_steps_for_tests

runner = CliRunner()


def setup_function():
    _clear_steps_for_tests()
    sys.modules.pop("cdt_steps.artifacts", None)
    sys.modules.pop("cdt_steps.side_effect", None)
    sys.modules.pop("cdt_steps", None)


def teardown_function():
    _clear_steps_for_tests()
    sys.modules.pop("cdt_steps.artifacts", None)
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
    assert "name" not in payload["steps"][0]["metadata"]
    assert "category" not in payload["steps"][0]["metadata"]
    assert "risk" not in payload["steps"][0]["metadata"]
    assert payload["steps"][1]["type"] == "parallel"
    assert payload["steps"][1]["risk"] == "build"
    assert payload["steps"][1]["steps"][0]["artifact_flow"] == {
        "requires": [],
        "requires_names": [],
        "produces_names": ["ios_ipa"],
        "produces_types": ["ios_ipa"],
    }
    assert payload["steps"][1]["steps"][1]["artifact_flow"] == {
        "requires": [],
        "requires_names": [],
        "produces_names": ["android_aab"],
        "produces_types": ["android_aab"],
    }
    assert payload["steps"][2]["risk"] == "upload"
    assert payload["steps"][2]["artifact_flow"] == {
        "requires": [
            {"types": ["ios_ipa"], "mode": "all", "names": ["ios_ipa"]},
        ],
        "requires_names": ["ios_ipa"],
        "produces_names": [],
        "produces_types": ["upload_result"],
    }
    assert payload["warnings"] == []


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


def test_pipeline_plan_json_warns_for_missing_artifact_name(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "cdt.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "pipelines:",
                "  demo:",
                "    steps:",
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
    assert payload["warnings"] == [
        {
            "code": "missing_required_artifact",
            "message": (
                "Step appstore.upload_testflight requires artifact name ios_ipa, "
                "but no previous step declares it."
            ),
            "path": "pipelines.demo.steps[0]",
        }
    ]


def test_pipeline_plan_json_warns_for_parallel_sibling_artifact_dependency(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "cdt.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "pipelines:",
                "  demo:",
                "    steps:",
                "      - parallel:",
                "          steps:",
                "            - ios.flutter_build_ipa:",
                "                artifact: ios_ipa",
                "            - appstore.upload_testflight:",
                "                artifact: ios_ipa",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["pipeline", "plan", "demo", "--json"])
    payload = json.loads(result.output)

    assert result.exit_code == 0
    assert payload["warnings"] == [
        {
            "code": "parallel_artifact_dependency",
            "message": (
                "Step appstore.upload_testflight requires artifact name ios_ipa from the same parallel group; "
                "parallel branches start together."
            ),
            "path": "pipelines.demo.steps[0].parallel.steps[1]",
        }
    ]


def test_pipeline_plan_json_ignores_dynamic_or_non_string_artifact_options(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "cdt.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "pipelines:",
                "  demo:",
                "    steps:",
                "      - appstore.upload_testflight:",
                "          artifact: ${values.ios_artifact}",
                "      - firebase.upload_app_distribution:",
                "          artifact:",
                "            - android_aab",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["pipeline", "plan", "demo", "--json"])
    payload = json.loads(result.output)

    assert result.exit_code == 0
    assert payload["steps"][0]["artifact_flow"]["requires_names"] == []
    assert payload["steps"][1]["artifact_flow"]["requires_names"] == []
    assert payload["warnings"] == []


def test_pipeline_plan_json_artifact_flow_grouped_requires(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "cdt.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "pipelines:",
                "  demo:",
                "    steps:",
                "      - firebase.upload_app_distribution:",
                "          artifact: android_aab",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["pipeline", "plan", "demo", "--json"])
    payload = json.loads(result.output)

    assert result.exit_code == 0
    assert payload["steps"][0]["artifact_flow"]["requires"] == [
        {"types": ["android_aab", "android_apk"], "mode": "any", "names": ["android_aab"]}
    ]
    assert payload["steps"][0]["artifact_flow"]["requires_names"] == ["android_aab"]


def test_pipeline_plan_json_any_requirement_allows_one_available_name(tmp_path, monkeypatch):
    _write_any_artifact_plugin(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    (tmp_path / "cdt.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "plugins:",
                "  - cdt_steps.artifacts",
                "pipelines:",
                "  demo:",
                "    steps:",
                "      - demo.produce_a:",
                "          artifact_a: app_a",
                "      - demo.consume_any:",
                "          artifact_a: app_a",
                "          artifact_b: app_b",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["pipeline", "plan", "demo", "--json"])
    payload = json.loads(result.output)

    assert result.exit_code == 0
    assert payload["warnings"] == []


def test_pipeline_plan_json_any_requirement_warns_once_for_parallel_sibling_names(tmp_path, monkeypatch):
    _write_any_artifact_plugin(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    (tmp_path / "cdt.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "plugins:",
                "  - cdt_steps.artifacts",
                "pipelines:",
                "  demo:",
                "    steps:",
                "      - parallel:",
                "          steps:",
                "            - demo.produce_a:",
                "                artifact_a: app_a",
                "            - demo.consume_any:",
                "                artifact_a: app_a",
                "                artifact_b: app_b",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["pipeline", "plan", "demo", "--json"])
    payload = json.loads(result.output)

    assert result.exit_code == 0
    assert payload["warnings"] == [
        {
            "code": "parallel_artifact_dependency",
            "message": (
                "Step demo.consume_any requires one of artifact names: app_a, app_b, "
                "but matching artifacts are produced in the same parallel group; "
                "parallel branches start together."
            ),
            "path": "pipelines.demo.steps[0].parallel.steps[1]",
        }
    ]


def test_pipeline_plan_json_any_requirement_warns_once_for_missing_names(tmp_path, monkeypatch):
    _write_any_artifact_plugin(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    (tmp_path / "cdt.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "plugins:",
                "  - cdt_steps.artifacts",
                "pipelines:",
                "  demo:",
                "    steps:",
                "      - demo.consume_any:",
                "          artifact_a: app_a",
                "          artifact_b: app_b",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["pipeline", "plan", "demo", "--json"])
    payload = json.loads(result.output)

    assert result.exit_code == 0
    assert payload["warnings"] == [
        {
            "code": "missing_required_artifact",
            "message": (
                "Step demo.consume_any requires one of artifact names: app_a, app_b, "
                "but no previous step declares any of them."
            ),
            "path": "pipelines.demo.steps[0]",
        }
    ]


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


def _write_any_artifact_plugin(tmp_path):
    package = tmp_path / "cdt_steps"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "artifacts.py").write_text(
        "\n".join(
            [
                "from cdt.sdk import ResultProduction, ResultRequirement, step",
                "",
                "@step(",
                "    'demo.produce_a',",
                "    category='demo',",
                "    risk='artifact',",
                "    produces=[ResultProduction('demo_artifact', name_options=['artifact_a'])],",
                ")",
                "def produce_a(ctx, artifact_a: str):",
                "    pass",
                "",
                "@step(",
                "    'demo.consume_any',",
                "    category='demo',",
                "    risk='upload',",
                "    requires=[",
                "        ResultRequirement(",
                "            ('demo_artifact_a', 'demo_artifact_b'),",
                "            mode='any',",
                "            name_options=['artifact_a', 'artifact_b'],",
                "        )",
                "    ],",
                ")",
                "def consume_any(ctx, artifact_a: str, artifact_b: str):",
                "    pass",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
