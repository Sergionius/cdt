from pathlib import Path

import pytest
import typer

from cdt.artifacts import ArtifactKind, BuildArtifact
from cdt.pipeline import PipelineContext
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


def test_firebase_upload_uses_named_artifact_and_task_notes(tmp_path):
    runner = RecordingRunner()
    ctx = _context(tmp_path, runner)

    FirebaseUploadAppDistributionStep(artifact="android", release_notes_from_ids=True).run(ctx)

    command = runner.calls[0][0]
    assert str(tmp_path / "app.aab") in command
    assert "TASK-1" in " ".join(command)


def test_firebase_upload_failure_exits(tmp_path, monkeypatch):
    runner = RecordingRunner(exit_code=1)
    ctx = _context(tmp_path, runner)
    monkeypatch.setattr("cdt.steps.firebase._play_fail_sound", lambda env, cwd: None)

    with pytest.raises(typer.Exit):
        FirebaseUploadAppDistributionStep(artifact="android").run(ctx)
