import sys
import tempfile
from pathlib import Path

import pytest
import typer

from cdt.pipeline import PipelineContext
from cdt.pipeline.config import load_pipeline_config, resolve_value
from cdt.runner import _run
from cdt.steps.flutter import IncrementFlutterBuildNumberStep
from cdt.steps.notify import NotifySuccessStep


class NoopRunner:
    def run(self, command: list[str], *, cwd: Path) -> int:
        return 0


def test_notify_success_default_pipeline_name_and_custom_message(tmp_path, monkeypatch, capsys):
    sent: list[tuple[str, list[str] | None]] = []

    def fake_notify(env, new_version, ids=None):
        sent.append((new_version, ids))

    monkeypatch.setattr("cdt.steps.notify._notify_success", fake_notify)
    ctx = PipelineContext(cwd=tmp_path, env={}, runner=NoopRunner(), ids=["APP-1"], pipeline_name="prod")
    ctx.new_version = "1.2.3+4"

    NotifySuccessStep(include_ids=True).run(ctx)
    NotifySuccessStep(message="Deploy finished").run(ctx)

    out = capsys.readouterr().out
    assert "✅ Pipeline 'prod' completed" in out
    assert "✅ Deploy finished" in out
    assert "iOS TestFlight flow" not in out
    assert sent == [("1.2.3+4", ["APP-1"]), ("1.2.3+4", None)]


def test_flutter_increment_build_number_does_not_write_legacy_aliases(tmp_path):
    (tmp_path / "pubspec.yaml").write_text("name: app\nversion: 1.2.3+4\n", encoding="utf-8")
    ctx = PipelineContext(cwd=tmp_path, env={}, runner=NoopRunner())

    IncrementFlutterBuildNumberStep().run(ctx)

    assert ctx.values["flutter.version.old"] == "1.2.3+4"
    assert ctx.values["flutter.version"] == "1.2.3+5"
    assert ctx.values["flutter.build_number"] == "5"
    assert "flutter_version_old" not in ctx.values
    assert "flutter_version" not in ctx.values


def test_flutter_version_interpolation_reads_pubspec_without_mutating_values(tmp_path):
    (tmp_path / "pubspec.yaml").write_text("name: app\nversion: 2.0.0+7\n", encoding="utf-8")
    ctx = PipelineContext(cwd=tmp_path, env={}, runner=NoopRunner())

    assert resolve_value("release ${flutter.version}", ctx) == "release 2.0.0+7"
    assert ctx.values == {}


def test_load_pipeline_config_missing_file_points_to_example(tmp_path):
    with pytest.raises(typer.BadParameter, match="See examples/cdt.yaml"):
        load_pipeline_config(tmp_path)


def test_run_deletes_temp_log_on_success(tmp_path, monkeypatch):
    original = tempfile.NamedTemporaryFile

    def named_temp_file_in_tmp(*args, **kwargs):
        kwargs["dir"] = tmp_path
        return original(*args, **kwargs)

    monkeypatch.setattr("cdt.runner.tempfile.NamedTemporaryFile", named_temp_file_in_tmp)

    assert _run([sys.executable, "-c", "print('ok')"], cwd=tmp_path) == 0
    assert list(tmp_path.glob("cdt-*.log")) == []


def test_run_keeps_failed_temp_log_and_reports_path(tmp_path, monkeypatch, capsys):
    original = tempfile.NamedTemporaryFile

    def named_temp_file_in_tmp(*args, **kwargs):
        kwargs["dir"] = tmp_path
        return original(*args, **kwargs)

    monkeypatch.setattr("cdt.runner.tempfile.NamedTemporaryFile", named_temp_file_in_tmp)

    assert _run([sys.executable, "-c", "import sys; print('boom'); sys.exit(3)"], cwd=tmp_path) == 3

    logs = list(tmp_path.glob("cdt-*.log"))
    assert len(logs) == 1
    err = capsys.readouterr().err
    assert f"Full log: {logs[0]}" in err
    assert "boom" in err
