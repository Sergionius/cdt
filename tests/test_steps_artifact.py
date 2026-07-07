import shutil

import pytest
import typer

from cdt.artifacts import ArtifactKind, BuildArtifact
from cdt.pipeline import PipelineContext
from cdt.runner import CommandRunner
from cdt.steps.artifact import CopyArtifactToDownloadsStep


def make_ctx(tmp_path):
    return PipelineContext(cwd=tmp_path, env={}, runner=CommandRunner())


def test_copy_artifact_to_downloads_rejects_directory_artifact(tmp_path):
    ctx = make_ctx(tmp_path)
    artifact_dir = tmp_path / "build"
    artifact_dir.mkdir()
    ctx.register_artifact("android", BuildArtifact(ArtifactKind.AAB, artifact_dir, "Android"))

    with pytest.raises(typer.BadParameter, match="Artifact path is a directory"):
        CopyArtifactToDownloadsStep("android").run(ctx)


def test_copy_artifact_to_downloads_rejects_missing_file(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.register_artifact("android", BuildArtifact(ArtifactKind.AAB, tmp_path / "missing.aab", "Android"))

    with pytest.raises(typer.BadParameter, match="Artifact file not found"):
        CopyArtifactToDownloadsStep("android").run(ctx)


def test_copy_artifact_to_relative_destination(tmp_path, monkeypatch):
    ctx = make_ctx(tmp_path)
    src = tmp_path / "app.aab"
    src.write_text("artifact", encoding="utf-8")
    ctx.register_artifact("android", BuildArtifact(ArtifactKind.AAB, src, "Android"))
    calls = []
    monkeypatch.setattr(shutil, "copy2", lambda source, dest: calls.append((source, dest)))

    CopyArtifactToDownloadsStep("android", destination="out").run(ctx)

    assert (tmp_path / "out").is_dir()
    assert calls == [(src, tmp_path / "out" / "app.aab")]


def test_copy_artifact_to_default_downloads(tmp_path, monkeypatch):
    ctx = make_ctx(tmp_path)
    src = tmp_path / "app.ipa"
    src.write_text("artifact", encoding="utf-8")
    ctx.register_artifact("ios", BuildArtifact(ArtifactKind.IPA, src, "iOS"))
    fake_home = tmp_path / "home"
    calls = []
    monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)
    monkeypatch.setattr(shutil, "copy2", lambda source, dest: calls.append((source, dest)))

    CopyArtifactToDownloadsStep("ios").run(ctx)

    assert calls == [(src, fake_home / "Downloads" / "app.ipa")]
