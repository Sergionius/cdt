import subprocess

import pytest
import typer

from cdt.pipeline import PipelineContext
from cdt.runner import CommandRunner
from cdt.steps import hook


def make_ctx(tmp_path, env=None):
    return PipelineContext(cwd=tmp_path, env=env or {}, runner=CommandRunner())


def write_script(tmp_path, name="hook.py"):
    script = tmp_path / name
    script.write_text("print('ok')\n", encoding="utf-8")
    return script


def test_python_script_hook_rejects_non_string_args(tmp_path):
    script = write_script(tmp_path)
    step = hook.PythonScriptHookStep(str(script), args=["ok", 1])

    with pytest.raises(typer.BadParameter, match="args must be a list of strings"):
        step.run(make_ctx(tmp_path))


def test_python_script_hook_rejects_script_outside_project_root(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("print('outside')\n", encoding="utf-8")
    step = hook.PythonScriptHookStep(str(outside))

    with pytest.raises(typer.BadParameter, match="script must be inside project root"):
        step.run(make_ctx(root))


def test_python_script_hook_rejects_missing_script(tmp_path):
    step = hook.PythonScriptHookStep("missing.py")

    with pytest.raises(typer.BadParameter, match="script not found"):
        step.run(make_ctx(tmp_path))


def test_python_script_hook_runs_with_project_and_step_env(tmp_path, monkeypatch):
    script = write_script(tmp_path)
    calls = []

    def fake_run(command, cwd, env, timeout):
        calls.append((command, cwd, env, timeout))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setenv("FROM_SHELL", "shell")
    monkeypatch.setenv("FROM_DOTENV", "shell-wins")
    monkeypatch.setattr(hook.subprocess, "run", fake_run)

    step = hook.PythonScriptHookStep("hook.py", name="custom hook", args=["a"], env={"FROM_STEP": 7}, timeout=9)
    step.run(make_ctx(tmp_path, {"FROM_DOTENV": "dotenv", "ONLY_DOTENV": "yes"}))

    command, cwd, env, timeout = calls[0]
    assert command == ["python3", str(script.resolve()), "a"]
    assert cwd == tmp_path
    assert timeout == 9
    assert env["FROM_SHELL"] == "shell"
    assert env["FROM_DOTENV"] == "shell-wins"
    assert env["ONLY_DOTENV"] == "yes"
    assert env["FROM_STEP"] == "7"


def test_python_script_hook_failed_exit_raises_when_fail_on_error(tmp_path, monkeypatch):
    write_script(tmp_path)
    monkeypatch.setattr(
        hook.subprocess,
        "run",
        lambda command, cwd, env, timeout: subprocess.CompletedProcess(command, 2),
    )

    with pytest.raises(typer.BadParameter, match="failed with exit code 2"):
        hook.PythonScriptHookStep("hook.py", name="bad hook").run(make_ctx(tmp_path))


def test_python_script_hook_failed_exit_ignored_when_fail_on_error_false(tmp_path, monkeypatch):
    write_script(tmp_path)
    monkeypatch.setattr(
        hook.subprocess,
        "run",
        lambda command, cwd, env, timeout: subprocess.CompletedProcess(command, 2),
    )

    hook.PythonScriptHookStep("hook.py", fail_on_error=False).run(make_ctx(tmp_path))


def test_python_script_hook_timeout_raises_when_fail_on_error(tmp_path, monkeypatch):
    write_script(tmp_path)

    def fake_run(command, cwd, env, timeout):
        raise subprocess.TimeoutExpired(command, timeout)

    monkeypatch.setattr(hook.subprocess, "run", fake_run)

    with pytest.raises(typer.BadParameter, match="timed out after 3s"):
        hook.PythonScriptHookStep("hook.py", timeout=3).run(make_ctx(tmp_path))


def test_python_script_hook_timeout_ignored_when_fail_on_error_false(tmp_path, monkeypatch):
    write_script(tmp_path)

    def fake_run(command, cwd, env, timeout):
        raise subprocess.TimeoutExpired(command, timeout)

    monkeypatch.setattr(hook.subprocess, "run", fake_run)

    hook.PythonScriptHookStep("hook.py", timeout=3, fail_on_error=False).run(make_ctx(tmp_path))


def test_python_script_hook_strict_outputs_allows_declared_changes(tmp_path, monkeypatch):
    write_script(tmp_path)
    changes = [set(), {"allowed.txt"}]
    monkeypatch.setattr(hook, "_tracked_changes", lambda cwd: changes.pop(0))
    monkeypatch.setattr(
        hook.subprocess,
        "run",
        lambda command, cwd, env, timeout: subprocess.CompletedProcess(command, 0),
    )

    hook.PythonScriptHookStep("hook.py", strict_outputs=True, outputs=["allowed.txt"]).run(make_ctx(tmp_path))


def test_python_script_hook_strict_outputs_rejects_undeclared_changes(tmp_path, monkeypatch):
    write_script(tmp_path)
    changes = [set(), {"allowed.txt", "other.txt"}]
    monkeypatch.setattr(hook, "_tracked_changes", lambda cwd: changes.pop(0))
    monkeypatch.setattr(
        hook.subprocess,
        "run",
        lambda command, cwd, env, timeout: subprocess.CompletedProcess(command, 0),
    )

    with pytest.raises(typer.BadParameter, match="changed tracked files outside outputs: other.txt"):
        hook.PythonScriptHookStep("hook.py", strict_outputs=True, outputs=["allowed.txt"]).run(make_ctx(tmp_path))


def test_tracked_changes_requires_git_repository(tmp_path, monkeypatch):
    monkeypatch.setattr(
        hook.subprocess,
        "run",
        lambda command, cwd, capture_output, text: subprocess.CompletedProcess(command, 1, stdout=""),
    )

    with pytest.raises(typer.BadParameter, match="requires a git repository"):
        hook._tracked_changes(tmp_path)


def test_tracked_changes_returns_normalized_non_empty_lines(tmp_path, monkeypatch):
    monkeypatch.setattr(
        hook.subprocess,
        "run",
        lambda command, cwd, capture_output, text: subprocess.CompletedProcess(
            command, 0, stdout=" file.txt \n\nsub/other.txt\n"
        ),
    )

    assert hook._tracked_changes(tmp_path) == {"file.txt", "sub/other.txt"}


def test_normalize_output_uses_forward_slashes():
    assert hook._normalize_output(r"dir\file.txt") == "dir/file.txt"
