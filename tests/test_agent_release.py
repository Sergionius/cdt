import json
import os

import yaml
from typer.testing import CliRunner

from cdt import agent_release, agent_release_worker
from cdt.cli import app

runner = CliRunner()


class FakeProcess:
    pid = os.getpid()


def test_agent_release_start_creates_metadata_without_streaming_log(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = []

    def fake_popen(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return FakeProcess()

    monkeypatch.setattr(agent_release.subprocess, "Popen", fake_popen)

    result = runner.invoke(app, ["agent-release", "start", "test", "--id", "BRH-471", "--json"])
    payload = json.loads(result.output)

    assert result.exit_code == 0
    assert payload["status"] == "running"
    assert payload["pipeline"] == "test"
    assert payload["log"] == str(tmp_path / ".cdt" / "agent-release-test.log")
    assert (tmp_path / ".cdt" / "agent-release-test.pid").read_text(encoding="utf-8").strip() == str(os.getpid())
    meta = json.loads((tmp_path / ".cdt" / "agent-release-test.meta.json").read_text(encoding="utf-8"))
    assert meta["ids"] == ["BRH-471"]
    assert meta["command"] == ["cdt", "run", "test", "--id", "BRH-471"]
    assert meta["worker_command"] == calls[0][0]
    assert calls[0][1]["stdout"] == agent_release.subprocess.DEVNULL
    assert calls[0][1]["stderr"] == agent_release.subprocess.DEVNULL


def test_agent_release_status_is_compact_and_does_not_read_log(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cdt_dir = tmp_path / ".cdt"
    cdt_dir.mkdir()
    (cdt_dir / "agent-release-test.pid").write_text("999999\n", encoding="utf-8")
    (cdt_dir / "agent-release-test.exit").write_text("0\n", encoding="utf-8")
    (cdt_dir / "agent-release-test.log").write_text("very noisy log\n", encoding="utf-8")
    (cdt_dir / "agent-release-test.status.json").write_text(
        json.dumps({"current_step": None, "completed_steps": ["flutter.pub_get"], "artifacts": []}),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["agent-release", "status", "test"])

    assert result.exit_code == 0
    parsed = yaml.safe_load(result.output)
    assert parsed["status"] == "success"
    assert parsed["completed_steps"] == ["flutter.pub_get"]
    assert "very noisy log" not in result.output


def test_agent_release_wait_returns_final_status(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cdt_dir = tmp_path / ".cdt"
    cdt_dir.mkdir()
    (cdt_dir / "agent-release-test.exit").write_text("1\n", encoding="utf-8")

    result = runner.invoke(app, ["agent-release", "status", "test", "--wait", "--timeout", "1s", "--json"])
    payload = json.loads(result.output)

    assert result.exit_code == 0
    assert payload["status"] == "failed"
    assert payload["exit_code"] == 1


def test_agent_release_wait_timeout_preserves_running_status(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cdt_dir = tmp_path / ".cdt"
    cdt_dir.mkdir()
    (cdt_dir / "agent-release-test.pid").write_text(f"{os.getpid()}\n", encoding="utf-8")

    result = runner.invoke(app, ["agent-release", "status", "test", "--wait", "--timeout", "0", "--json"])
    payload = json.loads(result.output)

    assert result.exit_code == 0
    assert payload["status"] == "running"
    assert payload["wait_status"] == "timeout"


def test_agent_release_worker_writes_exit_file_when_popen_fails(tmp_path, monkeypatch):
    log_path = tmp_path / ".cdt" / "agent-release-test.log"
    exit_path = tmp_path / ".cdt" / "agent-release-test.exit"
    status_path = tmp_path / ".cdt" / "agent-release-test.status.json"
    monkeypatch.setattr(
        agent_release_worker.sys,
        "argv",
        [
            "agent_release_worker",
            "--pipeline",
            "test",
            "--log",
            str(log_path),
            "--exit-file",
            str(exit_path),
            "--status-file",
            str(status_path),
        ],
    )

    def fail_popen(*args, **kwargs):
        raise OSError("cannot start")

    monkeypatch.setattr(agent_release_worker.subprocess, "Popen", fail_popen)

    assert agent_release_worker.main() == 1
    assert exit_path.read_text(encoding="utf-8") == "1\n"
    assert "cannot start" in log_path.read_text(encoding="utf-8")


def test_agent_release_stop_handles_missing_pid(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["agent-release", "stop", "test", "--json"])
    payload = json.loads(result.output)

    assert result.exit_code == 0
    assert payload["status"] == "unknown"
    assert payload["stop_result"] == "missing_pid"
