"""Contract tests for the google_play.upload_aab pipeline step.

All scenarios run on an in-memory fake of the Android Publisher API and
temporary files: no credentials, no network and no real Google Play.
"""

from __future__ import annotations

import inspect
import json
import re
import sys
import threading
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
    STAGE_EDIT_COMMIT,
    STAGE_EDIT_GET,
    STAGE_TRACK_GET,
    GooglePlayError,
    compute_file_sha256,
)
from cdt.services.google_play_state import (
    PublishIntent,
    PublishOutcome,
    TrackConflictError,
    UnknownResultError,
)
from cdt.steps.google_play import GooglePlayUploadAabStep

runner = CliRunner()

PACKAGE = "com.example.app"


def setup_function():
    _clear_steps_for_tests()
    register_builtin_steps()
    for module_name in ("cdt_steps.demo", "cdt_steps.play", "cdt_steps"):
        sys.modules.pop(module_name, None)


def teardown_function():
    _clear_steps_for_tests()
    for module_name in ("cdt_steps.demo", "cdt_steps.play", "cdt_steps"):
        sys.modules.pop(module_name, None)


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


def _patch_operation(
    monkeypatch,
    outcome: PublishOutcome | None,
    captured: dict[str, Any],
    *,
    error: Exception | None = None,
) -> None:
    class FakeOperation:
        def __init__(self, client: Any, cwd: Path, intent: PublishIntent, aab_path: Path):
            captured["intent"] = intent
            captured["aab_path"] = aab_path

        def run(self) -> PublishOutcome:
            if error is not None:
                raise error
            assert outcome is not None
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


# -- review rejections, Console-required errors and message boundaries ---------------


def test_review_rejection_fails_without_success_claims_or_flag_suggestions(tmp_path, monkeypatch, capsys):
    error = GooglePlayError(STAGE_EDIT_COMMIT, "app is currently under review", http_status=409)
    captured: dict[str, Any] = {}
    _patch_operation(monkeypatch, None, captured, error=error)

    with pytest.raises(typer.BadParameter) as excinfo:
        _step(track="production", release_status="completed").run(_context(tmp_path))

    out = capsys.readouterr().out
    assert "app is currently under review" in str(excinfo.value)
    # The definite Google rejection is surfaced as-is: no success claims, no
    # promise of a review submission and no suggestion to retry with flags that
    # would cancel the running review.
    assert "✅" not in out
    assert "accepted the changes" not in out
    assert "commit: confirmed" not in out
    assert "sent for review" not in out
    all_text = out + str(excinfo.value)
    assert "changesNotSentForReview" not in all_text


@pytest.mark.parametrize(
    ("error", "expected_fragment"),
    [
        pytest.param(
            TrackConflictError(
                "target track 'internal' has a release with status 'draft'; CDT stops instead of replacing "
                "it - resolve the track in Google Play Console"
            ),
            "Google Play Console",
            id="track-conflict",
        ),
        pytest.param(
            UnknownResultError(
                "Google Play publication result is unknown: the track update response was lost. Verify the "
                "release in Google Play Console before doing anything else"
            ),
            "result is unknown",
            id="unknown-result",
        ),
        pytest.param(
            GooglePlayError(
                STAGE_EDIT_COMMIT, "The current user has insufficient permissions", http_status=403
            ),
            "insufficient permissions",
            id="permission-denied",
        ),
    ],
)
def test_console_required_errors_stop_without_success_or_review_claims(
    tmp_path, monkeypatch, capsys, error, expected_fragment
):
    captured: dict[str, Any] = {}
    _patch_operation(monkeypatch, None, captured, error=error)

    with pytest.raises(typer.BadParameter) as excinfo:
        _step().run(_context(tmp_path))

    out = capsys.readouterr().out
    assert expected_fragment in str(excinfo.value)
    # Console-bound problems are never turned into success and never promise
    # that changes were submitted for review.
    assert "✅" not in out
    assert "accepted the changes" not in out
    assert "commit: confirmed" not in out
    assert "sent for review" not in out
    assert "Draft release created" not in out


