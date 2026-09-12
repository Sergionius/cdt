import os
from pathlib import Path

import pytest
import typer

from cdt.artifacts import ArtifactKind, BuildArtifact
from cdt.pipeline import PipelineContext
from cdt.platforms.android import (
    _android_aab_artifact,
    _android_apk_artifact,
    _build_android_aab_command,
    _build_android_apk_command,
    _copy_to_downloads,
    _find_android_aab,
    _find_android_apk,
    copy_artifacts_to_downloads,
)
from cdt.runner import CommandExecutionError
from cdt.steps.android import AndroidBuildAabStep, AndroidBuildApkStep


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


class RecordingRunner:
    def __init__(self, exit_code=0):
        self.exit_code = exit_code
        self.calls = []

    def run(self, command, *, cwd):
        self.calls.append((command, cwd))
        return self.exit_code


def _context(tmp_path: Path, runner: RecordingRunner) -> PipelineContext:
    return PipelineContext(cwd=tmp_path, env={}, runner=runner)


def _write_release_aab(tmp_path: Path) -> Path:
    aab = tmp_path / "build" / "app" / "outputs" / "bundle" / "release" / "app-release.aab"
    aab.parent.mkdir(parents=True)
    aab.write_text("aab", encoding="utf-8")
    return aab


def _write_release_apk(tmp_path: Path) -> Path:
    apk = tmp_path / "build" / "app" / "outputs" / "flutter-apk" / "app-release.apk"
    apk.parent.mkdir(parents=True)
    apk.write_text("apk", encoding="utf-8")
    return apk


ANDROID_BUILD_CASES = [
    pytest.param(
        AndroidBuildAabStep,
        _build_android_aab_command(),
        "Android AAB build failed. Check the Flutter/Gradle output above for details.",
        id="aab",
    ),
    pytest.param(
        AndroidBuildApkStep,
        _build_android_apk_command(),
        "Android APK build failed. Check the Flutter/Gradle output above for details.",
        id="apk",
    ),
]


@pytest.mark.parametrize(("step_class", "expected_command", "cause"), ANDROID_BUILD_CASES)
def test_android_build_failure_raises_command_error(tmp_path, monkeypatch, step_class, expected_command, cause):
    runner = RecordingRunner(exit_code=7)
    ctx = _context(tmp_path, runner)
    played: list[Path] = []
    monkeypatch.setattr("cdt.steps.android._play_fail_sound", lambda env, cwd: played.append(cwd))

    with pytest.raises(CommandExecutionError) as exc_info:
        step_class().run(ctx)

    error = exc_info.value
    assert error.cause == cause
    assert str(error) == cause
    assert error.exit_code == 7
    assert runner.calls == [(expected_command, tmp_path)]
    assert error.command == expected_command
    assert ctx.artifacts == {}
    assert played == [tmp_path]


ANDROID_BUILD_SUCCESS_CASES = [
    pytest.param(
        AndroidBuildAabStep,
        _build_android_aab_command(),
        "aab",
        ArtifactKind.AAB,
        "Android AAB",
        _write_release_aab,
        id="aab",
    ),
    pytest.param(
        AndroidBuildApkStep,
        _build_android_apk_command(),
        "apk",
        ArtifactKind.APK,
        "Android APK",
        _write_release_apk,
        id="apk",
    ),
]


@pytest.mark.parametrize(
    ("step_class", "expected_command", "artifact_name", "artifact_kind", "artifact_label", "write_output"),
    ANDROID_BUILD_SUCCESS_CASES,
)
def test_android_build_success_registers_named_artifact(
    tmp_path, monkeypatch, step_class, expected_command, artifact_name, artifact_kind, artifact_label, write_output
):
    artifact_path = write_output(tmp_path)
    runner = RecordingRunner()
    ctx = _context(tmp_path, runner)
    played: list[Path] = []
    monkeypatch.setattr("cdt.steps.android._play_fail_sound", lambda env, cwd: played.append(cwd))

    step_class().run(ctx)

    assert runner.calls == [(expected_command, tmp_path)]
    assert played == []
    artifact = ctx.artifact(artifact_name)
    assert artifact.kind == artifact_kind
    assert artifact.path == artifact_path
    assert artifact.label == artifact_label
