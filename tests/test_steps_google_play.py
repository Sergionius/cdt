"""Contract tests for the google_play.upload_aab pipeline step.

All scenarios run on an in-memory fake of the Android Publisher API and
temporary files: no credentials, no network and no real Google Play.
"""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

import cdt.steps.google_play as google_play_step
from cdt.artifacts import ArtifactKind, BuildArtifact
from cdt.cli import app
from cdt.pipeline import PipelineContext
from cdt.pipeline.builtins import register_builtin_steps
from cdt.pipeline.config import load_pipeline_config
from cdt.pipeline.preflight import preflight_payload
from cdt.pipeline.registry import _clear_steps_for_tests
from cdt.schema import schema_payload
from cdt.services.google_play import (
    STAGE_EDIT_GET,
    STAGE_TRACK_GET,
    GooglePlayError,
    compute_file_sha256,
)
from cdt.services.google_play_state import PublishIntent, PublishOutcome
from cdt.steps.google_play import GooglePlayUploadAabStep

runner = CliRunner()

PACKAGE = "com.example.app"


def setup_function():
    _clear_steps_for_tests()
    register_builtin_steps()


def teardown_function():
    _clear_steps_for_tests()


def _context(tmp_path: Path, *, env: dict[str, str] | None = None) -> PipelineContext:
    aab = tmp_path / "app-release.aab"
    aab.write_text("android-app-bundle-bytes", encoding="utf-8")
    return PipelineContext(
        cwd=tmp_path,
        env=env if env is not None else {},
        runner=None,
        artifacts={"aab": BuildArtifact(ArtifactKind.AAB, aab, "Android AAB")},
    )


def _step(**overrides: Any) -> GooglePlayUploadAabStep:
    options: dict[str, Any] = {
        "artifact": "aab",
        "package_name": PACKAGE,
        "track": "internal",
        "release_status": "draft",
    }
    options.update(overrides)
    return GooglePlayUploadAabStep(**options)


# -- step contract -------------------------------------------------------------


def test_required_options_have_no_defaults():
    signature = inspect.signature(GooglePlayUploadAabStep.__init__)
    for option in ("artifact", "package_name", "track", "release_status"):
        assert signature.parameters[option].default is inspect.Parameter.empty, option
    assert signature.parameters["release_notes"].default is None
    assert signature.parameters["release_name"].default is None
    assert signature.parameters["user_fraction"].default is None


def test_schema_marks_primary_options_required():
    payload = schema_payload()
    step_schemas = [obj for obj in payload["$defs"]["step"]["oneOf"] if isinstance(obj, dict) and obj.get("properties")]
    options_by_name = {next(iter(obj["properties"])): next(iter(obj["properties"].values())) for obj in step_schemas}

    upload_options = options_by_name["google_play.upload_aab"]
    assert upload_options["required"] == ["artifact", "package_name", "track", "release_status"]
    assert sorted(upload_options["properties"]) == [
        "artifact",
        "package_name",
        "release_name",
        "release_notes",
        "release_status",
        "track",
        "user_fraction",
    ]
    assert upload_options["properties"]["user_fraction"]["type"] == ["number", "string", "null"]
    assert upload_options["properties"]["release_notes"]["type"] == ["object", "null"]
    assert upload_options["properties"]["release_name"]["type"] == ["string", "null"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"package_name": "not-a-package"},
        {"package_name": "1abc.example"},
        {"package_name": "com..app"},
        {"package_name": ""},
        {"package_name": "  "},
        {"track": "with space"},
        {"track": ""},
        {"track": "продакшн"},
        {"release_status": "Draft"},
        {"release_status": "production"},
        {"release_status": ""},
    ],
)
def test_option_validation_rejects_invalid_values(overrides):
    with pytest.raises(typer.BadParameter):
        _step(**overrides)


def test_release_status_must_be_explicitly_allowed():
    _step(release_status="draft")
    _step(release_status="completed")
    _step(release_status="inProgress", user_fraction=0.5)
    with pytest.raises(typer.BadParameter, match="no default is applied"):
        _step(release_status="beta")


def test_staged_rollout_requires_fraction_strictly_between_zero_and_one():
    _step(release_status="inProgress", user_fraction=0.25)
    _step(release_status="inProgress", user_fraction="0.5")
    for fraction in (0, 1, 1.5, -0.1, "0", "1", "1.5"):
        with pytest.raises(typer.BadParameter, match="0 < user_fraction < 1"):
            _step(release_status="inProgress", user_fraction=fraction)
    with pytest.raises(typer.BadParameter, match="required for release_status 'inProgress'"):
        _step(release_status="inProgress")


