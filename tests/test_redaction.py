import io
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import cdt.steps.google_play as google_play_step
from cdt.cli import app
from cdt.pipeline.context import PipelineContext
from cdt.redaction import SecretRedactor, StreamingRedactor
from cdt.runner import CommandRunner
from cdt.runs import RunOutputRecorder, list_runs, read_json, run_paths
from cdt.services.google_play import (
    STAGE_EDIT_COMMIT,
    STAGE_TRACK_GET,
    GooglePlayError,
    compute_file_sha256,
)

runner = CliRunner()


@pytest.fixture(autouse=True)
def _drop_google_play_plugin_modules():
    """Keep inline cdt_steps plugins from leaking into other test modules."""
    yield
    for module_name in ("cdt_steps.play", "cdt_steps"):
        sys.modules.pop(module_name, None)


def test_redactor_uses_credential_and_explicit_environment_keys():
    env = {
        "API_TOKEN": "long-token-value",
        "CUSTOM_SESSION": "session-value",
        "CDT_REDACT_KEYS": "CUSTOM_SESSION",
        "ORDINARY": "visible-value",
        "SHORT_SECRET": "abc",
    }
    redactor = SecretRedactor.from_env(env)

    result = redactor.redact("long-token-value session-value visible-value abc")

    assert result == "*** *** visible-value abc"


def test_redactor_masks_multiline_private_key_fragments_independently():
    private_key = "-----BEGIN PRIVATE KEY-----\nprivate-key-body\n-----END PRIVATE KEY-----"
    redactor = SecretRedactor.from_env({"ASC_PRIVATE_KEY": private_key})

    result = redactor.redact("key output:\n-----BEGIN PRIVATE KEY-----\nprivate-key-body\n")

    assert "PRIVATE KEY" not in result
    assert "private-key-body" not in result


def test_redactor_handles_overlapping_values_longest_first_and_is_idempotent():
    redactor = SecretRedactor.from_env({"TOKEN": "token-value", "AUTH_TOKEN": "token-value-long"})

    once = redactor.redact("token-value-long token-value")

    assert once == "*** ***"
    assert redactor.redact(once) == once


def test_find_secrets_reports_verbatim_matches_without_redacting():
    redactor = SecretRedactor.from_env({"NOTIFY_TOKEN": "super-secret-value", "ORDINARY": "visible-value"})

    found = redactor.find_secrets("payload mentions super-secret-value and visible-value")

    assert found == ("super-secret-value",)
    assert redactor.find_secrets("nothing here") == ()


def test_find_secrets_skips_short_values_and_matches_multiline_fragments():
    redactor = SecretRedactor.from_env({"API_KEY": "abc", "PRIVATE_KEY": "line-one\nline-two"})

    # Short values (len < 4) are never tracked; multiline keys are tracked
    # both as a whole and per line.
    assert redactor.find_secrets("abc") == ()
    assert redactor.find_secrets("leaked line-one") == ("line-one",)
    assert redactor.find_secrets("line-one\nline-two") == ("line-one\nline-two", "line-one", "line-two")


def test_redactor_masks_common_patterns_without_masking_hashes():
    git_sha = "d747f5c9b2c7af59c6ca6a33112c5c3925cc84c7"
    checksum = "a" * 64
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature123"
    text = (
        "Authorization: Bearer provider-token\n"
        "fallback Bearer another.token-value\n"
        "password: plain-password\n"
        f"jwt={jwt}\nsha={git_sha}\nchecksum={checksum}\n"
    )

    result = SecretRedactor().redact(text)

    assert "provider-token" not in result
    assert "another.token-value" not in result
    assert "plain-password" not in result
    assert jwt not in result
    assert git_sha in result
    assert checksum in result


def test_streaming_redactor_handles_split_secret_and_final_line_without_newline():
    stream = StreamingRedactor(SecretRedactor.from_env({"API_TOKEN": "split-secret-value"}))

    output = stream.feed("before split-sec")
    output += stream.feed("ret-value after\nlast split-sec")
    output += stream.feed("ret-value", final=True)

    assert output == "before *** after\nlast ***"


def test_streaming_redactor_fails_closed_for_oversized_lines():
    stream = StreamingRedactor(SecretRedactor(), max_pending=8)

    output = stream.feed("a" * 9)
    output += stream.feed("discarded\nstill visible\n")

    assert "a" * 9 not in output
    assert "discarded" not in output
    assert "CDT redacted oversized output line" in output
    assert "still visible" in output


