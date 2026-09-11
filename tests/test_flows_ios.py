from pathlib import Path

import pytest
import typer

from cdt.artifacts import ArtifactKind, BuildArtifact
from cdt.flows import ios_flow
from cdt.pipeline import PipelineContext
from cdt.runner import CommandRunner
from cdt.steps import appstore as appstore_steps
from cdt.steps import ios as ios_steps
from cdt.steps import notify as notify_steps
from cdt.steps import tracker as tracker_steps
from cdt.steps.appstore import CompleteTestFlightStep, UploadTestFlightIpaStep, UploadTestFlightStep


def _pipeline_context(tmp_path, env=None, new_version=None):
    ctx = PipelineContext(cwd=tmp_path, env=env or {}, runner=CommandRunner(), new_version=new_version)
    return ctx


def _register_ipa(ctx, ipa: Path) -> None:
    ctx.register_artifact("ipa", BuildArtifact(kind=ArtifactKind.IPA, path=ipa, label="ipa"))


def test_ios_test_flow_runs_steps_and_keeps_repeated_ids(tmp_path, monkeypatch):
    events: list[tuple[str, object]] = []
    ipa = tmp_path / "App.ipa"
    env = {"IOS_TEST_SCHEME": "Runner"}

    def increment(cwd: Path, env_arg: dict[str, str], scheme: str) -> tuple[str, str]:
        events.append(("increment", scheme))
        return "1.2.3+4", "1.2.3+5"

    def build(cwd: Path, env_arg: dict[str, str], scheme: str) -> Path:
        events.append(("build", scheme))
        return ipa

    def upload(ipa_path: Path, env_arg: dict[str, str], changelog: str, new_version: str) -> int:
        events.append(("upload", (ipa_path, changelog, new_version)))
        return 0

    monkeypatch.setattr(ios_steps, "_increment_ios_build_number", increment)
    monkeypatch.setattr(ios_steps, "_ios_xcode_build_ipa", build)
    monkeypatch.setattr(appstore_steps, "_upload_testflight", upload)
    monkeypatch.setattr(
        notify_steps,
        "_notify_success",
        lambda env_arg, new_version, ids=None: events.append(("notify", (new_version, ids))),
    )
    monkeypatch.setattr(
        notify_steps,
        "_play_success_sound",
        lambda env_arg, cwd: events.append(("success_sound", cwd)),
    )
    monkeypatch.setattr(
        tracker_steps,
        "_tracker_comment",
        lambda env_arg, issue_id, new_version: events.append(("tracker", (issue_id, new_version))),
    )

    ios_flow.run_ios_test_flow(tmp_path, env, ["APP-1", "APP-2"])

    assert events == [
        ("increment", "Runner"),
        ("build", "Runner"),
        ("upload", (ipa, "dev build APP-1, APP-2", "1.2.3+5")),
        ("notify", ("1.2.3+5", ["APP-1", "APP-2"])),
        ("success_sound", tmp_path),
        ("tracker", ("APP-1", "1.2.3+5")),
        ("tracker", ("APP-2", "1.2.3+5")),
    ]


def test_ios_prod_flow_supports_legacy_scheme_and_prod_user_agent(tmp_path, monkeypatch):
    events: list[tuple[str, object]] = []
    ipa = tmp_path / "App.ipa"
    env = {"NATIVE_PROD_SCHEME": "LegacyRunner", "NOTIFY_PROVIDER": "pachca"}

    monkeypatch.setattr(
        ios_steps,
        "_increment_ios_build_number",
        lambda cwd, env_arg, scheme: events.append(("increment", scheme)) or ("2.0.0+9", "2.0.0+10"),
    )
    monkeypatch.setattr(
        ios_steps,
        "_ios_xcode_build_ipa",
        lambda cwd, env_arg, scheme: events.append(("build", scheme)) or ipa,
    )
    monkeypatch.setattr(
        appstore_steps,
        "_upload_testflight",
        lambda ipa_path, env_arg, changelog, new_version: events.append(
            ("upload", (ipa_path, changelog, new_version))
        )
        or 0,
    )
    monkeypatch.setattr(
        notify_steps,
        "_notify_success",
        lambda env_arg, new_version, ids=None: events.append(("notify", (new_version, ids))),
    )
    monkeypatch.setattr(
        notify_steps,
        "_play_success_sound",
        lambda env_arg, cwd: events.append(("success_sound", cwd)),
    )
    monkeypatch.setattr(
        notify_steps,
        "_notify_prod_user_agent_pachca",
        lambda env_arg, new_version: events.append(("prod_user_agent", new_version)),
    )

    ios_flow.run_ios_prod_flow(tmp_path, env)

    assert events == [
        ("increment", "LegacyRunner"),
        ("build", "LegacyRunner"),
        ("upload", (ipa, "prod build", "2.0.0+10")),
        ("notify", ("2.0.0+10", None)),
        ("success_sound", tmp_path),
        ("prod_user_agent", "2.0.0+10"),
    ]


