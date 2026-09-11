from pathlib import Path

import pytest

from cdt.artifacts import ArtifactKind
from cdt.pipeline import PipelineContext
from cdt.platforms.ios_flutter import (
    _build_ios_ipa_command,
    _build_ios_prod_ipa_command,
    _build_ios_test_ipa_command,
    _ios_ipa_artifact,
)
from cdt.runner import CommandExecutionError
from cdt.steps.ios import IosFlutterBuildIpaStep


class FakeRunner:
    def __init__(self, exit_code: int = 0):
        self.exit_code = exit_code
        self.commands: list[list[str]] = []

    def run(self, command: list[str], *, cwd: Path) -> int:
        self.commands.append(list(command))
        return self.exit_code


def _make_context(tmp_path: Path, runner: FakeRunner) -> PipelineContext:
    return PipelineContext(cwd=tmp_path, env={}, runner=runner)


def _seed_ipa(tmp_path: Path) -> Path:
    ipa = tmp_path / "build" / "ios" / "ipa" / "Runner.ipa"
    ipa.parent.mkdir(parents=True)
    ipa.write_text("ipa", encoding="utf-8")
    return ipa


def test_ios_build_commands():
    assert _build_ios_test_ipa_command() == [
        "flutter",
        "build",
        "ipa",
        "--obfuscate",
        "--split-debug-info=obfsymbols",
        "--no-pub",
    ]
    assert _build_ios_prod_ipa_command() == [
        "flutter",
        "build",
        "ipa",
        "--obfuscate",
        "--split-debug-info=obfsymbols",
        "--dart-define=ENV=prod",
        "--no-pub",
    ]


def test_ios_build_command_accepts_custom_flutter_options():
    assert _build_ios_test_ipa_command(
        dart_defines=["ENV=qa", "API=mock"],
        flavor="qa",
        target="lib/main_qa.dart",
        obfuscate=False,
        split_debug_info=None,
        no_pub=False,
        extra_args=["--export-method=ad-hoc"],
    ) == [
        "flutter",
        "build",
        "ipa",
        "--flavor",
        "qa",
        "--target",
        "lib/main_qa.dart",
        "--dart-define=ENV=qa",
        "--dart-define=API=mock",
        "--export-method=ad-hoc",
    ]


def test_ios_custom_profile_adds_matching_env_dart_define():
    assert _build_ios_ipa_command(profile="qa") == [
        "flutter",
        "build",
        "ipa",
        "--obfuscate",
        "--split-debug-info=obfsymbols",
        "--dart-define=ENV=qa",
        "--no-pub",
    ]


def test_ios_prod_build_command_merges_default_dart_defines():
    assert _build_ios_prod_ipa_command(dart_defines={"API": "prod"}) == [
        "flutter",
        "build",
        "ipa",
        "--obfuscate",
        "--split-debug-info=obfsymbols",
        "--dart-define=ENV=prod",
        "--dart-define=API=prod",
        "--no-pub",
    ]


def test_ios_ipa_artifact_wraps_found_ipa(tmp_path):
    ipa = tmp_path / "build" / "ios" / "ipa" / "Runner.ipa"
    ipa.parent.mkdir(parents=True)
    ipa.write_text("ipa", encoding="utf-8")

    artifact = _ios_ipa_artifact(tmp_path)

    assert artifact.kind == ArtifactKind.IPA
    assert artifact.path == ipa
    assert artifact.label == "iOS IPA"


def test_ios_flutter_step_runs_generated_command_and_registers_ipa(tmp_path, monkeypatch):
    ipa = _seed_ipa(tmp_path)
    runner = FakeRunner(exit_code=0)
    ctx = _make_context(tmp_path, runner)
    played: list[tuple[dict[str, str], Path]] = []
    monkeypatch.setattr("cdt.steps.ios._play_fail_sound", lambda env, cwd: played.append((env, cwd)))

    IosFlutterBuildIpaStep().run(ctx)

    assert runner.commands == [
        [
            "flutter",
            "build",
            "ipa",
            "--obfuscate",
            "--split-debug-info=obfsymbols",
            "--no-pub",
        ]
    ]
    assert played == []
    assert ctx.artifacts["ipa"].kind == ArtifactKind.IPA
    assert ctx.artifacts["ipa"].path == ipa


def test_ios_flutter_step_raises_command_error_on_failure(tmp_path, monkeypatch):
    runner = FakeRunner(exit_code=74)
    ctx = _make_context(tmp_path, runner)
    played: list[Path] = []
    monkeypatch.setattr("cdt.steps.ios._play_fail_sound", lambda env, cwd: played.append(cwd))

    with pytest.raises(CommandExecutionError) as exc_info:
        IosFlutterBuildIpaStep().run(ctx)

    error = exc_info.value
    assert str(error) == "iOS IPA build failed. Check the Flutter/Xcode output above for details."
    assert error.cause == "iOS IPA build failed. Check the Flutter/Xcode output above for details."
    assert error.exit_code == 74
    assert error.command == [
        "flutter",
        "build",
        "ipa",
        "--obfuscate",
        "--split-debug-info=obfsymbols",
        "--no-pub",
    ]
    assert played == [tmp_path]
    assert "ipa" not in ctx.artifacts
