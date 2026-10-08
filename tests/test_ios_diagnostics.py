import os
import sys
from pathlib import Path

import pytest

from cdt.pipeline import PipelineContext
from cdt.runner import CommandExecutionError, CommandRunner
from cdt.steps.ios import IosFlutterBuildIpaStep


def test_failed_build_keeps_redacted_verbose_diagnostics(tmp_path: Path):
    command = [
        sys.executable,
        "-c",
        "import sys; print('Archiving...'); print('error: provisioning profile expired'); "
        "print('token=private-value'); print('xcodebuild encountered an error (74)'); sys.exit(1)",
    ]
    code, details, log = CommandRunner().run_with_diagnostics(
        command, cwd=tmp_path, run_dir=tmp_path, env={"API_TOKEN": "private-value"}
    )

    assert code == 1
    assert details == "error: provisioning profile expired"
    assert log == tmp_path / "ios-build.log"
    assert "token=private-value" not in log.read_text()
    assert "private-value" not in log.read_text()
    assert "token=***" in log.read_text()
    assert os.stat(log).st_mode & 0o777 == 0o600


def test_success_does_not_keep_verbose_diagnostics(tmp_path: Path):
    code, details, log = CommandRunner().run_with_diagnostics(
        [sys.executable, "-c", "print('success')"], cwd=tmp_path, run_dir=tmp_path, env={}
    )
    assert (code, details, log) == (0, "", None)
    assert not (tmp_path / "ios-build.log").exists()


def test_generic_xcode_74_reports_no_specific_error(tmp_path: Path):
    code, details, log = CommandRunner().run_with_diagnostics(
        [sys.executable, "-c", "import sys; print('xcodebuild encountered an error (74)'); sys.exit(1)"],
        cwd=tmp_path, run_dir=tmp_path, env={},
    )
    assert code == 1
    assert details == ""
    assert log is not None and "error (74)" in log.read_text()


def test_ios_step_runs_verbose_once_and_links_diagnostics(tmp_path: Path, monkeypatch):
    class DiagnosticRunner:
        def __init__(self):
            self.commands = []

        def run_with_diagnostics(self, command, *, cwd, run_dir, env):
            self.commands.append(command[:])
            return 1, "error: export failed", run_dir / "ios-build.log"

    runner = DiagnosticRunner()
    ctx = PipelineContext(cwd=tmp_path, env={}, runner=runner, run_dir=tmp_path)
    monkeypatch.setattr("cdt.steps.ios._play_fail_sound", lambda env, cwd: None)
    with pytest.raises(CommandExecutionError) as failure:
        IosFlutterBuildIpaStep().run(ctx)
    assert runner.commands == [[
        "flutter", "build", "ipa", "--obfuscate", "--split-debug-info=obfsymbols", "--no-pub", "-v"
    ]]
    assert "Xcode reported: error: export failed" in str(failure.value)
    assert f"Diagnostic log: {tmp_path / 'ios-build.log'}" in str(failure.value)