def test_fraction_is_rejected_for_draft_and_completed():
    for status in ("draft", "completed"):
        with pytest.raises(typer.BadParameter, match="only allowed for release_status 'inProgress'"):
            _step(release_status=status, user_fraction=0.5)


@pytest.mark.parametrize("value", [True, False, "nan", "inf", "-inf", "Infinity", "NaN", "abc", "", "0.5abc", [0.5]])
def test_user_fraction_rejects_bool_nan_infinity_and_non_numeric_strings(value):
    with pytest.raises(typer.BadParameter):
        _step(release_status="inProgress", user_fraction=value)


def test_release_notes_must_be_non_empty_localized_strings():
    _step(release_notes={"en-US": "Bug fixes", "ru-RU": "  Исправления  "})
    with pytest.raises(typer.BadParameter, match="must map language codes"):
        _step(release_notes="notes")
    with pytest.raises(typer.BadParameter, match="must not be empty"):
        _step(release_notes={})
    with pytest.raises(typer.BadParameter, match="non-empty localized text"):
        _step(release_notes={"en-US": ""})
    with pytest.raises(typer.BadParameter, match="non-empty localized text"):
        _step(release_notes={"en-US": "   "})
    with pytest.raises(typer.BadParameter, match="empty language code"):
        _step(release_notes={"": "text"})


# -- artifact checks -------------------------------------------------------------


def test_missing_artifact_fails_with_clear_error(tmp_path):
    ctx = PipelineContext(cwd=tmp_path, env={}, runner=None)
    with pytest.raises(typer.BadParameter, match="Missing pipeline artifact: aab"):
        _step().run(ctx)


def test_non_aab_artifact_is_rejected(tmp_path):
    apk = tmp_path / "app.apk"
    apk.write_text("apk", encoding="utf-8")
    ctx = PipelineContext(
        cwd=tmp_path,
        env={},
        runner=None,
        artifacts={"apk": BuildArtifact(ArtifactKind.APK, apk, "Android APK")},
    )
    with pytest.raises(typer.BadParameter, match="requires an AAB artifact"):
        _step(artifact="apk").run(ctx)


def test_missing_aab_file_is_rejected(tmp_path):
    aab = tmp_path / "missing.aab"
    ctx = PipelineContext(
        cwd=tmp_path,
        env={},
        runner=None,
        artifacts={"aab": BuildArtifact(ArtifactKind.AAB, aab, "Android AAB")},
    )
    with pytest.raises(typer.BadParameter, match="artifact file not found"):
        _step().run(ctx)


# -- run wiring and safe final messages -------------------------------------------


def _patch_operation(monkeypatch, outcome: PublishOutcome, captured: dict[str, Any]) -> None:
    class FakeOperation:
        def __init__(self, client: Any, cwd: Path, intent: PublishIntent, aab_path: Path):
            captured["intent"] = intent
            captured["aab_path"] = aab_path

        def run(self) -> PublishOutcome:
            return outcome

    def fake_client(env: dict[str, str], cwd: Path):
        captured["env"] = env
        captured["cwd"] = cwd
        return ("fake-client",)

    monkeypatch.setattr(google_play_step, "GooglePlayClient", fake_client)
    monkeypatch.setattr(google_play_step, "GooglePlayPublishOperation", FakeOperation)


def test_draft_run_reports_commit_and_explicit_draft_notice(tmp_path, monkeypatch, capsys):
    captured: dict[str, Any] = {}
    _patch_operation(
        monkeypatch,
        PublishOutcome(
            operation_id="op",
            package_name=PACKAGE,
            track="internal",
            release_status="draft",
            version_code=40,
            aab_sha256="abc",
            changes_sent_for_review=False,
        ),
        captured,
    )
    ctx = _context(tmp_path, env={"GOOGLE_APPLICATION_CREDENTIALS": "adc.json"})

    _step(release_notes={"en-US": "First drop"}).run(ctx)

    out = capsys.readouterr().out
    assert f"package {PACKAGE}" in out
    assert "track 'internal'" in out
    assert "version code: 40" in out
    assert "requested status: draft" in out
    assert "commit: confirmed" in out
    assert "Draft release created" in out
    assert "NOT sent for review" in out
    assert "not available to users" in out
    assert "Managed publishing" not in out
    # The client receives the final pipeline env and project cwd; the intent is
    # built only from explicit options (no env or pipeline-name inference).
    assert captured["env"] == {"GOOGLE_APPLICATION_CREDENTIALS": "adc.json"}
    assert captured["cwd"] == tmp_path
    assert captured["intent"] == PublishIntent(
        package_name=PACKAGE,
        track="internal",
        release_status="draft",
        release_notes={"en-US": "First drop"},
    )
    assert captured["aab_path"] == tmp_path / "app-release.aab"


