import io
import json
import os

import yaml
from typer.testing import CliRunner

from cdt import agent_release, agent_release_worker
from cdt.cli import app

runner = CliRunner()


class FakeProcess:
    pid = os.getpid()


def _write_config(tmp_path):
    (tmp_path / "cdt.yaml").write_text(
        "version: 1\npipelines:\n  test:\n    steps:\n      - notify.success\n",
        encoding="utf-8",
    )


def _write_inputs_config(tmp_path):
    (tmp_path / "cdt.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "pipelines:",
                "  test:",
                "    inputs:",
                "      version:",
                "      channel:",
                "    steps:",
                "      - notify.success",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def test_agent_release_start_creates_metadata_without_streaming_log(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path)
    calls = []

    def fake_popen(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return FakeProcess()

    monkeypatch.setattr(agent_release.subprocess, "Popen", fake_popen)

    result = runner.invoke(app, ["agent-release", "start", "test", "--id", "BRH-471", "--json"])
    payload = json.loads(result.output)

    assert result.exit_code == 0
    assert payload["schema_version"] == 1
    assert payload["status"] == "running"
    assert payload["pipeline"] == "test"
    assert payload["run_id"]
    run_dir = tmp_path / ".cdt" / "runs" / payload["run_id"]
    assert payload["log"] == str(run_dir / "output.log")
    assert (run_dir / "pid").read_text(encoding="utf-8").strip() == str(os.getpid())
    meta = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert meta["ids"] == ["BRH-471"]
    assert meta["command"] == ["cdt", "run", "test", "--id", "BRH-471"]
    assert meta["worker_command"] == calls[-1][0]
    assert (tmp_path / ".cdt" / "runs" / "latest-test").read_text(encoding="utf-8").strip() == payload["run_id"]
    assert calls[-1][1]["stdout"] == agent_release.subprocess.DEVNULL
    assert calls[-1][1]["stderr"] == agent_release.subprocess.DEVNULL


def test_agent_release_start_failure_creates_terminal_redacted_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("START_TOKEN", "start-secret")
    _write_config(tmp_path)

    def fail_popen(*args, **kwargs):
        raise OSError("cannot spawn with start-secret")

    monkeypatch.setattr(agent_release.subprocess, "Popen", fail_popen)

    result = runner.invoke(app, ["agent-release", "start", "test", "--json"])
    payload = json.loads(result.output)

    assert result.exit_code == 1
    assert payload["status"] == "failed"
    assert payload["exit_code"] == 1
    assert "cannot spawn with ***" in payload["error"]
    run_dir = tmp_path / ".cdt" / "runs" / payload["run_id"]
    assert all("start-secret" not in path.read_text(encoding="utf-8") for path in run_dir.iterdir())


def test_agent_release_status_is_compact_and_does_not_read_log(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cdt_dir = tmp_path / ".cdt"
    cdt_dir.mkdir()
    (cdt_dir / "agent-release-test.pid").write_text("999999\n", encoding="utf-8")
    (cdt_dir / "agent-release-test.exit").write_text("0\n", encoding="utf-8")
    (cdt_dir / "agent-release-test.log").write_text("very noisy log\n", encoding="utf-8")
    (cdt_dir / "agent-release-test.status.json").write_text(
        json.dumps(
            {
                "current_step": None,
                "completed_steps": ["flutter.pub_get"],
                "running_steps": ["ios.flutter_build_ipa"],
                "parallel_completed": [],
                "parallel_failed": [],
                "artifacts": [],
            }
        ),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["agent-release", "status", "test"])

    assert result.exit_code == 0
    parsed = yaml.safe_load(result.output)
    assert parsed["status"] == "success"
    assert parsed["completed_steps"] == ["flutter.pub_get"]
    assert parsed["running_steps"] == ["ios.flutter_build_ipa"]
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


def test_agent_release_worker_writes_terminal_redacted_status_when_popen_fails(tmp_path, monkeypatch):
    log_path = tmp_path / ".cdt" / "agent-release-test.log"
    exit_path = tmp_path / ".cdt" / "agent-release-test.exit"
    status_path = tmp_path / ".cdt" / "agent-release-test.status.json"
    monkeypatch.setenv("DEMO_TOKEN", "worker-secret")
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
        raise OSError("cannot start with worker-secret")

    monkeypatch.setattr(agent_release_worker.subprocess, "Popen", fail_popen)

    assert agent_release_worker.main() == 1
    assert exit_path.read_text(encoding="utf-8") == "1\n"
    log = log_path.read_text(encoding="utf-8")
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    assert "cannot start with ***" in log
    assert "worker-secret" not in log
    assert payload["status"] == "failed"
    assert "cannot start with ***" in payload["error"]
    assert "worker-secret" not in status_path.read_text(encoding="utf-8")


def test_agent_release_worker_marks_missing_terminal_child_status_failed(tmp_path, monkeypatch):
    log_path = tmp_path / ".cdt" / "output.log"
    exit_path = tmp_path / ".cdt" / "exit-code"
    status_path = tmp_path / ".cdt" / "status.json"
    status_path.parent.mkdir()
    status_path.write_text(json.dumps({"status": "running"}), encoding="utf-8")
    monkeypatch.setattr(
        agent_release_worker.sys,
        "argv",
        [
            "agent_release_worker",
            "--pipeline",
            "test",
            "--run-id",
            "run-1",
            "--log",
            str(log_path),
            "--exit-file",
            str(exit_path),
            "--status-file",
            str(status_path),
        ],
    )

    class CrashedProcess:
        stdout = io.BytesIO(b"abrupt child exit\n")

        def wait(self):
            return 7

    monkeypatch.setattr(agent_release_worker.subprocess, "Popen", lambda *args, **kwargs: CrashedProcess())

    assert agent_release_worker.main() == 7
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["error"] == "CDT subprocess exited with code 7 before writing a terminal status"
    assert exit_path.read_text(encoding="utf-8") == "7\n"


def test_agent_release_stop_handles_missing_pid(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["agent-release", "stop", "test", "--json"])
    payload = json.loads(result.output)

    assert result.exit_code == 0
    assert payload["status"] == "unknown"
    assert payload["stop_result"] == "missing_pid"


def test_agent_release_start_propagates_inputs_to_worker_and_manifest(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_inputs_config(tmp_path)
    calls = []

    def fake_popen(cmd, **kwargs):
        calls.append(cmd)
        return FakeProcess()

    monkeypatch.setattr(agent_release.subprocess, "Popen", fake_popen)

    result = runner.invoke(
        app,
        ["agent-release", "start", "test", "--input", "version=0.5.2", "--id", "BRH-1", "--json"],
    )
    payload = json.loads(result.output)

    assert result.exit_code == 0
    run_dir = tmp_path / ".cdt" / "runs" / payload["run_id"]
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["inputs"] == {"version": "0.5.2"}
    assert manifest["command"] == ["cdt", "run", "test", "--input", "version=0.5.2", "--id", "BRH-1"]
    worker_cmd = calls[-1]
    assert worker_cmd[worker_cmd.index("--input") + 1] == "version=0.5.2"
    initial_status = json.loads((run_dir / "status.json").read_text(encoding="utf-8"))
    assert initial_status["inputs"] == {"version": "0.5.2"}


def test_agent_release_start_rejects_unknown_input(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path)

    result = runner.invoke(app, ["agent-release", "start", "test", "--input", "version=0.5.2"])

    assert result.exit_code != 0
    assert "Unknown pipeline input" in result.output
    assert not (tmp_path / ".cdt" / "runs").exists()


def test_agent_release_start_requires_declared_input(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "cdt.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "pipelines:",
                "  test:",
                "    inputs:",
                "      version:",
                "        required: true",
                "    steps:",
                "      - notify.success",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["agent-release", "start", "test"])

    assert result.exit_code != 0
    assert "Missing required pipeline input" in result.output
    assert not (tmp_path / ".cdt" / "runs").exists()


def test_agent_release_worker_passes_inputs_to_cdt_run(tmp_path, monkeypatch):
    log_path = tmp_path / ".cdt" / "agent-release-test.log"
    exit_path = tmp_path / ".cdt" / "agent-release-test.exit"
    status_path = tmp_path / ".cdt" / "agent-release-test.status.json"
    captured = {}

    def fail_popen(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        raise OSError("stop before start")

    monkeypatch.setattr(agent_release_worker.subprocess, "Popen", fail_popen)
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
            "--input",
            "version=0.5.2",
            "--input",
            "channel=beta",
        ],
    )

    assert agent_release_worker.main() == 1

    cmd = captured["cmd"]
    inputs = [cmd[index + 1] for index, flag in enumerate(cmd) if flag == "--input"]
    assert inputs == ["version=0.5.2", "channel=beta"]


def test_agent_release_status_includes_inputs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cdt_dir = tmp_path / ".cdt"
    cdt_dir.mkdir()
    (cdt_dir / "agent-release-test.exit").write_text("0\n", encoding="utf-8")
    (cdt_dir / "agent-release-test.status.json").write_text(
        json.dumps({"status": "success", "inputs": {"version": "0.5.2"}}),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["agent-release", "status", "test", "--json"])
    payload = json.loads(result.output)

    assert result.exit_code == 0
    assert payload["inputs"] == {"version": "0.5.2"}