def test_final_messages_keep_commit_review_managed_publishing_and_release_separate(
    tmp_path, monkeypatch, capsys
):
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

    _step(release_status="draft").run(_context(tmp_path))

    draft_out = capsys.readouterr().out
    assert "commit: confirmed" in draft_out
    assert "Draft release created" in draft_out
    assert "NOT sent for review" in draft_out
    assert "nothing is published" in draft_out
    assert "not available to users" in draft_out
    # A draft never mixes in review approval or managed-publishing wording.
    assert "Managed publishing" not in draft_out
    assert "accepted the changes" not in draft_out
    assert "Review approval" not in draft_out

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

    _step(track="production", release_status="completed").run(_context(tmp_path))

    completed_out = capsys.readouterr().out
    assert "commit: confirmed" in completed_out
    assert "accepted the changes" in completed_out
    assert "Review approval and user availability were NOT verified" in completed_out
    assert "manual" in completed_out
    assert "Publish in Google Play Console" in completed_out
    assert "if it applies" in completed_out
    assert "CDT cannot detect which mode is enabled" in completed_out
    # A confirmed commit is never reported as approval or user availability.
    assert "Draft release created" not in completed_out
    assert "was approved" not in completed_out
    assert "is now available" not in completed_out
    assert "successfully published" not in completed_out


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


def test_step_draft_publication_keeps_previous_completed_release(tmp_path, monkeypatch, capsys):
    client = FakePlayClient()
    client.app_tracks["production"] = [{"status": "completed", "versionCodes": [39], "name": "1.0.0"}]
    monkeypatch.setattr(google_play_step, "GooglePlayClient", lambda env, cwd: client)
    ctx = _context(tmp_path)

    _step(
        track="production",
        release_status="draft",
        release_notes={"en-US": "Next drop"},
    ).run(ctx)

    out = capsys.readouterr().out
    # The retained completed release stays next to the new draft; only the draft
    # message is shown and it never claims a review submission or publication.
    assert client.app_tracks["production"] == [
        {"status": "completed", "versionCodes": [39], "name": "1.0.0"},
        {
            "status": "draft",
            "versionCodes": [40],
            "releaseNotes": [{"language": "en-US", "text": "Next drop"}],
        },
    ]
    assert "Draft release created" in out
    assert "NOT sent for review" in out


# -- parallel branches keep different apps strictly isolated -------------------------


