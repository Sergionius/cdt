import json
import signal
import subprocess
from pathlib import Path

import pytest
import typer

from cdt import config, runner
from cdt.pipeline.runner import run_configured_pipeline


class FakePopen:
    def __init__(self, command, cwd=None, stdin=None, stdout=None, stderr=None, returncode=0):
        self.command = command
        self.cwd = cwd
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode

    def wait(self):
        return self.returncode


def test_tail_text_returns_last_lines(tmp_path):
    path = tmp_path / "log.txt"
    path.write_text("one\ntwo\nthree\n", encoding="utf-8")

    assert runner._tail_text(path, lines=2) == "two\nthree"


def test_tail_text_returns_empty_string_on_read_error(tmp_path):
    assert runner._tail_text(tmp_path / "missing.log") == ""


def test_run_verbose_uses_popen_without_temp_log(tmp_path, monkeypatch, capsys):
    calls = []

    def fake_popen(command, cwd, **kwargs):
        calls.append((command, cwd, kwargs))
        return FakePopen(command, cwd=cwd, returncode=7)

    monkeypatch.setattr(config, "UI_MODE", "verbose")
    monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)

    assert runner._run(["echo", "hello world"], cwd=tmp_path) == 7
    assert calls == [(["echo", "hello world"], tmp_path, {})]
    assert "$ echo 'hello world'" in capsys.readouterr().out


def test_run_passes_additional_environment_without_mutating_parent(tmp_path, monkeypatch):
    calls = []

    def fake_popen(command, cwd, env):
        calls.append(env)
        return FakePopen(command, cwd=cwd)

    monkeypatch.setenv("PARENT_VALUE", "parent")
    original_credentials = __import__("os").environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    monkeypatch.setattr(config, "UI_MODE", "verbose")
    monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)

    assert runner._run(["cmd"], cwd=tmp_path, env={"GOOGLE_APPLICATION_CREDENTIALS": "/key.json"}) == 0
    assert calls[0]["PARENT_VALUE"] == "parent"
    assert calls[0]["GOOGLE_APPLICATION_CREDENTIALS"] == "/key.json"
    assert __import__("os").environ.get("GOOGLE_APPLICATION_CREDENTIALS") == original_credentials


def test_spawn_verbose_returns_process_without_log(tmp_path, monkeypatch):
    fake = FakePopen(["cmd"], cwd=tmp_path)
    monkeypatch.setattr(config, "UI_MODE", "verbose")
    monkeypatch.setattr(runner.subprocess, "Popen", lambda command, cwd: fake)

    proc, log_path = runner._spawn(["cmd"], cwd=tmp_path)

    assert proc is fake
    assert log_path is None


def test_spawn_non_verbose_redirects_to_temp_log(tmp_path, monkeypatch):
    calls = []

    def fake_popen(command, cwd, stdin, stdout, stderr):
        calls.append((command, cwd, stdin, stdout, stderr))
        return FakePopen(command, cwd=cwd, stdin=stdin, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(config, "UI_MODE", "quiet")
    monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)

    proc, log_path = runner._spawn(["cmd", "arg"], cwd=tmp_path)

    assert proc.command == ["cmd", "arg"]
    assert log_path is not None
    assert log_path.name.startswith("cdt-")
    assert calls[0][2] == subprocess.DEVNULL
    assert calls[0][4] == subprocess.STDOUT


def test_command_runner_delegates_to_helpers(tmp_path, monkeypatch):
    command_runner = runner.CommandRunner()
    fake_proc = FakePopen(["spawn"])
    monkeypatch.setattr(runner, "_run", lambda command, cwd: 3)
    monkeypatch.setattr(runner, "_spawn", lambda command, cwd: (fake_proc, Path("log")))
    monkeypatch.setattr(runner, "_tail_text", lambda path, lines: "tail")

    assert command_runner.run(["run"], cwd=tmp_path) == 3
    spawned = command_runner.spawn(["spawn"], cwd=tmp_path)
    assert spawned.proc is fake_proc
    assert spawned.log_path == Path("log")
    assert command_runner.tail(Path("log"), lines=5) == "tail"


