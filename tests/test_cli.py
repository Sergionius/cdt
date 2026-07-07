import json
import re
import subprocess
import sys

from typer.testing import CliRunner

from cdt import __version__
from cdt import self_update as self_update_module
from cdt.cli import app
from cdt.pipeline.registry import _clear_steps_for_tests
from tests._helpers import FakeResponse

runner = CliRunner()
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _visible_text(output: str) -> str:
    return ANSI_RE.sub("", output)


def setup_function():
    _clear_steps_for_tests()
    sys.modules.pop("cdt_steps.offline", None)
    sys.modules.pop("cdt_steps", None)


def teardown_function():
    _clear_steps_for_tests()
    sys.modules.pop("cdt_steps.offline", None)
    sys.modules.pop("cdt_steps", None)


def test_root_help_lists_commands():
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    for command in (
        "run",
        "pipeline",
        "doctor",
        "self-update",
    ):
        assert command in result.output


def test_root_version_flags():
    for flag in ("--version", "-V"):
        result = runner.invoke(app, [flag])

        assert result.exit_code == 0
        assert result.output == f"cdt {__version__}\n"


def test_python_module_version_flag():
    result = subprocess.run(
        [sys.executable, "-m", "cdt", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == f"cdt {__version__}\n"


def test_command_help_lists_key_options():
    cases = {
        "run": ("--id", "--dry-run"),
        "pipeline": (),
    }

    for command, options in cases.items():
        result = runner.invoke(app, [command, "--help"])
        output = _visible_text(result.output)

        assert result.exit_code == 0
        for option in options:
            assert option in output


def test_self_update_help_available():
    result = runner.invoke(app, ["self-update", "--help"])
    normalized_output = re.sub(r"\s+", "", _visible_text(result.output))

    assert result.exit_code == 0
    assert "--dry-run" in normalized_output
    assert "--check" in normalized_output
    assert "--json" in normalized_output
    assert "--manager" in normalized_output


def test_self_update_dry_run_shows_version_and_command(monkeypatch):
    monkeypatch.setattr(self_update_module, "_latest_release_tag", lambda owner, repo: "v9.9.9")
    monkeypatch.setattr(self_update_module, "_detect_install_method", lambda: ("pipx", False))

    result = runner.invoke(app, ["self-update", "--dry-run"])

    assert result.exit_code == 0
    assert f"Current version: {__version__}" in result.output
    assert "Latest release: v9.9.9" in result.output
    assert "pipx install --force git+https://github.com/Sergionius/cdt.git@v9.9.9" in result.output
    assert "Dry run" in result.output


def test_self_update_check_reports_available(monkeypatch):
    monkeypatch.setattr(self_update_module, "_latest_release_tag", lambda owner, repo: "v9.9.9")

    result = runner.invoke(app, ["self-update", "--check"])

    assert result.exit_code == 0
    assert f"Current version: {__version__}" in result.output
    assert "Latest release: v9.9.9" in result.output
    assert "Update available" in result.output


def test_self_update_check_json_reports_available(monkeypatch):
    monkeypatch.setattr(self_update_module, "_latest_release_tag", lambda owner, repo: "v9.9.9")

    result = runner.invoke(app, ["self-update", "--check", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["current"] == __version__
    assert payload["latest"] == "v9.9.9"
    assert payload["update_available"] is True
    assert payload["status"] == "update_available"


def test_self_update_network_error_reports_failure(monkeypatch):
    import urllib.error

    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(urllib.error.URLError("no route")),
    )

    result = runner.invoke(app, ["self-update"])

    assert result.exit_code != 0
    assert "Network error" in result.output or "Network error" in result.stderr


def test_self_update_rate_limit_reports_failure(monkeypatch):
    import urllib.error

    error = urllib.error.HTTPError(
        "https://api.github.com/repos/Sergionius/cdt/releases/latest",
        403,
        "Forbidden",
        {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1893456000"},
        None,
    )
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: (_ for _ in ()).throw(error))

    result = runner.invoke(app, ["self-update"])

    assert result.exit_code != 0
    assert "rate limit" in result.output.lower() or "rate limit" in result.stderr.lower()
    assert "GITHUB_TOKEN" in result.output or "GITHUB_TOKEN" in result.stderr


def test_self_update_missing_tag_reports_failure(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *args, **kwargs: FakeResponse(json.dumps({}).encode("utf-8")),
    )

    result = runner.invoke(app, ["self-update"])

    assert result.exit_code != 0
    assert "tag_name" in result.output or "tag_name" in result.stderr


def test_self_update_unknown_install_method_reports_failure(monkeypatch):
    monkeypatch.setattr(self_update_module, "_latest_release_tag", lambda owner, repo: "v9.9.9")
    monkeypatch.setattr(self_update_module, "_detect_install_method", lambda: None)

    result = runner.invoke(app, ["self-update"])

    assert result.exit_code != 0
    assert "Unable to detect" in result.output or "Unable to detect" in result.stderr


def test_self_update_dry_run_unknown_install_method_reports_manual_command(monkeypatch):
    monkeypatch.setattr(self_update_module, "_latest_release_tag", lambda owner, repo: "v9.9.9")
    monkeypatch.setattr(self_update_module, "_detect_install_method", lambda: None)

    result = runner.invoke(app, ["self-update", "--dry-run"])

    assert result.exit_code == 0
    assert "Unable to detect" in result.output
    assert "pipx install --force" in result.output


def test_migrate_command_is_unavailable():
    result = runner.invoke(app, ["migrate", "--help"])

    assert result.exit_code != 0


def test_pipeline_inspect_json_returns_step_tree(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "cdt.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "pipelines:",
                "  demo:",
                "    steps:",
                "      - flutter.pub_get",
                "      - parallel:",
                "          steps:",
                "            - web.build:",
                "                env: prod",
                "            - android.build_aab:",
                "                artifact: android_aab",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["pipeline", "inspect", "demo", "--json"])
    payload = json.loads(result.output)

    assert result.exit_code == 0
    assert payload["schema_version"] == 1
    assert payload["pipeline"] == "demo"
    assert payload["plugins"] == []
    assert payload["errors"] == []
    assert payload["steps"] == [
        {"type": "step", "name": "flutter.pub_get", "options": {}},
        {
            "type": "parallel",
            "steps": [
                {"type": "step", "name": "web.build", "options": {"env": "prod"}},
                {"type": "step", "name": "android.build_aab", "options": {"artifact": "android_aab"}},
            ],
        },
    ]


def test_pipeline_validate_json_reports_unknown_step(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "cdt.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "pipelines:",
                "  demo:",
                "    steps:",
                "      - missing.step",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["pipeline", "validate", "demo", "--json"])
    payload = json.loads(result.output)

    assert result.exit_code == 1
    assert payload["schema_version"] == 1
    assert payload["pipeline"] == "demo"
    assert payload["errors"][0]["code"] == "unknown_step"
    assert payload["errors"][0]["path"] == "pipelines.demo.steps[0]"


def test_pipeline_steps_json_includes_plugin_steps(tmp_path, monkeypatch):
    package = tmp_path / "cdt_steps"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "offline.py").write_text(
        "\n".join(
            [
                "from cdt.sdk import step",
                "",
                "@step('offline.fetch_config')",
                "def fetch_config(ctx, output: str):",
                "    pass",
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
                "  - cdt_steps.offline",
                "pipelines:",
                "  offline-test:",
                "    steps:",
                "      - offline.fetch_config:",
                "          output: build/offline/config.json",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("cdt_steps.offline", None)
    sys.modules.pop("cdt_steps", None)

    result = runner.invoke(app, ["pipeline", "steps", "--json"])
    payload = json.loads(result.output)

    assert result.exit_code == 0
    assert payload["schema_version"] == 1
    assert "flutter.pub_get" in payload["registered_steps"]
    assert "offline.fetch_config" in payload["registered_steps"]
    steps = {step["name"]: step for step in payload["steps"]}
    assert steps["flutter.pub_get"]["name"] == "flutter.pub_get"
    assert steps["flutter.pub_get"]["category"] == "flutter"
    assert steps["flutter.pub_get"]["risk"] == "safe"
    firebase = steps["firebase.upload_app_distribution"]
    assert "requires_artifacts" not in firebase
    assert firebase["requires"] == [
        {
            "result_types": ["android_aab", "android_apk"],
            "mode": "any",
            "name_options": ["artifact"],
        }
    ]
    assert firebase["produces"] == [{"result_type": "upload_result", "name_options": []}]
    assert steps["offline.fetch_config"]["risk"] == "custom"
    assert steps["offline.fetch_config"]["plugin"] is True