def test_non_draft_run_reports_accepted_changes_and_managed_publishing_alternatives(tmp_path, monkeypatch, capsys):
    captured: dict[str, Any] = {}
    _patch_operation(
        monkeypatch,
        PublishOutcome(
            operation_id="op",
            package_name=PACKAGE,
            track="production",
            release_status="completed",
            version_code=41,
            aab_sha256="abc",
            changes_sent_for_review=True,
        ),
        captured,
    )
    ctx = _context(tmp_path)

    _step(track="production", release_status="completed").run(ctx)

    out = capsys.readouterr().out
    assert "accepted the changes" in out
    assert "commit: confirmed" in out
    assert "Review approval and user availability were NOT verified" in out
    assert "manual" in out and "Publish" in out
    assert "CDT cannot detect which mode is enabled" in out
    # The message must not claim which managed-publishing mode is active.
    assert re.search(r"if it applies", out, flags=re.IGNORECASE)


def test_resumed_outcome_reports_no_repeated_mutation(tmp_path, monkeypatch, capsys):
    captured: dict[str, Any] = {}
    _patch_operation(
        monkeypatch,
        PublishOutcome(
            operation_id="op",
            package_name=PACKAGE,
            track="internal",
            release_status="completed",
            version_code=40,
            aab_sha256="abc",
            changes_sent_for_review=True,
            resumed=True,
        ),
        captured,
    )
    ctx = _context(tmp_path)

    _step(release_status="completed").run(ctx)

    out = capsys.readouterr().out
    assert "(resumed)" in out
    assert "nothing was uploaded or committed again" in out


# -- offline end-to-end against the fake Android Publisher API ---------------------


class FakePlayClient:
    """Duck-typed GooglePlayClient with app-level commit semantics."""

    def __init__(self):
        self.app_bundles: list[dict[str, Any]] = []
        self.app_tracks: dict[str, list[dict[str, Any]]] = {}
        self.edits: dict[str, dict[str, Any]] = {}
        self.commits: list[str] = []
        self._counter = 0

    def create_edit(self, package_name: str) -> dict[str, Any]:
        self._counter += 1
        edit_id = f"edit-{self._counter}"
        self.edits[edit_id] = {
            "bundles": list(self.app_bundles),
            "tracks": {track: list(releases) for track, releases in self.app_tracks.items()},
        }
        return {"id": edit_id, "expiryTimeSeconds": "3600"}

    def get_edit(self, package_name: str, edit_id: str) -> dict[str, Any]:
        if edit_id not in self.edits:
            raise GooglePlayError(STAGE_EDIT_GET, "edit not found", http_status=404)
        return {"id": edit_id}

    def delete_edit(self, package_name: str, edit_id: str) -> None:
        self.edits.pop(edit_id, None)

    def list_bundles(self, package_name: str, edit_id: str) -> list[dict[str, Any]]:
        return list(self.edits[edit_id]["bundles"])

    def upload_bundle(self, package_name: str, edit_id: str, aab_path: Path) -> dict[str, Any]:
        bundle = {"versionCode": 40, "sha256": compute_file_sha256(Path(aab_path))}
        self.edits[edit_id]["bundles"].append(bundle)
        return dict(bundle)

    def get_track(self, package_name: str, edit_id: str, track: str) -> dict[str, Any]:
        releases = self.edits[edit_id]["tracks"].get(track)
        if releases is None:
            raise GooglePlayError(STAGE_TRACK_GET, "track not found", http_status=404)
        return {"track": track, "releases": list(releases)}

    def update_track(
        self, package_name: str, edit_id: str, track: str, releases: list[dict[str, Any]]
    ) -> dict[str, Any]:
        self.edits[edit_id]["tracks"][track] = list(releases)
        return {"track": track, "releases": list(releases)}

    def commit_edit(self, package_name: str, edit_id: str) -> dict[str, Any]:
        data = self.edits.pop(edit_id)
        self.app_bundles = list(data["bundles"])
        self.app_tracks = {track: list(releases) for track, releases in data["tracks"].items()}
        self.commits.append(edit_id)
        return {"id": edit_id}


