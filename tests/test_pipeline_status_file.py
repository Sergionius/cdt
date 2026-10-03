import json
import sys

import pytest
import typer
from typer.testing import CliRunner

from cdt.cli import app
from cdt.pipeline.registry import _clear_steps_for_tests
from cdt.runs import list_runs, read_json
from cdt.services.appstore_state import save_upload_record
from tests.test_services_appstore_state import FakeAsc, _stub_client

runner = CliRunner()


@pytest.mark.parametrize("secret_key", [False, True])
def test_values_checkpoint_redaction_blocks_resume(tmp_path, secret_key):
    from cdt.pipeline import PipelineContext
    from cdt.pipeline.runner import _restore_resume_status
    from cdt.runner import CommandRunner

    status = tmp_path / "status.json"
    secret = "super-sensitive-value"
    values = {secret: "value"} if secret_key else {"data": secret}
    ctx = PipelineContext(
        cwd=tmp_path, env={"API_TOKEN": secret}, runner=CommandRunner(), values=values, status_file=status
    )
    ctx.write_status("failed")
    assert secret not in status.read_text()
    assert json.loads(status.read_text())["values_state"]["restorable"] is False
    restored = PipelineContext(cwd=tmp_path, env={}, runner=CommandRunner())
    with pytest.raises(typer.BadParameter, match="not restorable"):
        _restore_resume_status(restored, status)


def test_status_parallel_completion_and_checkpoint_are_one_snapshot(tmp_path):
    from cdt.pipeline import ParallelStepGroup, PipelineContext, PipelineExecutor
    from cdt.runner import CommandRunner
    from tests.test_pipeline_executor import CallbackStep

    status = tmp_path / "status.json"
    ctx = PipelineContext(cwd=tmp_path, env={}, runner=CommandRunner(), status_file=status)
    snapshots = []
    original = ctx.write_status

    def capture(state):
        # Called under the same status lock as mutations.
        with ctx._status_lock:
            original(state)
            snapshots.append(json.loads(status.read_text()))

    ctx.write_status = capture
    steps = [CallbackStep(f"0/{i}", lambda ctx, i=i: ctx.values.update({str(i): str(i)})) for i in range(12)]
    PipelineExecutor().run([ParallelStepGroup(steps, "0")], ctx)
    for snapshot in snapshots:
        groups = snapshot["values_state"]["groups"]
        for i in range(12):
            if f"0/{i}" in snapshot["completed_steps"]:
                values = groups["0"]["branches"][f"0/{i}"] if groups else snapshot["values_state"]["root"]
                assert values[str(i)] == str(i)
    assert len(ctx.values) == 12


