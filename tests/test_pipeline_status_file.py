import json
import sys

from typer.testing import CliRunner

from cdt.cli import app
from cdt.pipeline.registry import _clear_steps_for_tests

runner = CliRunner()


def setup_function():
    _clear_steps_for_tests()
    sys.modules.pop("cdt_steps.demo", None)
    sys.modules.pop("cdt_steps", None)


def teardown_function():
    _clear_steps_for_tests()
    sys.modules.pop("cdt_steps.demo", None)
    sys.modules.pop("cdt_steps", None)


def _write_demo_project(tmp_path, *, failing: bool = False) -> None:
    package = tmp_path / "cdt_steps"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "demo.py").write_text(
        "\n".join(
            [
                "import typer",
                "from cdt.sdk import step",
                "",
                "@step('demo.ok')",
                "def ok(ctx):",
                "    ctx.values['ok'] = '1'",
                "",
                "@step('demo.fail')",
                "def fail(ctx):",
                "    raise typer.BadParameter('boom')",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    steps = ["demo.ok", "demo.fail"] if failing else ["demo.ok"]
    (tmp_path / "cdt.yaml").write_text(
        "version: 1\nplugins:\n  - cdt_steps.demo\npipelines:\n  demo:\n    steps:\n"
        + "".join(f"      - {step}\n" for step in steps),
        encoding="utf-8",
    )


def test_run_status_file_records_success(tmp_path, monkeypatch):
    _write_demo_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    status_file = tmp_path / ".cdt" / "status.json"

    result = runner.invoke(app, ["run", "demo", "--status-file", str(status_file)])
    payload = json.loads(status_file.read_text(encoding="utf-8"))

    assert result.exit_code == 0
    assert payload["status"] == "success"
    assert payload["pipeline"] == "demo"
    assert payload["completed_steps"] == ["demo.ok"]
    assert payload["current_step"] is None
    assert payload["started_at"]
    assert payload["finished_at"]


def test_run_status_file_records_failure(tmp_path, monkeypatch):
    _write_demo_project(tmp_path, failing=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    status_file = tmp_path / ".cdt" / "status.json"

    result = runner.invoke(app, ["run", "demo", "--status-file", str(status_file)])
    payload = json.loads(status_file.read_text(encoding="utf-8"))

    assert result.exit_code != 0
    assert payload["status"] == "failed"
    assert payload["completed_steps"] == ["demo.ok"]
    assert payload["failed_step"] == "demo.fail"
    assert "boom" in payload["error"]
