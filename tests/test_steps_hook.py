import os
import signal
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
        return 0

    monkeypatch.setenv("FROM_SHELL", "shell")
    monkeypatch.setenv("FROM_DOTENV", "shell-wins")
    monkeypatch.setattr(hook, "run_managed_subprocess", fake_run)

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


def test_python_script_hook_default_timeout_is_thirty_seconds(tmp_path, monkeypatch):
    write_script(tmp_path)
    calls = []
    monkeypatch.setattr(hook, "run_managed_subprocess", lambda command, cwd, env, timeout: calls.append(timeout) or 0)

    hook.PythonScriptHookStep("hook.py").run(make_ctx(tmp_path))

    assert calls == [30]


def test_python_script_hook_allows_null_timeout_for_legacy_form(tmp_path, monkeypatch):
    write_script(tmp_path)
    calls = []
    monkeypatch.setattr(hook, "run_managed_subprocess", lambda command, cwd, env, timeout: calls.append(timeout) or 0)

    hook.PythonScriptHookStep("hook.py", timeout=None).run(make_ctx(tmp_path))

    assert calls == [None]


def test_python_script_hook_failed_exit_raises_when_fail_on_error(tmp_path, monkeypatch):
    write_script(tmp_path)
    monkeypatch.setattr(hook, "run_managed_subprocess", lambda command, cwd, env, timeout: 2)

    with pytest.raises(typer.BadParameter, match="failed with exit code 2"):
        hook.PythonScriptHookStep("hook.py", name="bad hook").run(make_ctx(tmp_path))


def test_python_script_hook_failed_exit_ignored_when_fail_on_error_false(tmp_path, monkeypatch):
    write_script(tmp_path)
    monkeypatch.setattr(hook, "run_managed_subprocess", lambda command, cwd, env, timeout: 2)

    hook.PythonScriptHookStep("hook.py", fail_on_error=False).run(make_ctx(tmp_path))


def test_python_script_hook_timeout_raises_when_fail_on_error(tmp_path, monkeypatch):
    write_script(tmp_path)

    def fake_run(command, cwd, env, timeout):
        raise subprocess.TimeoutExpired(command, timeout)

    monkeypatch.setattr(hook, "run_managed_subprocess", fake_run)

    with pytest.raises(typer.BadParameter, match="timed out after 3s"):
        hook.PythonScriptHookStep("hook.py", timeout=3).run(make_ctx(tmp_path))


def test_python_script_hook_timeout_ignored_when_fail_on_error_false(tmp_path, monkeypatch):
    write_script(tmp_path)

    def fake_run(command, cwd, env, timeout):
        raise subprocess.TimeoutExpired(command, timeout)

    monkeypatch.setattr(hook, "run_managed_subprocess", fake_run)

    hook.PythonScriptHookStep("hook.py", timeout=3, fail_on_error=False).run(make_ctx(tmp_path))


def _wait_for_marker(path, deadline_seconds=15):
    import time

    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return False


HOOK_CHILD_CODE = """
import os
import signal
import time
from pathlib import Path

dir_path = Path(os.environ["HOOK_TEST_DIR"])


def terminate(signum, frame):
    (dir_path / "child.terminated").write_text("1")
    os._exit(0)


signal.signal(signal.SIGTERM, terminate)
time.sleep(120)
"""

HOOK_SCRIPT_CODE = """
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

dir_path = Path(os.environ["HOOK_TEST_DIR"])
child = subprocess.Popen([sys.executable, str(dir_path / "child.py")])
(dir_path / "child.pid").write_text(str(child.pid))


def terminate(signum, frame):
    (dir_path / "hook.terminated").write_text("1")
    os._exit(0)


signal.signal(signal.SIGTERM, terminate)
time.sleep(120)
"""


def _wait_for_marker(path, deadline_seconds=15):
    import time

    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return False


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups are required")
def test_python_script_hook_timeout_terminates_child_process_group(tmp_path):
    (tmp_path / "child.py").write_text(HOOK_CHILD_CODE, encoding="utf-8")
    hook_script = tmp_path / "hook.py"
    hook_script.write_text(HOOK_SCRIPT_CODE, encoding="utf-8")
    child_pid_path = tmp_path / "child.pid"
    try:
        step = hook.PythonScriptHookStep(
            str(hook_script), name="hung hook", timeout=1, env={"HOOK_TEST_DIR": str(tmp_path)}
        )
        with pytest.raises(typer.BadParameter, match="timed out after 1s"):
            step.run(make_ctx(tmp_path))

        # Both the hook and its child received the group TERM within the grace period.
        assert _wait_for_marker(tmp_path / "hook.terminated")
        assert _wait_for_marker(tmp_path / "child.terminated")
        if child_pid_path.exists():
            child_pid = int(child_pid_path.read_text().strip())
            with pytest.raises(ProcessLookupError):
                os.kill(child_pid, 0)
    finally:
        # Guaranteed cleanup: never leak the hung tree into the test session.
        if child_pid_path.exists():
            try:
                os.kill(int(child_pid_path.read_text().strip()), signal.SIGKILL)
            except (ProcessLookupError, ValueError):
                pass


def test_python_script_hook_strict_outputs_allows_declared_changes(tmp_path, monkeypatch):
    write_script(tmp_path)
    changes = [set(), {"allowed.txt"}]
    monkeypatch.setattr(hook, "_tracked_changes", lambda cwd: changes.pop(0))
    monkeypatch.setattr(hook, "run_managed_subprocess", lambda command, cwd, env, timeout: 0)

    hook.PythonScriptHookStep("hook.py", strict_outputs=True, outputs=["allowed.txt"]).run(make_ctx(tmp_path))


def test_python_script_hook_strict_outputs_rejects_undeclared_changes(tmp_path, monkeypatch):
    write_script(tmp_path)
    changes = [set(), {"allowed.txt", "other.txt"}]
    monkeypatch.setattr(hook, "_tracked_changes", lambda cwd: changes.pop(0))
    monkeypatch.setattr(hook, "run_managed_subprocess", lambda command, cwd, env, timeout: 0)

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
