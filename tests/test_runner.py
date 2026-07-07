import subprocess
from pathlib import Path

import pytest
import typer

from cdt import config, runner


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

    def fake_popen(command, cwd):
        calls.append((command, cwd))
        return FakePopen(command, cwd=cwd, returncode=7)

    monkeypatch.setattr(config, "UI_MODE", "verbose")
    monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)

    assert runner._run(["echo", "hello world"], cwd=tmp_path) == 7
    assert calls == [(["echo", "hello world"], tmp_path)]
    assert "$ echo 'hello world'" in capsys.readouterr().out


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