def test_prepare_git_clean_main_runs_restore_clean_and_checkout_main(tmp_path, monkeypatch):
    calls = []

    def fake_run(command, cwd):
        calls.append((command, cwd))
        return 0

    monkeypatch.setattr(runner, "_run", fake_run)

    runner._prepare_git_clean_main(tmp_path)

    assert calls == [
        (["git", "rev-parse", "--is-inside-work-tree"], tmp_path),
        (["git", "restore", "."], tmp_path),
        (["git", "clean", "-fd"], tmp_path),
        (["git", "checkout", "main"], tmp_path),
    ]


def test_prepare_git_clean_main_falls_back_to_master(tmp_path, monkeypatch):
    responses = iter([0, 0, 0, 1, 0])
    calls = []

    def fake_run(command, cwd):
        calls.append(command)
        return next(responses)

    monkeypatch.setattr(runner, "_run", fake_run)

    runner._prepare_git_clean_main(tmp_path)

    assert calls[-2:] == [["git", "checkout", "main"], ["git", "checkout", "master"]]


def test_prepare_git_clean_main_reports_failures(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_run", lambda command, cwd: 1)
    with pytest.raises(typer.BadParameter, match="Not a git repository"):
        runner._prepare_git_clean_main(tmp_path)


class ScriptedProc:
    def __init__(self, pid, waits):
        self.pid = pid
        self.waits = list(waits)
        self.wait_calls = []

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        outcome = self.waits.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _patch_popen(monkeypatch, proc):
    calls = []

    def fake_popen(command, cwd=None, **kwargs):
        calls.append((command, cwd, kwargs))
        return proc

    monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)
    return calls


def _patch_killpg(monkeypatch):
    signals = []
    monkeypatch.setattr(runner.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    return signals


def test_run_managed_subprocess_returns_exit_code_and_uses_process_group(tmp_path, monkeypatch):
    proc = ScriptedProc(4242, [0])
    calls = _patch_popen(monkeypatch, proc)

    code = runner.run_managed_subprocess(["hook"], cwd=tmp_path, env={"A": "1"}, timeout=None)

    assert code == 0
    if runner.supports_process_groups():
        assert calls == [(["hook"], tmp_path, {"env": {"A": "1"}, "start_new_session": True})]
    else:
        assert calls == [(["hook"], tmp_path, {"env": {"A": "1"}})]
    assert proc.wait_calls == [None]


def test_run_managed_subprocess_timeout_terms_group_then_reraises(tmp_path, monkeypatch):
    timeout_error = subprocess.TimeoutExpired(cmd=["hook"], timeout=3)
    proc = ScriptedProc(4242, [timeout_error, -15])
    _patch_popen(monkeypatch, proc)
    signals = _patch_killpg(monkeypatch)

    with pytest.raises(subprocess.TimeoutExpired):
        runner.run_managed_subprocess(["hook"], cwd=tmp_path, timeout=3)

    if runner.supports_process_groups():
        assert signals == [(4242, signal.SIGTERM)]
        assert proc.wait_calls == [3, runner.PROCESS_GROUP_TERMINATE_GRACE_SECONDS]


def test_run_managed_subprocess_kills_group_after_bounded_grace(tmp_path, monkeypatch):
    timeout_error = subprocess.TimeoutExpired(cmd=["hook"], timeout=3)
    proc = ScriptedProc(4242, [timeout_error, timeout_error, -9])
    _patch_popen(monkeypatch, proc)
    signals = _patch_killpg(monkeypatch)

    with pytest.raises(subprocess.TimeoutExpired):
        runner.run_managed_subprocess(["hook"], cwd=tmp_path, timeout=3)

    if runner.supports_process_groups():
        assert signals == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]
        assert proc.wait_calls[-1] == runner.PROCESS_GROUP_TERMINATE_GRACE_SECONDS


def test_run_managed_subprocess_does_not_hide_failed_cleanup(tmp_path, monkeypatch):
    timeout_error = subprocess.TimeoutExpired(cmd=["hook"], timeout=3)
    proc = ScriptedProc(4242, [timeout_error, timeout_error, timeout_error])
    _patch_popen(monkeypatch, proc)
    _patch_killpg(monkeypatch)

    with pytest.raises(RuntimeError, match="Failed to reap managed subprocess"):
        runner.run_managed_subprocess(["hook"], cwd=tmp_path, timeout=3)


def test_run_managed_subprocess_cleans_up_group_on_interrupted_wait(tmp_path, monkeypatch):
    proc = ScriptedProc(4242, [KeyboardInterrupt(), -15])
    _patch_popen(monkeypatch, proc)
    signals = _patch_killpg(monkeypatch)

    with pytest.raises(KeyboardInterrupt):
        runner.run_managed_subprocess(["hook"], cwd=tmp_path, timeout=60)

    if runner.supports_process_groups():
        assert signals == [(4242, signal.SIGTERM)]


