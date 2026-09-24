import json
import sys

from typer.testing import CliRunner

from cdt import cli, orca_status
from cdt.cli import app
from cdt.pipeline.executor import PipelineExecutionError
from cdt.pipeline.registry import _clear_steps_for_tests
from cdt.user_settings import orca_status_enabled, settings_path
from tests.test_agent_first import _write_project

runner = CliRunner()


def teardown_function():
    _clear_steps_for_tests()
    sys.modules.pop("cdt_steps.demo", None)
    sys.modules.pop("cdt_steps", None)


def test_global_settings_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert not orca_status_enabled()
    assert runner.invoke(app, ["settings", "enable", "experimental.orca-status"]).exit_code == 0
    assert orca_status_enabled()
    assert json.loads(settings_path().read_text())["experimental"]["orca_status"] is True
    assert "enabled" in runner.invoke(app, ["settings", "show"]).output
    assert runner.invoke(app, ["settings", "disable", "experimental.orca-status"]).exit_code == 0
    assert not orca_status_enabled()
    assert runner.invoke(app, ["settings", "enable", "unknown"]).exit_code != 0


def test_settings_keep_other_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = settings_path()
    path.parent.mkdir(parents=True)
    path.write_text('{"other": 1, "experimental": {"another": true}}')
    runner.invoke(app, ["settings", "enable", "experimental.orca-status"])
    assert json.loads(path.read_text()) == {"other": 1, "experimental": {"another": True, "orca_status": True}}


def test_osc_only_in_orca_manual_tty(monkeypatch):
    writes = []

    class Tty:
        def isatty(self):
            return True

        def fileno(self):
            return 42

    monkeypatch.setattr(orca_status, "orca_status_enabled", lambda: True)
    monkeypatch.setenv("ORCA_PANE_KEY", "pane")
    monkeypatch.delenv("ORCA_PI_STATUS_OWNED", raising=False)
    monkeypatch.delenv("PI_SESSION_ID", raising=False)
    monkeypatch.setattr(orca_status.sys, "stdout", Tty())
    monkeypatch.setattr(orca_status.os, "write", lambda fd, data: writes.append((fd, data)))
    orca_status.report("working", "test")
    orca_status.report("done", "test", failed=True)
    assert [json.loads(data[7:-1])["state"] for _, data in writes] == ["working", "done"]
    assert b"failed" in writes[-1][1]
    monkeypatch.setenv("PI_SESSION_ID", "pi")
    orca_status.report("working", "test")
    assert len(writes) == 2
    monkeypatch.delenv("PI_SESSION_ID")
    monkeypatch.delenv("ORCA_PANE_KEY")
    orca_status.report("working", "test")
    assert len(writes) == 2


def test_direct_run_reports_lifecycle_only_after_preflight(tmp_path, monkeypatch):
    _write_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    events = []
    monkeypatch.setattr(cli, "report_orca_status", lambda *args, **kwargs: events.append((args, kwargs)))
    assert runner.invoke(app, ["run", "test", "--dry-run"]).exit_code == 0
    assert events == []
    assert runner.invoke(app, ["run", "test"]).exit_code == 0
    assert [args[0] for args, _ in events] == ["working", "done"]
    assert events[-1][1]["failed"] is False
    events.clear()
    def fail_pipeline(*args, **kwargs):
        raise PipelineExecutionError("failed")

    monkeypatch.setattr(cli, "run_configured_pipeline", fail_pipeline)
    assert runner.invoke(app, ["run", "test"]).exit_code == 1
    assert [args[0] for args, _ in events] == ["working", "done"]
    assert events[-1][1]["failed"] is True


def test_no_osc_with_redirected_output(monkeypatch):
    monkeypatch.setattr(orca_status, "orca_status_enabled", lambda: True)
    monkeypatch.setenv("ORCA_PANE_KEY", "pane")
    monkeypatch.setattr(orca_status.sys, "stdout", type("Pipe", (), {"isatty": lambda self: False})())
    monkeypatch.setattr(orca_status.os, "write", lambda *_: (_ for _ in ()).throw(AssertionError("wrote OSC")))
    orca_status.report("working", "test")