def test_pipeline_status_errors_are_redacted(tmp_path):
    status_file = tmp_path / "status.json"
    ctx = PipelineContext(
        cwd=tmp_path,
        env={"API_TOKEN": "status-secret"},
        runner=CommandRunner(),
        pipeline_name="test",
        status_file=status_file,
    )

    ctx.mark_status_failed("0", "provider rejected status-secret")
    payload = json.loads(status_file.read_text(encoding="utf-8"))

    assert payload["error"] == "provider rejected ***"
    assert "status-secret" not in status_file.read_text(encoding="utf-8")


def test_run_output_recorder_keeps_terminal_raw_and_log_redacted(tmp_path, monkeypatch):
    terminal = io.StringIO()
    errors = io.StringIO()
    monkeypatch.setattr(sys, "stdout", terminal)
    monkeypatch.setattr(sys, "stderr", errors)
    log = tmp_path / "output.log"
    recorder = RunOutputRecorder(log, SecretRedactor.from_env({"API_TOKEN": "terminal-secret"}))

    recorder.install()
    try:
        sys.stdout.write("visible terminal-secret\n")
        sys.stderr.write("error terminal-secret\n")
        sys.stdout.flush()
        assert not sys.stdout.isatty()
    finally:
        recorder.close()

    assert sys.stdout is terminal
    assert sys.stderr is errors
    assert terminal.getvalue() == "visible terminal-secret\n"
    assert errors.getvalue() == "error terminal-secret\n"
    saved = log.read_text(encoding="utf-8")
    assert "visible ***\n" in saved
    assert "error ***\n" in saved
    assert "terminal-secret" not in saved


def test_run_output_recorder_flushes_pending_line_on_close(tmp_path):
    log = tmp_path / "output.log"
    recorder = RunOutputRecorder(log, SecretRedactor.from_env({"API_TOKEN": "pending-secret"}))

    recorder.install()
    recorder.record("complete line\n")
    recorder.record("partial line without newline holds pending-secret")
    recorder.close()

    saved = log.read_text(encoding="utf-8")
    assert "complete line\n" in saved
    assert "partial line without newline holds ***" in saved
    assert "pending-secret" not in saved


def test_run_output_recorder_serializes_parallel_branch_writes(tmp_path):
    log = tmp_path / "output.log"
    recorder = RunOutputRecorder(log, SecretRedactor())

    def worker(branch: int) -> None:
        for index in range(25):
            recorder.record(f"branch-{branch}-line-{index}\n")

    threads = [threading.Thread(target=worker, args=(branch,)) for branch in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    recorder.close()

    saved = log.read_text(encoding="utf-8").splitlines()
    expected = [f"branch-{branch}-line-{index}" for branch in range(8) for index in range(25)]
    assert sorted(saved) == sorted(expected)
    assert all(re.fullmatch(r"branch-\d+-line-\d+", line) for line in saved)


def test_run_output_recorder_redacts_jwt_authorization_and_multiline_key(tmp_path):
    log = tmp_path / "output.log"
    jwt = "eyJhbGciOiJFUzI1NiJ9.eyJzdWIiOiJ0ZXN0aW5nIn0.qETJbV0mZKcLMDM23zvFsq"
    recorder = RunOutputRecorder(
        log,
        SecretRedactor.from_env({"ASC_PRIVATE_KEY": "key-line-one\nkey-line-two"}),
    )

    recorder.record(f"Authorization: Bearer {jwt}\n")
    recorder.record_line("debug key-line-one value")
    recorder.close()

    saved = log.read_text(encoding="utf-8")
    assert jwt not in saved
    assert "Authorization: ***" in saved
    assert "key-line-one" not in saved


def test_detached_worker_persists_only_redacted_output(tmp_path):
    package = tmp_path / "cdt_steps"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "demo.py").write_text(
        "from cdt.sdk import step\n\n"
        "@step('demo.output')\n"
        "def output(ctx):\n"
        "    print('provider says ' + ctx.env['DEMO_TOKEN'], end='')\n",
        encoding="utf-8",
    )
    (tmp_path / "cdt.yaml").write_text(
        "version: 1\nplugins:\n  - cdt_steps.demo\npipelines:\n  test:\n    steps:\n      - demo.output\n",
        encoding="utf-8",
    )
    run_dir = tmp_path / ".cdt" / "runs" / "smoke-run"
    run_dir.mkdir(parents=True)
    log_path = run_dir / "output.log"
    exit_path = run_dir / "exit-code"
    status_path = run_dir / "status.json"
    env = dict(os.environ)
    env["DEMO_TOKEN"] = "detached-secret-value"
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(tmp_path), env.get("PYTHONPATH")]))

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "cdt.agent_release_worker",
            "--pipeline",
            "test",
            "--run-id",
            "smoke-run",
            "--log",
            str(log_path),
            "--exit-file",
            str(exit_path),
            "--status-file",
            str(status_path),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert exit_path.read_text(encoding="utf-8") == "0\n"
    assert "provider says ***" in log_path.read_text(encoding="utf-8")
    assert all("detached-secret-value" not in path.read_text(encoding="utf-8") for path in run_dir.iterdir())


