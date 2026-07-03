from pathlib import Path

import pytest
import typer

from cdt.artifacts import ArtifactKind, BuildArtifact
from cdt.pipeline import PipelineContext
from cdt.runner import CommandRunner


def _ctx(tmp_path: Path, env: dict[str, str] | None = None) -> PipelineContext:
    return PipelineContext(cwd=tmp_path, env=env or {}, runner=CommandRunner())


def test_context_stores_versions_and_artifacts(tmp_path):
    ctx = _ctx(tmp_path)
    artifact = BuildArtifact(ArtifactKind.IPA, tmp_path / "app.ipa", "App IPA")

    ctx.old_version = "1.2.3+4"
    ctx.new_version = "1.2.3+5"
    ctx.register_artifact("ipa", artifact)

    assert ctx.old_version == "1.2.3+4"
    assert ctx.new_version == "1.2.3+5"
    assert ctx.artifact("ipa") == artifact


def test_context_reads_required_env_with_fallback(tmp_path):
    ctx = _ctx(tmp_path, {"NATIVE_TEST_SCHEME": "Runner"})

    assert ctx.require_env("IOS_TEST_SCHEME", "NATIVE_TEST_SCHEME") == "Runner"


def test_context_required_env_uses_primary_key_in_error(tmp_path):
    ctx = _ctx(tmp_path)

    with pytest.raises(typer.BadParameter, match="Missing IOS_TEST_SCHEME"):
        ctx.require_env("IOS_TEST_SCHEME", "NATIVE_TEST_SCHEME")


def test_context_resolves_project_relative_path(tmp_path):
    ctx = _ctx(tmp_path)

    assert ctx.project_path("ios/Runner") == tmp_path / "ios" / "Runner"


def test_context_errors_when_artifact_is_missing(tmp_path):
    ctx = _ctx(tmp_path)

    with pytest.raises(typer.BadParameter, match="Missing pipeline artifact: ipa"):
        ctx.artifact("ipa")
