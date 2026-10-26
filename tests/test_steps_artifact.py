import shutil

import pytest
import typer

from cdt.artifacts import ArtifactKind, BuildArtifact
from cdt.pipeline import PipelineContext
from cdt.runner import CommandRunner
from cdt.steps.artifact import ANDROID_ARTIFACT_PREFIX_ENV, CopyArtifactToDownloadsStep


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


@pytest.mark.parametrize("kind", [ArtifactKind.APK, ArtifactKind.AAB])
@pytest.mark.parametrize(
    ("env_value", "expected_name"),
    [
        ("myapp", "myapp_app-release.bin"),
        (None, "app-release.bin"),
        ("", "app-release.bin"),
        ("  myapp  ", "myapp_app-release.bin"),
        ("   ", "app-release.bin"),
    ],
)
def test_copy_artifact_android_prefix_variants(tmp_path, monkeypatch, kind, env_value, expected_name):
    ctx = make_ctx(tmp_path)
    if env_value is not None:
        ctx.env[ANDROID_ARTIFACT_PREFIX_ENV] = env_value
    src = tmp_path / "app-release.bin"
    src.write_text("artifact", encoding="utf-8")
    ctx.register_artifact("android", BuildArtifact(kind, src, "Android"))
    fake_home = tmp_path / "home"
    calls = []
    monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)
    monkeypatch.setattr(shutil, "copy2", lambda source, dest: calls.append((source, dest)))

    CopyArtifactToDownloadsStep("android").run(ctx)

    assert calls == [(src, fake_home / "Downloads" / expected_name)]
    assert src.read_text(encoding="utf-8") == "artifact"
    assert ctx.artifacts["android"].path == src


@pytest.mark.parametrize("kind", [ArtifactKind.APK, ArtifactKind.AAB])
def test_copy_artifact_android_prefix_custom_destination(tmp_path, kind):
    ctx = make_ctx(tmp_path)
    ctx.env[ANDROID_ARTIFACT_PREFIX_ENV] = "myapp"
    src = tmp_path / "app-release.bin"
    src.write_text("artifact", encoding="utf-8")
    ctx.register_artifact("android", BuildArtifact(kind, src, "Android"))

    CopyArtifactToDownloadsStep("android", destination="out").run(ctx)

    dst = tmp_path / "out" / "myapp_app-release.bin"
    assert dst.is_file()
    assert dst.read_text(encoding="utf-8") == "artifact"
    assert not (tmp_path / "out" / "app-release.bin").exists()
    assert src.read_text(encoding="utf-8") == "artifact"
    assert ctx.artifacts["android"].path == src


@pytest.mark.parametrize("kind", [ArtifactKind.APK, ArtifactKind.AAB])
@pytest.mark.parametrize("bad_value", ["my/app", "my\\app", "bad\x01name"])
def test_copy_artifact_rejects_unsafe_android_prefix(tmp_path, monkeypatch, kind, bad_value):
    ctx = make_ctx(tmp_path)
    ctx.env[ANDROID_ARTIFACT_PREFIX_ENV] = bad_value
    src = tmp_path / "app-release.bin"
    src.write_text("artifact", encoding="utf-8")
    ctx.register_artifact("android", BuildArtifact(kind, src, "Android"))
    calls = []
    monkeypatch.setattr(shutil, "copy2", lambda source, dest: calls.append((source, dest)))

    with pytest.raises(typer.BadParameter, match="ANDROID_ARTIFACT_PREFIX"):
        CopyArtifactToDownloadsStep("android", destination="out").run(ctx)

    assert calls == []
    assert not (tmp_path / "out").exists()
    assert src.read_text(encoding="utf-8") == "artifact"
    assert ctx.artifacts["android"].path == src


def test_copy_artifact_ipa_ignores_android_prefix(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.env[ANDROID_ARTIFACT_PREFIX_ENV] = "bad/prefix"
    src = tmp_path / "app.ipa"
    src.write_text("artifact", encoding="utf-8")
    ctx.register_artifact("ios", BuildArtifact(ArtifactKind.IPA, src, "iOS"))

    CopyArtifactToDownloadsStep("ios", destination="out").run(ctx)

    dst = tmp_path / "out" / "app.ipa"
    assert dst.is_file()
    assert dst.read_text(encoding="utf-8") == "artifact"
    assert ctx.artifacts["ios"].path == src
