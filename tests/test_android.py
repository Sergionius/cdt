import os

import pytest
import typer

from cdt.artifacts import ArtifactKind, BuildArtifact
from cdt.platforms.android import (
    _android_aab_artifact,
    _android_apk_artifact,
    _copy_to_downloads,
    _find_android_aab,
    _find_android_apk,
    copy_artifacts_to_downloads,
)


def test_find_android_aab_returns_newest_file(tmp_path):
    output_dir = tmp_path / "build" / "app" / "outputs" / "bundle" / "release"
    output_dir.mkdir(parents=True)
    old = output_dir / "old.aab"
    new = output_dir / "new.aab"
    old.write_text("old", encoding="utf-8")
    new.write_text("new", encoding="utf-8")
    os.utime(old, (100, 100))
    os.utime(new, (200, 200))

    assert _find_android_aab(tmp_path) == new


def test_find_android_aab_errors_when_missing(tmp_path):
    output_dir = tmp_path / "build" / "app" / "outputs" / "bundle" / "release"
    output_dir.mkdir(parents=True)

    with pytest.raises(typer.BadParameter, match="No .aab files found"):
        _find_android_aab(tmp_path)


def test_find_android_apk_returns_release_apk(tmp_path):
    apk = tmp_path / "build" / "app" / "outputs" / "flutter-apk" / "app-release.apk"
    apk.parent.mkdir(parents=True)
    apk.write_text("apk", encoding="utf-8")

    assert _find_android_apk(tmp_path) == apk


def test_copy_to_downloads_uses_injected_destination(tmp_path):
    src = tmp_path / "app-release.apk"
    src.write_text("apk", encoding="utf-8")
    downloads = tmp_path / "downloads"

    _copy_to_downloads([src], downloads_dir=downloads)

    assert (downloads / "app-release.apk").read_text(encoding="utf-8") == "apk"


def test_android_aab_artifact_wraps_found_aab(tmp_path):
    aab = tmp_path / "build" / "app" / "outputs" / "bundle" / "release" / "app-release.aab"
    aab.parent.mkdir(parents=True)
    aab.write_text("aab", encoding="utf-8")

    artifact = _android_aab_artifact(tmp_path)

    assert artifact.kind == ArtifactKind.AAB
    assert artifact.path == aab
    assert artifact.label == "Android AAB"


def test_android_apk_artifact_wraps_found_apk(tmp_path):
    apk = tmp_path / "build" / "app" / "outputs" / "flutter-apk" / "app-release.apk"
    apk.parent.mkdir(parents=True)
    apk.write_text("apk", encoding="utf-8")

    artifact = _android_apk_artifact(tmp_path)

    assert artifact.kind == ArtifactKind.APK
    assert artifact.path == apk
    assert artifact.label == "Android APK"


def test_copy_artifacts_to_downloads_uses_artifact_paths(tmp_path):
    src = tmp_path / "app-release.aab"
    src.write_text("aab", encoding="utf-8")
    downloads = tmp_path / "downloads"

    copy_artifacts_to_downloads(
        [BuildArtifact(kind=ArtifactKind.AAB, path=src, label="Android AAB")],
        downloads_dir=downloads,
    )

    assert (downloads / "app-release.aab").read_text(encoding="utf-8") == "aab"