def test_google_play_run_keeps_secrets_out_of_status_log_and_checkpoint(tmp_path, monkeypatch):
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
                "def make_aab(ctx, output: str):",
                "    aab = ctx.cwd / output",
                "    aab.write_bytes(b'android-app-bundle-bytes')",
                "    ctx.register_artifact('aab', BuildArtifact(ArtifactKind.AAB, aab, 'Android AAB'))",
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
                "  play:",
                "    risk: production",
                "    steps:",
                "      - demo.aab: {output: app-release.aab}",
                "      - google_play.upload_aab:",
                "          artifact: aab",
                "          package_name: com.example.app",
                "          track: internal",
                "          release_status: completed",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    for module_name in ("cdt_steps.play", "cdt_steps"):
        sys.modules.pop(module_name, None)

    token = "play-publishing-secret"
    adc_material = "fake-service-account-private-key-material"
    adc_file = tmp_path / "adc.json"
    adc_file.write_text(adc_material, encoding="utf-8")
    monkeypatch.setenv("DEMO_TOKEN", token)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(adc_file))

    class LeakyFakeClient:
        """Fake publishing client whose definite failure echoes a secret."""

        def __init__(self, env: dict[str, str], cwd: Path):
            pass

        def create_edit(self, package_name: str) -> dict[str, Any]:
            return {"id": "edit-1", "expiryTimeSeconds": "43200"}

        def get_edit(self, package_name: str, edit_id: str) -> dict[str, Any]:
            return {"id": edit_id}

        def list_bundles(self, package_name: str, edit_id: str) -> list[dict[str, Any]]:
            return []

        def upload_bundle(self, package_name: str, edit_id: str, aab_path: Path) -> dict[str, Any]:
            return {"versionCode": 40, "sha256": compute_file_sha256(Path(aab_path))}

        def get_track(self, package_name: str, edit_id: str, track: str) -> dict[str, Any]:
            raise GooglePlayError(STAGE_TRACK_GET, "track not found", http_status=404)

        def update_track(
            self, package_name: str, edit_id: str, track: str, releases: list[dict[str, Any]]
        ) -> dict[str, Any]:
            return {"track": track, "releases": list(releases)}

        def commit_edit(self, package_name: str, edit_id: str) -> dict[str, Any]:
            raise GooglePlayError(
                STAGE_EDIT_COMMIT,
                f"permission denied for context {token}",
                http_status=403,
            )

        def delete_edit(self, package_name: str, edit_id: str) -> None:
            pass

    monkeypatch.setattr(google_play_step, "GooglePlayClient", LeakyFakeClient)

    result = runner.invoke(app, ["run", "play", "--confirm", "play"])

    assert result.exit_code != 0
    assert token not in result.output
    assert "permission denied for context ***" in result.output

    runs = list_runs(tmp_path)
    paths = run_paths(tmp_path, runs[0]["run_id"])
    status = read_json(paths.status)
    assert status["status"] == "failed"
    assert "***" in status["error"]
    # The saved status and log are redacted end to end.
    saved_files = [path for path in paths.root.iterdir() if path.is_file()]
    assert saved_files, "run record files are expected"
    for path in saved_files:
        saved = path.read_text(encoding="utf-8")
        assert token not in saved, path.name
        assert adc_material not in saved, path.name

    # The publication checkpoint stores parameters only, never secrets.
    checkpoints = list((tmp_path / ".cdt" / "google-play" / "operations").glob("*.json"))
    assert len(checkpoints) == 1
    checkpoint_raw = checkpoints[0].read_text(encoding="utf-8")
    checkpoint = json.loads(checkpoint_raw)
    assert checkpoint["phase"] == "track_updated"
    for secret in (token, adc_material):
        assert secret not in checkpoint_raw
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in checkpoint_raw
