from pathlib import Path

import pytest
import typer

from cdt.artifacts import ArtifactKind, BuildArtifact
from cdt.pipeline import PipelineContext
from cdt.pipeline.config import resolve_value
from cdt.runner import CommandRunner


def _ctx(tmp_path: Path, env: dict[str, str] | None = None) -> PipelineContext:
    return PipelineContext(cwd=tmp_path, env=env or {}, runner=CommandRunner())


def test_values_mapping_and_worker_scope_cleanup(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    ctx = PipelineContext(cwd=tmp_path, env={}, runner=CommandRunner(), values={"root": "yes"})

    def worker():
        with pytest.raises(RuntimeError), ctx.values.scope("0/0", {"local": "yes"}):
            ctx.values.setdefault("new", "value")
            assert ctx.values.copy() == {"local": "yes", "new": "value"}
            ctx.values.clear()
            raise RuntimeError("failure")
        assert dict(ctx.values) == {"root": "yes"}
        assert ctx.values.branch_id is None

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(worker).result()
        pool.submit(worker).result()
    assert ctx.values.pop("root") == "yes"
    assert ctx.values == {}


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


def test_resolve_value_interpolates_inputs(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.inputs = {"version": "0.5.2"}

    assert resolve_value("${inputs.version}", ctx) == "0.5.2"
    assert resolve_value("v${inputs.version}", ctx) == "v0.5.2"


def test_resolve_value_missing_input_errors_clearly(tmp_path):
    ctx = _ctx(tmp_path)

    with pytest.raises(typer.BadParameter, match="Missing pipeline input: version"):
        resolve_value("${inputs.version}", ctx)
