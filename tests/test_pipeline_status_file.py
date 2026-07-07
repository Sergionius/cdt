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


def _write_demo_project(tmp_path, *, failing: bool = False, artifact: bool = False) -> None:
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
                "",
                "@step('demo.artifact')",
                "def artifact(ctx):",
                "    from cdt.artifacts import ArtifactKind, BuildArtifact",
                "    count = ctx.cwd / 'build-count.txt'",
                "    value = int(count.read_text()) if count.exists() else 0",
                "    count.write_text(str(value + 1), encoding='utf-8')",
                "    path = ctx.cwd / 'app.aab'",
                "    path.write_text('artifact', encoding='utf-8')",
                "    ctx.register_artifact('app', BuildArtifact(ArtifactKind.AAB, path, 'App'))",
                "",
                "@step('demo.upload')",
                "def upload(ctx):",
                "    ctx.artifact('app')",
                "    count = ctx.cwd / 'upload-count.txt'",
                "    value = int(count.read_text()) if count.exists() else 0",
                "    count.write_text(str(value + 1), encoding='utf-8')",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    if artifact:
        steps = ["demo.artifact", "demo.upload"]
    else:
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


def test_run_resume_from_restores_artifacts_and_skips_prior_steps(tmp_path, monkeypatch):
    _write_demo_project(tmp_path, artifact=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    status_file = tmp_path / ".cdt" / "status.json"

    first = runner.invoke(app, ["run", "demo", "--status-file", str(status_file)])
    second = runner.invoke(app, ["run", "demo", "--status-file", str(status_file), "--resume-from", "demo.upload"])

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert (tmp_path / "build-count.txt").read_text(encoding="utf-8") == "1"
    assert (tmp_path / "upload-count.txt").read_text(encoding="utf-8") == "2"


def test_run_skip_completed_does_not_execute_completed_steps(tmp_path, monkeypatch):
    _write_demo_project(tmp_path, artifact=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    status_file = tmp_path / ".cdt" / "status.json"

    first = runner.invoke(app, ["run", "demo", "--status-file", str(status_file)])
    second = runner.invoke(app, ["run", "demo", "--status-file", str(status_file), "--skip-completed"])

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert (tmp_path / "build-count.txt").read_text(encoding="utf-8") == "1"
    assert (tmp_path / "upload-count.txt").read_text(encoding="utf-8") == "1"


def test_run_resume_fails_when_restored_artifact_is_missing(tmp_path, monkeypatch):
    _write_demo_project(tmp_path, artifact=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    status_file = tmp_path / ".cdt" / "status.json"

    first = runner.invoke(app, ["run", "demo", "--status-file", str(status_file)])
    (tmp_path / "app.aab").unlink()
    second = runner.invoke(app, ["run", "demo", "--status-file", str(status_file), "--resume-from", "demo.upload"])

    assert first.exit_code == 0
    assert second.exit_code != 0
    assert "Resume artifact does not exist" in second.output