def test_run_managed_subprocess_timeout_requires_process_groups(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "supports_process_groups", lambda: False)
    calls = _patch_popen(monkeypatch, ScriptedProc(1, [0]))

    with pytest.raises(RuntimeError, match="POSIX process groups"):
        runner.run_managed_subprocess(["hook"], cwd=tmp_path, timeout=3)
    assert calls == []

    # Without a timeout the plain legacy path still works on any platform.
    assert runner.run_managed_subprocess(["hook"], cwd=tmp_path) == 0
    assert calls[0][2] == {}

    responses = iter([0, 1])
    monkeypatch.setattr(runner, "_run", lambda command, cwd: next(responses))
    with pytest.raises(typer.BadParameter, match="Failed to restore tracked files"):
        runner._prepare_git_clean_main(tmp_path)

    responses = iter([0, 0, 1])
    monkeypatch.setattr(runner, "_run", lambda command, cwd: next(responses))
    with pytest.raises(typer.BadParameter, match="Failed to clean untracked files"):
        runner._prepare_git_clean_main(tmp_path)

    responses = iter([0, 0, 0, 1, 1])
    monkeypatch.setattr(runner, "_run", lambda command, cwd: next(responses))
    with pytest.raises(typer.BadParameter, match="Neither main nor master"):
        runner._prepare_git_clean_main(tmp_path)


def _write_rollback_project(tmp_path: Path, *, guard_fails: bool) -> None:
    package = tmp_path / "cdt_steps_rb"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "demo.py").write_text(
        "\n".join(
            [
                "import typer",
                "from cdt.sdk import step",
                "",
                "@step('demo.guard')",
                "def guard(ctx):",
                "    if (ctx.cwd / 'fail-marker').exists():",
                "        raise typer.BadParameter('boom')",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "0.5.1"\n', encoding="utf-8")
    init_dir = tmp_path / "cdt"
    init_dir.mkdir(exist_ok=True)
    (init_dir / "__init__.py").write_text('__version__ = "0.5.1"\n', encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## Unreleased\n\n- Real change.\n\n## v0.5.1 - 2026-09-01\n\n- Old.\n",
        encoding="utf-8",
    )
    (tmp_path / "cdt.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "plugins:",
                "  - cdt_steps_rb.demo",
                "pipelines:",
                "  release:",
                "    inputs:",
                "      version:",
                "        required: true",
                "    steps:",
                '      - python.prepare_release: {version: "${inputs.version}"}',
                "      - demo.guard",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    if guard_fails:
        (tmp_path / "fail-marker").write_text("", encoding="utf-8")


def test_run_configured_pipeline_rolls_back_release_files_before_commit(tmp_path, monkeypatch):
    _write_rollback_project(tmp_path, guard_fails=True)
    monkeypatch.syspath_prepend(str(tmp_path))
    status_file = tmp_path / "status.json"

    with pytest.raises(typer.BadParameter, match="boom"):
        run_configured_pipeline(
            tmp_path,
            {},
            "release",
            inputs={"version": "0.5.2"},
            status_file=status_file,
            record_run=False,
        )

    assert 'version = "0.5.1"' in (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    assert '__version__ = "0.5.1"' in (tmp_path / "cdt" / "__init__.py").read_text(encoding="utf-8")
    changelog = (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## Unreleased\n\n- Real change." in changelog
    assert "## v0.5.2" not in changelog
    payload = json.loads(status_file.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["rolled_back"] is True


def test_run_configured_pipeline_keeps_release_files_after_success(tmp_path, monkeypatch):
    _write_rollback_project(tmp_path, guard_fails=False)
    monkeypatch.syspath_prepend(str(tmp_path))
    status_file = tmp_path / "status.json"

    run_configured_pipeline(
        tmp_path,
        {},
        "release",
        inputs={"version": "0.5.2"},
        status_file=status_file,
        record_run=False,
    )

    assert 'version = "0.5.2"' in (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    changelog = (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## v0.5.2 - " in changelog
    payload = json.loads(status_file.read_text(encoding="utf-8"))
    assert payload["status"] == "success"
    assert payload["rolled_back"] is False
    assert payload["new_version"] == "0.5.2"