def test_step_publishes_completed_release_and_resumes_identical_operation(tmp_path, monkeypatch, capsys):
    client = FakePlayClient()
    client.app_tracks["production"] = [{"status": "completed", "versionCodes": [39]}]
    monkeypatch.setattr(google_play_step, "GooglePlayClient", lambda env, cwd: client)
    ctx = _context(tmp_path)
    options = dict(track="production", release_status="completed", release_name="Release 1.2")

    _step(**options).run(ctx)

    out = capsys.readouterr().out
    assert "commit: confirmed" in out
    assert client.app_tracks["production"] == [
        {"status": "completed", "versionCodes": [40], "name": "Release 1.2"}
    ]
    checkpoints = list((tmp_path / ".cdt" / "google-play" / "operations").glob("*.json"))
    assert len(checkpoints) == 1
    payload = json.loads(checkpoints[0].read_text(encoding="utf-8"))
    assert payload["phase"] == "confirmed"
    assert payload["version_code"] == 40
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in json.dumps(payload)

    # An identical re-run returns the saved result without any new mutation.
    commits_before = len(client.commits)
    _step(**options).run(ctx)
    assert len(client.commits) == commits_before
    assert "(resumed)" in capsys.readouterr().out


def test_step_staged_rollout_keeps_completed_base_release(tmp_path, monkeypatch, capsys):
    client = FakePlayClient()
    client.app_tracks["production"] = [{"status": "completed", "versionCodes": [39]}]
    monkeypatch.setattr(google_play_step, "GooglePlayClient", lambda env, cwd: client)
    ctx = _context(tmp_path)

    _step(track="production", release_status="inProgress", user_fraction="0.25").run(ctx)

    capsys.readouterr()
    statuses = [release["status"] for release in client.app_tracks["production"]]
    assert statuses == ["completed", "inProgress"]
    staged = client.app_tracks["production"][1]
    assert staged["versionCodes"] == [40]
    assert staged["userFraction"] == 0.25


# -- planning, preflight and schema stay offline -----------------------------------


def _write_play_pipeline(path: Path, *, risk: str) -> None:
    (path / "cdt.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "pipelines:",
                "  play:",
                f"    risk: {risk}",
                "    steps:",
                "      - google_play.upload_aab:",
                "          artifact: aab",
                f"          package_name: {PACKAGE}",
                "          track: internal",
                "          release_status: draft",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def test_plan_inspect_and_schema_do_not_touch_adc_or_checkpoint(tmp_path, monkeypatch):
    _write_play_pipeline(tmp_path, risk="production")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)

    dry_run = runner.invoke(app, ["run", "play", "--dry-run"])
    inspect = runner.invoke(app, ["pipeline", "inspect", "play", "--json"])
    steps = runner.invoke(app, ["pipeline", "steps", "--json"])
    schema = runner.invoke(app, ["schema"])

    assert dry_run.exit_code == 0, dry_run.output
    assert inspect.exit_code == 0, inspect.output
    assert steps.exit_code == 0, steps.output
    assert schema.exit_code == 0, schema.output
    assert "google_play.upload_aab" in json.loads(inspect.output)["registered_steps"]
    assert not (tmp_path / ".cdt").exists()


def test_preflight_checks_explicit_adc_file_and_ignores_missing_variable(tmp_path):
    _write_play_pipeline(tmp_path, risk="production")
    register_builtin_steps()

    config = load_pipeline_config(tmp_path)

    # Unset GOOGLE_APPLICATION_CREDENTIALS is not an error.
    payload = preflight_payload(config, "play", {}, cwd=tmp_path)
    assert payload["status"] == "ok"
    assert all("GOOGLE_APPLICATION_CREDENTIALS" not in check["name"] for check in payload["env"])

    # An explicitly configured ADC file is checked read-only: existing passes,
    # missing fails, and credentials never prove Play Console permissions.
    (tmp_path / "adc.json").write_text("{}", encoding="utf-8")
    payload = preflight_payload(config, "play", {"GOOGLE_APPLICATION_CREDENTIALS": "adc.json"}, cwd=tmp_path)
    assert payload["status"] == "ok"
    assert payload["env"] == [
        {"name": "adc.json (GOOGLE_APPLICATION_CREDENTIALS ADC file)", "present": True}
    ]

    payload = preflight_payload(config, "play", {"GOOGLE_APPLICATION_CREDENTIALS": "missing.json"}, cwd=tmp_path)
    assert payload["status"] == "error"
    assert payload["missing_env"] == ["missing.json (GOOGLE_APPLICATION_CREDENTIALS ADC file)"]


def test_pipeline_without_play_step_is_not_affected_by_adc_check(tmp_path):
    (tmp_path / "cdt.yaml").write_text(
        "version: 1\npipelines:\n  test:\n    risk: standard\n    steps:\n      - flutter.pub_get\n",
        encoding="utf-8",
    )
    register_builtin_steps()

    config = load_pipeline_config(tmp_path)

    payload = preflight_payload(config, "test", {"GOOGLE_APPLICATION_CREDENTIALS": "missing.json"}, cwd=tmp_path)

    assert payload["status"] == "ok"
    assert payload["env"] == []
