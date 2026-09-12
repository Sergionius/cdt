from pathlib import Path

import pytest

from cdt.artifacts import ArtifactKind, BuildArtifact
from cdt.pipeline import PipelineContext
from cdt.runner import CommandExecutionError
from cdt.steps.firebase import FirebaseDeployStep, FirebaseUploadAppDistributionStep


class RecordingRunner:
    def __init__(self, exit_code=0):
        self.exit_code = exit_code
        self.calls = []

    def run(self, command, *, cwd):
        self.calls.append((command, cwd))
        return self.exit_code


def _context(tmp_path: Path, runner: RecordingRunner) -> PipelineContext:
    artifact_path = tmp_path / "app.aab"
    artifact_path.write_text("aab", encoding="utf-8")
    return PipelineContext(
        cwd=tmp_path,
        env={"FIREBASE_APP_ID_ANDROID": "app", "FIREBASE_TOKEN": "token"},
        runner=runner,
        ids=["TASK-1"],
        artifacts={"android": BuildArtifact(ArtifactKind.AAB, artifact_path, "Android")},
    )


def test_firebase_deploy_uses_context_runner(tmp_path):
    runner = RecordingRunner()
    ctx = _context(tmp_path, runner)

    FirebaseDeployStep().run(ctx)

    assert runner.calls == [(["firebase", "deploy"], tmp_path)]


def test_firebase_upload_uses_named_artifact_and_task_notes(tmp_path, monkeypatch, capsys):
    runner = RecordingRunner()
    ctx = _context(tmp_path, runner)
    played: list[Path] = []
    monkeypatch.setattr("cdt.steps.firebase._play_fail_sound", lambda env, cwd: played.append(cwd))

    FirebaseUploadAppDistributionStep(artifact="android", release_notes_from_ids=True).run(ctx)

    assert len(runner.calls) == 1
    command, cwd = runner.calls[0]
    assert cwd == tmp_path
    assert str(tmp_path / "app.aab") in command
    assert "TASK-1" in " ".join(command)
    assert played == []
    assert "✅ Firebase App Distribution upload completed" in capsys.readouterr().out


@pytest.mark.parametrize("exit_code", [1, 7])
def test_firebase_upload_failure_raises_command_error(tmp_path, monkeypatch, exit_code):
    runner = RecordingRunner(exit_code=exit_code)
    ctx = _context(tmp_path, runner)
    played: list[Path] = []
    monkeypatch.setattr("cdt.steps.firebase._play_fail_sound", lambda env, cwd: played.append(cwd))

    with pytest.raises(CommandExecutionError) as exc_info:
        FirebaseUploadAppDistributionStep(artifact="android", release_notes_from_ids=True).run(ctx)

    cause = (
        "Firebase App Distribution upload failed. Check the Firebase CLI output above for details; "
        "inspect the saved run with cdt logs <run-id>."
    )
    error = exc_info.value
    assert error.cause == cause
    assert str(error) == cause
    assert error.exit_code == exit_code
    assert len(runner.calls) == 1
    command, cwd = runner.calls[0]
    assert error.command == command
    assert cwd == tmp_path
    assert "appdistribution:distribute" in command
    assert str(tmp_path / "app.aab") in command
    assert "TASK-1" in " ".join(command)
    assert played == [tmp_path]