def test_ios_flow_requires_scheme(tmp_path):
    with pytest.raises(typer.BadParameter, match="Missing IOS_TEST_SCHEME"):
        ios_flow.run_ios_test_flow(tmp_path, {}, [])


def test_full_upload_step_keeps_compatible_cycle(tmp_path, monkeypatch):
    ipa = tmp_path / "App.ipa"
    calls = []
    monkeypatch.setattr(
        appstore_steps,
        "_upload_testflight",
        lambda path, env, changelog, new_version: calls.append((changelog, new_version)) or 0,
    )
    ctx = _pipeline_context(tmp_path, new_version="1.2.3+5")
    _register_ipa(ctx, ipa)

    UploadTestFlightStep("notes").run(ctx)

    assert calls == [("notes", "1.2.3+5")]


def test_upload_only_step_runs_transporter_without_completion(tmp_path, monkeypatch):
    ipa = tmp_path / "App.ipa"
    ipa.write_bytes(b"ipa")
    calls = []
    monkeypatch.setattr(
        appstore_steps, "_upload_testflight_ipa", lambda path, env: calls.append(("upload", path)) or 0
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("upload-only step must not run post-upload processing")

    monkeypatch.setattr(appstore_steps, "_complete_testflight_after_upload", forbidden)
    ctx = _pipeline_context(tmp_path)
    _register_ipa(ctx, ipa)

    UploadTestFlightIpaStep().run(ctx)

    assert calls == [("upload", ipa)]


def test_upload_only_step_fails_on_transporter_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(appstore_steps, "_upload_testflight_ipa", lambda path, env: 7)
    played = []
    monkeypatch.setattr(appstore_steps, "_play_fail_sound", lambda env, cwd: played.append(cwd))
    ctx = _pipeline_context(tmp_path)
    _register_ipa(ctx, tmp_path / "App.ipa")

    with pytest.raises(typer.Exit) as excinfo:
        UploadTestFlightIpaStep().run(ctx)

    assert excinfo.value.exit_code == 1
    assert played == [tmp_path]


def test_complete_only_step_updates_changelog_without_upload(tmp_path, monkeypatch):
    calls = []

    def fake_complete(env, changelog, new_version):
        calls.append((changelog, new_version))
        return 0

    def forbidden(*args, **kwargs):
        raise AssertionError("completion step must not upload the IPA")

    monkeypatch.setattr(appstore_steps, "_complete_testflight_after_upload", fake_complete)
    monkeypatch.setattr(appstore_steps, "_upload_testflight", forbidden)
    monkeypatch.setattr(appstore_steps, "_upload_testflight_ipa", forbidden)
    ctx = _pipeline_context(
        tmp_path, env={"IOS_BUNDLE_ID": "com.example.app"}, new_version="1.2.3+5"
    )

    step = CompleteTestFlightStep("notes")
    step.run(ctx)
    step.run(ctx)  # repeat completion for an already existing build stays upload-free

    assert calls == [("notes", "1.2.3+5"), ("notes", "1.2.3+5")]


def test_complete_only_step_requires_new_version(tmp_path):
    ctx = _pipeline_context(tmp_path)

    with pytest.raises(typer.BadParameter, match="Missing pipeline value: new_version"):
        CompleteTestFlightStep().run(ctx)