class MultiAppPlayClient:
    """Thread-safe duck-typed GooglePlayClient with per-package app state."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._edit_counter = 0
        self.apps: dict[str, dict[str, Any]] = {}

    def _app(self, package_name: str) -> dict[str, Any]:
        return self.apps.setdefault(
            package_name,
            {"committed_bundles": {}, "tracks": {}, "edits": {}, "next_version_code": 40},
        )

    def create_edit(self, package_name: str) -> dict[str, Any]:
        with self._lock:
            self._edit_counter += 1
            edit_id = f"edit-{self._edit_counter}"
            app = self._app(package_name)
            app["edits"][edit_id] = {
                "bundles": dict(app["committed_bundles"]),
                "tracks": {track: list(releases) for track, releases in app["tracks"].items()},
            }
            return {"id": edit_id, "expiryTimeSeconds": "43200"}

    def get_edit(self, package_name: str, edit_id: str) -> dict[str, Any]:
        with self._lock:
            if edit_id not in self._app(package_name)["edits"]:
                raise GooglePlayError(STAGE_EDIT_GET, "edit not found", http_status=404)
            return {"id": edit_id}

    def delete_edit(self, package_name: str, edit_id: str) -> None:
        with self._lock:
            self._app(package_name)["edits"].pop(edit_id, None)

    def list_bundles(self, package_name: str, edit_id: str) -> list[dict[str, Any]]:
        with self._lock:
            edit = self._app(package_name)["edits"][edit_id]
            return [dict(bundle) for _, bundle in sorted(edit["bundles"].items())]

    def upload_bundle(self, package_name: str, edit_id: str, aab_path: Path) -> dict[str, Any]:
        with self._lock:
            app = self._app(package_name)
            bundle = {
                "versionCode": app["next_version_code"],
                "sha256": compute_file_sha256(Path(aab_path)),
            }
            app["next_version_code"] += 1
            app["edits"][edit_id]["bundles"][bundle["versionCode"]] = bundle
            return dict(bundle)

    def get_track(self, package_name: str, edit_id: str, track: str) -> dict[str, Any]:
        with self._lock:
            releases = self._app(package_name)["edits"][edit_id]["tracks"].get(track)
            if releases is None:
                raise GooglePlayError(STAGE_TRACK_GET, "track not found", http_status=404)
            return {"track": track, "releases": list(releases)}

    def update_track(
        self, package_name: str, edit_id: str, track: str, releases: list[dict[str, Any]]
    ) -> dict[str, Any]:
        with self._lock:
            edit = self._app(package_name)["edits"][edit_id]
            edit["tracks"][track] = [dict(release) for release in releases]
            return {"track": track, "releases": list(releases)}

    def commit_edit(self, package_name: str, edit_id: str) -> dict[str, Any]:
        with self._lock:
            app = self._app(package_name)
            data = app["edits"].pop(edit_id)
            app["committed_bundles"].update(data["bundles"])
            app["tracks"] = {track: list(releases) for track, releases in data["tracks"].items()}
            return {"id": edit_id}


def _write_parallel_play_project(tmp_path: Path) -> None:
    """Two apps published by two parallel branches with fully explicit parameters."""
    package = tmp_path / "cdt_steps"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "play.py").write_text(
        "\n".join(
            [
                "from cdt.artifacts import ArtifactKind, BuildArtifact",
                "from cdt.sdk import step",
                "",
                "@step('demo.aab')",
                "def make_aab(ctx, artifact: str, output: str, content: str):",
                "    aab = ctx.cwd / output",
                "    aab.write_bytes(('aab-' + content).encode('utf-8'))",
                "    ctx.register_artifact(artifact, BuildArtifact(ArtifactKind.AAB, aab, 'Android AAB'))",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "cdt.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "plugins:",
                "  - cdt_steps.play",
                "pipelines:",
                "  apps:",
                "    risk: production",
                "    steps:",
                "      - parallel:",
                "          steps:",
                "            - demo.aab: {artifact: aab-one, output: one.aab, content: first}",
                "            - demo.aab: {artifact: aab-two, output: two.aab, content: second}",
                "      - parallel:",
                "          steps:",
                "            - google_play.upload_aab:",
                "                artifact: aab-one",
                "                package_name: com.example.one",
                "                track: internal",
                "                release_status: completed",
                "                release_notes:",
                "                  en-US: First app notes",
                "            - google_play.upload_aab:",
                "                artifact: aab-two",
                "                package_name: com.example.two",
                "                track: beta",
                "                release_status: draft",
                "                release_notes:",
                "                  en-US: Second app notes",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def test_parallel_branches_keep_different_apps_and_artifacts_isolated(tmp_path, monkeypatch):
    _write_parallel_play_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    for module_name in ("cdt_steps.play", "cdt_steps"):
        sys.modules.pop(module_name, None)
    client = MultiAppPlayClient()
    monkeypatch.setattr(google_play_step, "GooglePlayClient", lambda env, cwd: client)

    result = runner.invoke(app, ["run", "apps", "--confirm", "apps"])

    assert result.exit_code == 0, result.output
    # Each branch delivered its own artifact, package, track, status and notes:
    # no parameter or bundle ever crossed between the apps.
    assert client.apps["com.example.one"]["tracks"]["internal"] == [
        {
            "status": "completed",
            "versionCodes": [40],
            "releaseNotes": [{"language": "en-US", "text": "First app notes"}],
        }
    ]
    assert client.apps["com.example.two"]["tracks"]["beta"] == [
        {
            "status": "draft",
            "versionCodes": [40],
            "releaseNotes": [{"language": "en-US", "text": "Second app notes"}],
        }
    ]
    assert result.output.count("commit: confirmed") == 2
    assert "Draft release created" in result.output
    payloads = {
        payload["package_name"]: payload
        for payload in (
            json.loads(path.read_text(encoding="utf-8"))
            for path in (tmp_path / ".cdt" / "google-play" / "operations").glob("*.json")
        )
    }
    assert set(payloads) == {"com.example.one", "com.example.two"}
    assert payloads["com.example.one"]["track"] == "internal"
    assert payloads["com.example.one"]["release_status"] == "completed"
    assert payloads["com.example.one"]["aab_sha256"] == compute_file_sha256(tmp_path / "one.aab")
    assert payloads["com.example.two"]["track"] == "beta"
    assert payloads["com.example.two"]["release_status"] == "draft"
    assert payloads["com.example.two"]["release_notes"] == {"en-US": "Second app notes"}
    assert payloads["com.example.two"]["aab_sha256"] == compute_file_sha256(tmp_path / "two.aab")


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


def test_pipeline_without_play_step_is_not_affected_by_adc_check(tmp_path, monkeypatch):
    monkeypatch.setattr("cdt.pipeline.preflight.shutil.which", lambda tool: f"/mock/bin/{tool}")
    (tmp_path / "cdt.yaml").write_text(
        "version: 1\npipelines:\n  test:\n    risk: standard\n    steps:\n      - flutter.pub_get\n",
        encoding="utf-8",
    )
    register_builtin_steps()

    config = load_pipeline_config(tmp_path)

    payload = preflight_payload(config, "test", {"GOOGLE_APPLICATION_CREDENTIALS": "missing.json"}, cwd=tmp_path)

    assert payload["status"] == "ok"
    assert payload["env"] == []


# -- standard pipelines are rejected before any step runs ----------------------------


def _write_plugin_project(tmp_path: Path, *, risk: str) -> None:
    """Project with a marker step preceding google_play.upload_aab."""
    package = tmp_path / "cdt_steps"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "demo.py").write_text(
        "\n".join(
            [
                "from cdt.sdk import step",
                "",
                "@step('demo.marker')",
                "def marker(ctx, output: str):",
                "    (ctx.cwd / output).write_text('ran', encoding='utf-8')",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "cdt.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "plugins:",
                "  - cdt_steps.demo",
                "pipelines:",
                "  play:",
                f"    risk: {risk}",
                "    steps:",
                "      - demo.marker: {output: marker.txt}",
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


def test_standard_pipeline_rejects_play_step_before_running_preceding_steps(tmp_path, monkeypatch):
    _write_plugin_project(tmp_path, risk="standard")
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    for module_name in ("cdt_steps.demo", "cdt_steps"):
        sys.modules.pop(module_name, None)
    constructed: list[bool] = []

    def forbidden_client(env: dict[str, str], cwd: Path):
        constructed.append(True)
        raise AssertionError("publishing API client must not be constructed for a standard pipeline")

    monkeypatch.setattr(google_play_step, "GooglePlayClient", forbidden_client)

    validation = runner.invoke(app, ["pipeline", "validate", "play"])
    result = runner.invoke(app, ["run", "play"])

    assert validation.exit_code != 0
    assert "pipelines.play.steps[1]" in validation.output
    assert result.exit_code != 0
    visible = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", result.output)
    assert "requires pipeline risk: production" in " ".join(visible.split())
    # Validation stops the whole pipeline: the preceding step never ran and the
    # publishing API was never touched.
    assert not (tmp_path / "marker.txt").exists()
    assert constructed == []
    assert not (tmp_path / ".cdt" / "google-play").exists()