def test_status_separates_skipped_leaves_from_completed(tmp_path, monkeypatch):
    _write_demo_project(tmp_path)
    (tmp_path / "cdt.yaml").write_text(
        "version: 1\nplugins: [cdt_steps.demo]\npipelines:\n  demo:\n"
        "    inputs: {deploy: {}}\n    steps:\n"
        "      - step: demo.artifact\n        when: {input: deploy, present: true}\n"
        "      - demo.ok\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    output = tmp_path / "out.json"
    result = runner.invoke(app, ["run", "demo", "--status-file", str(output)])
    assert result.exit_code == 0, result.output
    status = json.loads(output.read_text())
    assert status["status"] == "success"
    assert status["skipped_steps"] == ["0"]
    assert status["completed_steps"] == ["1"]
    assert status["step_decisions"] == {"0": "skip", "1": "run"}
    assert status["artifacts"] == []
    assert not (tmp_path / "build-count.txt").exists()


def setup_function():
    _clear_steps_for_tests()
    sys.modules.pop("cdt_steps.demo", None)
    sys.modules.pop("cdt_steps", None)


def teardown_function():
    _clear_steps_for_tests()
    sys.modules.pop("cdt_steps.demo", None)
    sys.modules.pop("cdt_steps", None)


def _write_demo_project(tmp_path, *, failing: bool = False, artifact: bool = False, inputs_yaml: str = "") -> None:
    package = tmp_path / "cdt_steps"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "demo.py").write_text(
        "\n".join(
            [
                "import typer",
                "from cdt.sdk import step",
                "",
                "@step('demo.ok')",
                "def ok(ctx):",
                "    typer.echo('demo.ok diagnostics line')",
                "    ctx.values['ok'] = '1'",
                "",
                "@step('demo.fail')",
                "def fail(ctx):",
                "    raise typer.BadParameter('boom')",
                "",
                "@step('demo.artifact')",
                "def artifact(ctx):",
                "    from cdt.artifacts import ArtifactKind, BuildArtifact",
                "    count = ctx.cwd / 'build-count.txt'",
                "    value = int(count.read_text()) if count.exists() else 0",
                "    count.write_text(str(value + 1), encoding='utf-8')",
                "    path = ctx.cwd / 'app.aab'",
                "    path.write_text('artifact', encoding='utf-8')",
                "    ctx.register_artifact('app', BuildArtifact(ArtifactKind.AAB, path, 'App'))",
                "",
                "@step('demo.upload')",
                "def upload(ctx):",
                "    ctx.artifact('app')",
                "    count = ctx.cwd / 'upload-count.txt'",
                "    value = int(count.read_text()) if count.exists() else 0",
                "    count.write_text(str(value + 1), encoding='utf-8')",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    if artifact:
        steps = ["demo.artifact", "demo.upload"]
    else:
        steps = ["demo.ok", "demo.fail"] if failing else ["demo.ok"]
    (tmp_path / "cdt.yaml").write_text(
        "version: 1\nplugins:\n  - cdt_steps.demo\npipelines:\n  demo:\n"
        + inputs_yaml
        + "    steps:\n"
        + "".join(f"      - {step}\n" for step in steps),
        encoding="utf-8",
    )


def test_run_status_file_records_success(tmp_path, monkeypatch):
    _write_demo_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    status_file = tmp_path / ".cdt" / "status.json"

    result = runner.invoke(app, ["run", "demo", "--status-file", str(status_file)])
    payload = json.loads(status_file.read_text(encoding="utf-8"))

    assert result.exit_code == 0
    assert payload["status"] == "success"
    assert payload["pipeline"] == "demo"
    assert payload["completed_steps"] == ["0"]
    assert payload["current_step"] is None
    assert payload["started_at"]
    assert payload["finished_at"]


def test_run_status_file_records_failure(tmp_path, monkeypatch):
    _write_demo_project(tmp_path, failing=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    status_file = tmp_path / ".cdt" / "status.json"

    result = runner.invoke(app, ["run", "demo", "--status-file", str(status_file)])
    payload = json.loads(status_file.read_text(encoding="utf-8"))

    assert result.exit_code != 0
    assert payload["status"] == "failed"
    assert payload["completed_steps"] == ["0"]
    assert payload["failed_step"] == "1"
    assert "boom" in payload["error"]


def test_run_status_file_records_retry_attempts_with_redacted_last_error(tmp_path, monkeypatch):
    package = tmp_path / "cdt_steps"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "demo.py").write_text(
        "\n".join(
            [
                "from cdt.sdk import RetryableStepError, step",
                "",
                "@step('demo.transient', retry_safe=True)",
                "def transient(ctx):",
                "    count = ctx.cwd / 'attempts.txt'",
                "    value = int(count.read_text(encoding='utf-8')) if count.exists() else 0",
                "    count.write_text(str(value + 1), encoding='utf-8')",
                "    if value < 2:",
                "        raise RetryableStepError('token supersecret123 unavailable')",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "cdt.yaml").write_text(
        "version: 1\nplugins:\n  - cdt_steps.demo\npipelines:\n  demo:\n    steps:\n"
        "      - step: demo.transient\n        retry: {max_attempts: 3, delay_seconds: 0}\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("RETRY_SECRET", "supersecret123")
    status_file = tmp_path / ".cdt" / "status.json"

    result = runner.invoke(app, ["run", "demo", "--status-file", str(status_file)])
    payload = json.loads(status_file.read_text(encoding="utf-8"))

    assert result.exit_code == 0, result.output
    assert payload["status"] == "success"
    assert payload["completed_steps"] == ["0"]
    assert payload["failed_step"] is None
    # Attempts count and the redacted intermediate error survive; the retry
    # itself is never a terminal failure. Two attempts failed, the third ran.
    assert payload["step_attempts"] == {"0": {"attempts": 2, "last_error": "token *** unavailable"}}
    assert "supersecret123" not in status_file.read_text(encoding="utf-8")


def test_run_status_file_coexists_with_nonempty_run_log(tmp_path, monkeypatch):
    _write_demo_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    status_file = tmp_path / ".cdt" / "status.json"

    result = runner.invoke(app, ["run", "demo", "--status-file", str(status_file)])
    runs = list_runs(tmp_path)
    log = (tmp_path / ".cdt" / "runs" / runs[0]["run_id"] / "output.log").read_text(encoding="utf-8")
    payload = json.loads(status_file.read_text(encoding="utf-8"))

    assert result.exit_code == 0
    assert payload["status"] == "success"
    assert "demo.ok diagnostics line" in log


def test_run_status_file_failure_log_contains_terminal_summary(tmp_path, monkeypatch):
    _write_demo_project(tmp_path, failing=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    status_file = tmp_path / ".cdt" / "status.json"

    result = runner.invoke(app, ["run", "demo", "--status-file", str(status_file)])
    runs = list_runs(tmp_path)
    log = (tmp_path / ".cdt" / "runs" / runs[0]["run_id"] / "output.log").read_text(encoding="utf-8")
    payload = json.loads(status_file.read_text(encoding="utf-8"))

    assert result.exit_code != 0
    assert payload["status"] == "failed"
    assert payload["failed_step"] == "1"
    assert "boom" in log
    assert "PipelineExecutionError" not in log
    assert "Pipeline failed at step" in log
    assert "Artifacts produced: none" in log


def test_run_status_file_records_release_confirmation_results(tmp_path, monkeypatch):
    package = tmp_path / "cdt_steps"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "demo.py").write_text(
        "\n".join(
            [
                "from cdt.sdk import step",
                "",
                "@step('demo.confirm')",
                "def confirm(ctx):",
                "    ctx.register_release_results({",
                "        'github_release_url': 'https://github.com/example/cdt/releases/tag/v0.5.2',",
                "        'pypi_release_url': 'https://pypi.org/project/cdt-release/0.5.2/',",
                "        'blank': '  ',",
                "    })",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "cdt.yaml").write_text(
        "version: 1\nplugins:\n  - cdt_steps.demo\npipelines:\n  demo:\n    steps:\n      - demo.confirm\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    status_file = tmp_path / ".cdt" / "status.json"

    result = runner.invoke(app, ["run", "demo", "--status-file", str(status_file)])
    payload = json.loads(status_file.read_text(encoding="utf-8"))

    assert result.exit_code == 0, result.output
    assert payload["status"] == "success"
    assert payload["release_results"] == {
        "github_release_url": "https://github.com/example/cdt/releases/tag/v0.5.2",
        "pypi_release_url": "https://pypi.org/project/cdt-release/0.5.2/",
    }


def _write_submit_project(tmp_path) -> None:
    (tmp_path / "cdt.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "pipelines:",
                "  submit:",
                "    risk: production",
                "    inputs:",
                "      whats_new:",
                "        required: true",
                "    steps:",
                "      - appstore.submit_review:",
                "          whats_new:",
                '            ru: "${inputs.whats_new}"',
                "          release_mode: manual",
                "          phased_release: true",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "IOS_BUNDLE_ID=com.example.app",
                "ASC_KEY_ID=SECRETKEYID42",
                "ASC_ISSUER_ID=SECRETISSUER42",
                "ASC_PRIVATE_KEY_PATH=secrets/SUPERSECRETKEY.p8",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def test_submit_review_status_file_records_result_without_secrets(tmp_path, monkeypatch):
    _write_submit_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    save_upload_record(tmp_path, "com.example.app", "1.2.3+5")
    FakeAsc(monkeypatch)
    _stub_client(monkeypatch)  # credentials are never used against real Apple
    status_file = tmp_path / ".cdt" / "status.json"

    result = runner.invoke(
        app,
        ["run", "submit", "--input", "whats_new=Исправления", "--confirm", "submit", "--status-file", str(status_file)],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(status_file.read_text(encoding="utf-8"))
    assert payload["status"] == "success"
    assert payload["error"] is None
    release_results = dict(payload["release_results"])
    submission_id = release_results.pop("appstore_review_submission_id")
    assert submission_id
    assert release_results == {
        "appstore_review_bundle_id": "com.example.app",
        "appstore_review_marketing_version": "1.2.3",
        "appstore_review_build_number": "5",
        "appstore_review_submission_state": "WAITING_FOR_REVIEW",
        "appstore_review_release_mode": "manual",
        "appstore_review_phased_release": "true",
    }
    # No ASC credential material reaches the status file.
    text = status_file.read_text(encoding="utf-8")
    for secret in ("SECRETKEYID42", "SECRETISSUER42", "SUPERSECRETKEY", "BEGIN PRIVATE KEY"):
        assert secret not in text
    # The message separates review submission from user availability.
    assert "Submitted for App Store review" in result.output
    assert "does NOT mean approval or user availability" in result.output
    assert "available to users" not in result.output.split("Submitted for App Store review")[0]


def test_run_resume_from_restores_artifacts_and_skips_prior_steps(tmp_path, monkeypatch):
    _write_demo_project(tmp_path, artifact=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    status_file = tmp_path / ".cdt" / "status.json"

    first = runner.invoke(app, ["run", "demo", "--status-file", str(status_file)])
    second = runner.invoke(
        app,
        [
            "run",
            "demo",
            "--resume-status-file",
            str(status_file),
            "--status-file",
            str(status_file),
            "--resume-from",
            "demo.upload",
        ],
    )

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert (tmp_path / "build-count.txt").read_text(encoding="utf-8") == "1"
    assert (tmp_path / "upload-count.txt").read_text(encoding="utf-8") == "2"


def test_run_skip_completed_does_not_execute_completed_steps(tmp_path, monkeypatch):
    _write_demo_project(tmp_path, artifact=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    status_file = tmp_path / ".cdt" / "status.json"

    first = runner.invoke(app, ["run", "demo", "--status-file", str(status_file)])
    second = runner.invoke(
        app,
        [
            "run",
            "demo",
            "--resume-status-file",
            str(status_file),
            "--status-file",
            str(status_file),
            "--skip-completed",
        ],
    )

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert (tmp_path / "build-count.txt").read_text(encoding="utf-8") == "1"
    assert (tmp_path / "upload-count.txt").read_text(encoding="utf-8") == "1"


def test_run_resume_fails_when_restored_artifact_is_missing(tmp_path, monkeypatch):
    _write_demo_project(tmp_path, artifact=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    status_file = tmp_path / ".cdt" / "status.json"

    first = runner.invoke(app, ["run", "demo", "--status-file", str(status_file)])
    (tmp_path / "app.aab").unlink()
    second = runner.invoke(
        app,
        [
            "run",
            "demo",
            "--resume-status-file",
            str(status_file),
            "--status-file",
            str(status_file),
            "--resume-from",
            "demo.upload",
        ],
    )

    assert first.exit_code == 0
    assert second.exit_code != 0
    assert "Resume artifact does not exist" in second.output


def test_run_records_inputs_in_status_and_manifest(tmp_path, monkeypatch):
    _write_demo_project(tmp_path, inputs_yaml="    inputs:\n      version:\n      channel:\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))

    result = runner.invoke(app, ["run", "demo", "--input", "version=0.5.2", "--input", "channel=beta"])

    assert result.exit_code == 0, result.output
    runs = list_runs(tmp_path)
    assert len(runs) == 1
    run_dir = tmp_path / ".cdt" / "runs" / runs[0]["run_id"]
    status = read_json(run_dir / "status.json")
    manifest = read_json(run_dir / "manifest.json")
    assert status["inputs"] == {"version": "0.5.2", "channel": "beta"}
    assert manifest["inputs"] == {"version": "0.5.2", "channel": "beta"}
    assert manifest["command"] == [
        "cdt",
        "run",
        "demo",
        "--input",
        "version=0.5.2",
        "--input",
        "channel=beta",
    ]


def test_run_redacts_secret_shaped_inputs_before_persistence(tmp_path, monkeypatch):
    _write_demo_project(
        tmp_path,
        inputs_yaml="    inputs:\n      version:\n      release_token:\n",
    )
    (tmp_path / ".env").write_text("RELEASE_TOKEN=supersecret7\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))

    result = runner.invoke(
        app,
        ["run", "demo", "--input", "version=0.5.2", "--input", "release_token=supersecret7"],
    )

    assert result.exit_code == 0, result.output
    runs = list_runs(tmp_path)
    run_dir = tmp_path / ".cdt" / "runs" / runs[0]["run_id"]
    status_text = (run_dir / "status.json").read_text(encoding="utf-8")
    manifest_text = (run_dir / "manifest.json").read_text(encoding="utf-8")
    assert "supersecret7" not in status_text
    assert "supersecret7" not in manifest_text
    assert "***" in status_text
