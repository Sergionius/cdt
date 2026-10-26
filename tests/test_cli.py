import json
import re
import signal
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from typer.testing import CliRunner

from cdt import __version__
from cdt import self_update as self_update_module
from cdt.cli import app
from cdt.pipeline.registry import _clear_steps_for_tests
from cdt.runs import RunPaths, create_run, list_runs, read_json, run_paths, write_exit_code, write_json_atomic
from tests._helpers import FakeResponse

runner = CliRunner()
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _visible_text(output: str) -> str:
    return ANSI_RE.sub("", output)


def _portable_error_text(output: str) -> str:
    """Error text without ANSI styles and Rich panel borders.

    Rich wraps long error messages at panel width, so wrapped lines carry
    their own ANSI sequences and border characters between words. Removing
    them makes message assertions independent of the terminal width.
    """
    without_borders = re.sub(r"[│╭╮╰╯─]+", " ", _visible_text(output))
    return " ".join(without_borders.split())


def test_pipeline_plan_input_option_and_invalid_inputs(tmp_path, monkeypatch):
    help_result = runner.invoke(app, ["pipeline", "plan", "--help"])
    assert help_result.exit_code == 0
    assert "--input" in _visible_text(help_result.output)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "cdt.yaml").write_text(
        "version: 1\npipelines:\n  demo:\n    inputs: {deploy: {}}\n    steps:\n"
        "      - step: flutter.pub_get\n        when: {input: deploy, present: true}\n"
    )
    result = runner.invoke(app, ["pipeline", "plan", "demo", "--json", "--input", "unknown=yes"])
    assert result.exit_code != 0
    assert json.loads(result.output)["errors"][0]["code"] == "invalid_pipeline_input"


def setup_function():
    _clear_steps_for_tests()
    sys.modules.pop("cdt_steps.offline", None)
    sys.modules.pop("cdt_steps.capture", None)
    sys.modules.pop("cdt_steps", None)


def teardown_function():
    _clear_steps_for_tests()
    sys.modules.pop("cdt_steps.offline", None)
    sys.modules.pop("cdt_steps.capture", None)
    sys.modules.pop("cdt_steps", None)


def test_root_help_lists_commands():
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    for command in (
        "init",
        "run",
        "history",
        "status",
        "logs",
        "schema",
        "pipeline",
        "agent-release",
        "settings",
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


def test_root_registers_exact_command_set_without_duplicates():
    command_names = [info.name for info in app.registered_commands]
    group_names = [info.name for info in app.registered_groups]

    assert len(command_names) == len(set(command_names))
    assert len(group_names) == len(set(group_names))
    assert not set(command_names) & set(group_names)
    assert set(command_names) == {"init", "run", "status", "logs", "schema", "doctor", "self-update"}
    assert set(group_names) == {"settings", "pipeline", "agent-release", "history"}


def test_groups_register_exact_subcommand_sets_without_duplicates():
    expected = {
        "settings": {"enable", "disable", "show"},
        "pipeline": {"list", "inspect", "plan", "validate", "preflight", "steps"},
        "agent-release": {"start", "status", "stop"},
        "history": {"prune"},
    }
    actual = {}
    for group_info in app.registered_groups:
        assert group_info.name not in actual
        names = [info.name for info in group_info.typer_instance.registered_commands]
        assert len(names) == len(set(names))
        actual[group_info.name] = set(names)

    assert actual == expected


def _click_command(*path: str):
    """Resolve the effective click command for a CLI path, e.g. ("history", "prune")."""
    from typer.main import get_command

    command = get_command(app)
    for part in path:
        command = command.commands[part]
    return command


def _param_contract(command) -> list[tuple]:
    """Parameter contract per command: (name, option flags, default, required, hidden)."""
    return [
        (param.name, tuple(param.opts), param.default, param.required, bool(getattr(param, "hidden", False)))
        for param in command.params
    ]


# The hand-written Typer parameter contract, pinned from the current CLI
# declarations: arguments carry their plain name as the only "flag".
_EXPECTED_PARAM_CONTRACT = {
    ("self-update",): [
        ("dry_run", ("--dry-run",), False, False, False),
        ("check", ("--check",), False, False, False),
        ("json_output", ("--json",), False, False, False),
        ("manager", ("--manager",), None, False, False),
    ],
    ("doctor",): [("json_output", ("--json",), False, False, False)],
    ("schema",): [("output", ("--output", "-o"), None, False, False)],
    ("init",): [("force", ("--force",), False, False, False)],
    ("status",): [
        ("run_id", ("run_id",), None, False, False),
        ("pipeline", ("--pipeline",), None, False, False),
        ("json_output", ("--json",), False, False, False),
    ],
    ("logs",): [
        ("run_id", ("run_id",), None, False, False),
        ("pipeline", ("--pipeline",), None, False, False),
        ("tail", ("--tail",), 80, False, False),
    ],
    ("run",): [
        ("name", ("name",), None, True, False),
        ("id", ("--id",), [], False, False),
        ("input", ("--input",), [], False, False),
        ("dry_run", ("--dry-run",), False, False, False),
        ("status_file", ("--status-file",), None, False, False),
        ("resume_from", ("--resume-from",), None, False, False),
        ("skip_completed", ("--skip-completed",), False, False, False),
        ("resume_status_file", ("--resume-status-file",), None, False, False),
        ("confirm", ("--confirm",), None, False, False),
        ("run_id", ("--run-id",), None, False, True),
        ("capture_output", ("--capture-output",), False, False, False),
        ("capture_child", ("--capture-child",), False, False, True),
    ],
    ("settings", "enable"): [("name", ("name",), None, True, False)],
    ("settings", "disable"): [("name", ("name",), None, True, False)],
    ("settings", "show"): [],
    ("pipeline", "list"): [],
    ("pipeline", "inspect"): [
        ("name", ("name",), None, True, False),
        ("json_output", ("--json",), False, False, False),
    ],
    ("pipeline", "plan"): [
        ("name", ("name",), None, True, False),
        ("input", ("--input",), [], False, False),
        ("json_output", ("--json",), False, False, False),
    ],
    ("pipeline", "validate"): [
        ("name", ("name",), None, False, False),
        ("json_output", ("--json",), False, False, False),
        ("strict", ("--strict",), False, False, False),
    ],
    ("pipeline", "preflight"): [
        ("name", ("name",), None, True, False),
        ("json_output", ("--json",), False, False, False),
    ],
    ("pipeline", "steps"): [("json_output", ("--json",), False, False, False)],
    ("agent-release", "start"): [
        ("pipeline", ("pipeline",), None, True, False),
        ("id", ("--id",), [], False, False),
        ("input", ("--input",), [], False, False),
        ("confirm", ("--confirm",), None, False, False),
        ("json_output", ("--json",), False, False, False),
    ],
    ("agent-release", "status"): [
        ("pipeline", ("pipeline",), None, False, False),
        ("run_id", ("--run",), None, False, False),
        ("wait", ("--wait",), False, False, False),
        ("timeout", ("--timeout",), "40m", False, False),
        ("json_output", ("--json",), False, False, False),
    ],
    ("agent-release", "stop"): [
        ("pipeline", ("pipeline",), None, False, False),
        ("run_id", ("--run",), None, False, False),
        ("timeout", ("--timeout",), "30s", False, False),
        ("json_output", ("--json",), False, False, False),
    ],
    ("history", "prune"): [
        ("older_than", ("--older-than",), None, False, False),
        ("keep_last", ("--keep-last",), None, False, False),
        ("pipeline", ("--pipeline",), None, False, False),
        ("dry_run", ("--dry-run",), False, False, False),
        ("confirm", ("--confirm",), False, False, False),
        ("json_output", ("--json",), False, False, False),
    ],
}


def test_command_parameters_match_pinned_contract():
    for path, expected in _EXPECTED_PARAM_CONTRACT.items():
        assert _param_contract(_click_command(*path)) == expected, path


def test_root_callback_pins_version_option_alias_and_eagerness():
    version = {param.name: param for param in _click_command().params}["version"]

    assert tuple(version.opts) == ("--version", "-V")
    assert version.default is False
    assert version.required is False
    assert version.is_eager is True


def test_ranged_options_keep_current_defaults_and_bounds():
    limit = {param.name: param for param in _click_command("history").params}["limit"]
    tail = {param.name: param for param in _click_command("logs").params}["tail"]
    keep_last = {param.name: param for param in _click_command("history", "prune").params}["keep_last"]

    assert limit.default == 20
    assert (limit.type.min, limit.type.max) == (1, 500)
    assert (tail.type.min, tail.type.max) == (1, 10000)
    assert keep_last.default is None
    assert keep_last.type.min == 0


def test_root_and_groups_show_help_when_called_without_arguments():
    # The root and the settings/pipeline/agent-release groups use
    # no_args_is_help; history lists runs instead (see its own tests).
    assert _click_command().no_args_is_help is True
    for group in ("settings", "pipeline", "agent-release"):
        assert _click_command(group).no_args_is_help is True

    for argv in ([], ["settings"], ["pipeline"], ["agent-release"]):
        result = runner.invoke(app, argv)

        assert result.exit_code == 2, argv
        assert "Usage" in result.output, argv


def test_history_without_subcommand_lists_runs_instead_of_help(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    paths = create_run(tmp_path, "demo")

    result = runner.invoke(app, ["history"])

    assert result.exit_code == 0
    assert paths.run_id in result.stdout


def test_history_prune_without_filters_fails_without_listing_history(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    paths = create_run(tmp_path, "demo")

    result = runner.invoke(app, ["history", "prune"])

    assert result.exit_code == 2
    assert "Provide at least one filter" in _portable_error_text(result.output)
    assert paths.run_id not in result.output
    assert "No recorded runs." not in result.output
    assert paths.root.exists()


def test_python_cli_module_version_flag():
    result = subprocess.run(
        [sys.executable, "-m", "cdt.cli", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == f"cdt {__version__}\n"


def test_command_help_lists_key_options():
    cases = {
        "run": ("--id", "--dry-run", "--status-file", "--confirm"),
        "history": ("--pipeline", "--status", "--limit", "--json"),
        "status": ("--pipeline", "--json"),
        "logs": ("--pipeline", "--tail"),
        "pipeline": (),
        "agent-release": (),
        "agent-release start": ("--id", "--confirm", "--json"),
        "agent-release status": ("--run", "--wait", "--timeout", "--json"),
        "agent-release stop": ("--run", "--timeout", "--json"),
    }

    for command, options in cases.items():
        result = runner.invoke(app, [*command.split(), "--help"])
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
        {"type": "step", "step_id": "0", "name": "flutter.pub_get", "options": {}},
        {
            "type": "parallel",
            "step_id": "1",
            "steps": [
                {"type": "step", "step_id": "1/0", "name": "web.build", "options": {"env": "prod"}},
                {
                    "type": "step",
                    "step_id": "1/1",
                    "name": "android.build_aab",
                    "options": {"artifact": "android_aab"},
                },
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


def test_pipeline_inspect_json_includes_inputs_declarations(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "cdt.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "pipelines:",
                "  demo:",
                "    inputs:",
                "      version:",
                "        required: true",
                "        pattern: '^\\d+\\.\\d+\\.\\d+$'",
                "    steps:",
                "      - flutter.pub_get",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["pipeline", "inspect", "demo", "--json"])
    payload = json.loads(result.output)

    assert result.exit_code == 0
    assert payload["inputs"] == {"version": {"required": True, "pattern": r"^\d+\.\d+\.\d+$"}}


# -- Foreground --capture-output -----------------------------------------------------

_CAPTURE_SECRET = "e2e-capture-token-9sett"


def _write_capture_project(tmp_path: Path, *, risk: str = "standard") -> None:
    """Plugin emitting lines through print, os.write and a child subprocess."""
    for module_name in ("cdt_steps", "cdt_steps.capture"):
        sys.modules.pop(module_name, None)
    package = tmp_path / "cdt_steps"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "capture.py").write_text(
        "\n".join(
            [
                "import os",
                "import subprocess",
                "import sys",
                "from pathlib import Path",
                "",
                "from cdt.sdk import step",
                "",
                "@step('capture.emit')",
                "def emit(ctx):",
                "    secret = os.environ['CAPTURE_E2E_TOKEN']",
                "    print('python-line value=' + secret, flush=True)",
                "    os.write(1, b'os-write-line value=' + secret.encode() + b'\\n')",
                "    code = \"print('subprocess-line value=' + __import__('os').environ['CAPTURE_E2E_TOKEN'])\"",
                "    subprocess.run([sys.executable, '-c', code], check=False)",
                "    marker = os.environ.get('CAPTURE_MARKER_FILE')",
                "    if marker:",
                "        Path(marker).write_text('called\\n', encoding='utf-8')",
                "",
                "@step('capture.hard_exit')",
                "def hard_exit(ctx):",
                "    print('exiting without terminal status', flush=True)",
                "    os._exit(0)",
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
                "  - cdt_steps.capture",
                "pipelines:",
                "  demo:",
                f"    risk: {risk}",
                "    steps:",
                "      - capture.emit",
                "  hardexit:",
                "    steps:",
                "      - capture.hard_exit",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _capture_project(tmp_path, monkeypatch, *, risk: str = "standard") -> None:
    _write_capture_project(tmp_path, risk=risk)
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("CAPTURE_E2E_TOKEN", _CAPTURE_SECRET)


def test_capture_output_end_to_end_records_single_redacted_run(tmp_path, monkeypatch):
    _capture_project(tmp_path, monkeypatch)

    result = runner.invoke(app, ["run", "demo", "--capture-output"])
    runs = list_runs(tmp_path)

    assert result.exit_code == 0, result.output
    assert len(runs) == 1
    assert runs[0]["status"] == "success"
    assert result.output.count("Run: ") == 1
    paths = run_paths(tmp_path, runs[0]["run_id"])
    manifest = read_json(paths.manifest)
    assert manifest["detached"] is False
    assert manifest["capture_output"] is True
    assert "--capture-output" in manifest["command"]
    assert "cdt_version" in manifest
    saved = paths.log.read_text(encoding="utf-8")
    for line in ("python-line value=***", "os-write-line value=***", "subprocess-line value=***"):
        assert line in saved
        assert line in result.output
    assert saved.count("python-line value=") == 1
    assert _CAPTURE_SECRET not in saved
    assert _CAPTURE_SECRET not in result.output
    status = read_json(paths.status)
    assert status["status"] == "success"
    assert paths.exit.read_text(encoding="utf-8").strip() == "0"


def test_capture_output_supports_status_logs_and_custom_status_file(tmp_path, monkeypatch):
    _capture_project(tmp_path, monkeypatch)
    status_file = tmp_path / "out" / "mirror-status.json"

    result = runner.invoke(app, ["run", "demo", "--capture-output", "--status-file", str(status_file)])
    runs = list_runs(tmp_path)
    shown = runner.invoke(app, ["status", "--json"])
    logs = runner.invoke(app, ["logs", "--tail", "5"])

    assert result.exit_code == 0, result.output
    assert len(runs) == 1
    assert json.loads(shown.output)["run_id"] == runs[0]["run_id"]
    assert json.loads(shown.output)["status"] == "success"
    assert "python-line value=***" in logs.output
    mirror = json.loads(status_file.read_text(encoding="utf-8"))
    assert mirror["status"] == "success"
    assert mirror["run_id"] == runs[0]["run_id"]


def test_capture_flag_combinations_are_rejected_before_any_run(tmp_path, monkeypatch):
    _capture_project(tmp_path, monkeypatch)

    child_without_run = runner.invoke(app, ["run", "demo", "--capture-child"])
    recursive = runner.invoke(app, ["run", "demo", "--run-id", "x-run", "--capture-child", "--capture-output"])
    capture_with_run = runner.invoke(app, ["run", "demo", "--capture-output", "--run-id", "x-run"])

    for result in (child_without_run, recursive, capture_with_run):
        assert result.exit_code != 0
    assert not (tmp_path / ".cdt" / "runs").exists()


def test_capture_output_rejected_on_unsupported_platform_before_record(tmp_path, monkeypatch):
    _capture_project(tmp_path, monkeypatch)

    def reject():
        from cdt.foreground_run import ForegroundCaptureError

        raise ForegroundCaptureError("Foreground output capture requires POSIX process-group primitives (Linux/macOS)")

    monkeypatch.setattr("cdt.cli.require_posix_capture", reject)

    result = runner.invoke(app, ["run", "demo", "--capture-output"])

    assert result.exit_code == 1
    assert "POSIX" in result.output
    assert not (tmp_path / ".cdt" / "runs").exists()


def test_capture_output_dry_run_never_executes_or_records(tmp_path, monkeypatch):
    _capture_project(tmp_path, monkeypatch)

    result = runner.invoke(app, ["run", "demo", "--capture-output", "--dry-run"])

    assert result.exit_code == 0
    assert "Pipeline: demo" in result.output
    assert not (tmp_path / ".cdt").exists()


def test_capture_output_requires_explicit_production_confirmation(tmp_path, monkeypatch):
    _capture_project(tmp_path, monkeypatch, risk="production")

    unconfirmed = runner.invoke(app, ["run", "demo", "--capture-output"])
    wrong = runner.invoke(app, ["run", "demo", "--capture-output", "--confirm", "wrong"])

    for result in (unconfirmed, wrong):
        assert result.exit_code != 0
        assert "--confirm demo" in _portable_error_text(result.output)
    assert not (tmp_path / ".cdt" / "runs").exists()

    accepted = runner.invoke(app, ["run", "demo", "--capture-output", "--confirm", "demo"])

    assert accepted.exit_code == 0, accepted.output
    assert len(list_runs(tmp_path)) == 1


def test_capture_output_records_safe_error_when_child_skips_terminal_status(tmp_path, monkeypatch):
    _capture_project(tmp_path, monkeypatch)

    result = runner.invoke(app, ["run", "hardexit", "--capture-output"])
    runs = list_runs(tmp_path)

    assert result.exit_code == 1
    assert len(runs) == 1
    assert runs[0]["status"] == "failed"
    paths = run_paths(tmp_path, runs[0]["run_id"])
    status = read_json(paths.status)
    assert status["status"] == "failed"
    assert "without writing a terminal status" in status["error"]
    assert paths.exit.read_text(encoding="utf-8").strip() == "1"


def test_finalize_captured_run_marks_incomplete_capture_as_failure(tmp_path):
    paths = create_run(tmp_path, "demo", run_id="capture-problem")
    payload = read_json(paths.status)
    payload.update(
        {
            "status": "success",
            "completed_steps": ["0"],
            "artifacts": [{"name": "aab", "path": "build/app.aab"}],
        }
    )
    write_json_atomic(paths.status, payload)

    from cdt.cli import _finalize_captured_run

    problems = ("terminal output failed; live copy stopped: broken",)
    exit_code = _finalize_captured_run({}, paths, "demo", None, 0, None, capture_problems=problems)

    assert exit_code == 1
    saved = read_json(paths.status)
    assert saved["status"] == "failed"
    assert "Foreground capture was incomplete" in saved["error"]
    assert "terminal output failed" in saved["error"]
    # Child progress is preserved, never rewritten as a clean success.
    assert saved["completed_steps"] == ["0"]
    assert saved["artifacts"] == [{"name": "aab", "path": "build/app.aab"}]
    assert paths.exit.read_text(encoding="utf-8").strip() == "1"


def test_finalize_captured_run_keeps_child_failure_with_capture_problems(tmp_path):
    paths = create_run(tmp_path, "demo", run_id="capture-problem-failed")
    payload = read_json(paths.status)
    payload.update({"status": "failed", "error": "Pipeline failed at step 0", "completed_steps": []})
    write_json_atomic(paths.status, payload)

    from cdt.cli import _finalize_captured_run

    exit_code = _finalize_captured_run(
        {}, paths, "demo", None, 1, None, capture_problems=("capture log write failed; saved copy stopped",)
    )

    assert exit_code == 1
    saved = read_json(paths.status)
    # The child's own failure and message stay authoritative; no fake rewrite.
    assert saved["status"] == "failed"
    assert saved["error"] == "Pipeline failed at step 0"


def test_finalize_captured_run_preserves_progress_and_marks_failure(tmp_path):
    paths = create_run(tmp_path, "demo", run_id="finalize-run")
    payload = read_json(paths.status)
    payload.update(
        {
            "status": "running",
            "completed_steps": ["0"],
            "artifacts": [{"name": "aab", "path": "build/app.aab"}],
        }
    )
    write_json_atomic(paths.status, payload)

    from cdt.cli import _finalize_captured_run

    exit_code = _finalize_captured_run({}, paths, "demo", None, 1, None)

    assert exit_code == 1
    saved = read_json(paths.status)
    assert saved["status"] == "failed"
    assert "exited with code 1" in saved["error"]
    assert saved["completed_steps"] == ["0"]
    assert saved["artifacts"] == [{"name": "aab", "path": "build/app.aab"}]
    assert paths.exit.read_text(encoding="utf-8").strip() == "1"


def test_capture_output_restores_terminal_signal_handlers(tmp_path, monkeypatch):
    _capture_project(tmp_path, monkeypatch)
    before = (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM))

    result = runner.invoke(app, ["run", "demo", "--capture-output"])
    after = (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM))

    assert result.exit_code == 0, result.output
    assert before == after


def _make_finished_run(
    root: Path,
    pipeline: str,
    *,
    age_days: float | None = None,
    status: str = "success",
    exit_code: int | None = 0,
) -> RunPaths:
    """Create a finished-looking run record with a controlled start time."""
    paths = create_run(root, pipeline)
    if age_days is not None:
        started = datetime.now(timezone.utc) - timedelta(days=age_days)
        manifest = read_json(paths.manifest)
        manifest["started_at"] = started.isoformat()
        write_json_atomic(paths.manifest, manifest)
        payload = read_json(paths.status)
        payload["status"] = status
        payload["started_at"] = started.isoformat()
        payload["finished_at"] = started.isoformat()
        write_json_atomic(paths.status, payload)
    if exit_code is not None:
        write_exit_code(paths.exit, exit_code)
    return paths


def test_history_group_keeps_existing_list_behavior(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    old = _make_finished_run(tmp_path, "test", age_days=5, status="failed", exit_code=1)
    fresh = _make_finished_run(tmp_path, "test", age_days=1)
    other = _make_finished_run(tmp_path, "other", age_days=2)

    listed = runner.invoke(app, ["history"])
    assert listed.exit_code == 0
    assert old.run_id in listed.stdout
    assert other.run_id in listed.stdout

    json_runs = runner.invoke(app, ["history", "--json"])
    payload = json.loads(json_runs.stdout)
    assert json_runs.exit_code == 0
    assert payload["schema_version"] == 1
    assert payload["filters"] == {"pipeline": None, "status": None, "limit": 20}
    assert {item["run_id"] for item in payload["runs"]} == {old.run_id, fresh.run_id, other.run_id}

    filtered = runner.invoke(
        app,
        ["history", "--pipeline", "test", "--status", "failed", "--limit", "1", "--json"],
    )
    filtered_payload = json.loads(filtered.stdout)
    assert filtered.exit_code == 0
    assert [item["run_id"] for item in filtered_payload["runs"]] == [old.run_id]

    prune_help = runner.invoke(app, ["history", "prune", "--help"])
    assert prune_help.exit_code == 0
    visible = _visible_text(prune_help.output)
    for option in ("--older-than", "--keep-last", "--pipeline", "--dry-run", "--confirm", "--json"):
        assert option in visible


def test_prune_rejects_invalid_arguments_without_deleting(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    paths = _make_finished_run(tmp_path, "test", age_days=60)

    cases = [
        ["history", "prune"],
        ["history", "prune", "--dry-run", "--confirm"],
        ["history", "prune", "--older-than", "abc"],
        ["history", "prune", "--older-than", "0d"],
        ["history", "prune", "--keep-last", "-1"],
    ]
    for arguments in cases:
        result = runner.invoke(app, arguments)
        assert result.exit_code == 2, arguments
        assert paths.root.exists(), arguments


def test_prune_preview_changes_nothing_and_confirm_deletes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    old = _make_finished_run(tmp_path, "test", age_days=60)
    fresh = _make_finished_run(tmp_path, "test", age_days=1)

    preview = runner.invoke(app, ["history", "prune", "--older-than", "30d"])
    assert preview.exit_code == 0
    assert old.run_id in preview.stdout
    assert fresh.run_id not in preview.stdout
    assert "nothing was deleted" in preview.stdout
    assert "resume" in preview.stdout
    assert old.root.exists()
    assert fresh.root.exists()

    dry_run = runner.invoke(app, ["history", "prune", "--older-than", "30d", "--dry-run", "--json"])
    dry_payload = json.loads(dry_run.stdout)
    assert dry_run.exit_code == 0
    assert dry_payload["mode"] == "preview"
    assert [record["run_id"] for record in dry_payload["candidates"]] == [old.run_id]
    assert dry_payload["deleted"] == []
    assert dry_payload["freed_bytes"] == 0
    assert dry_run.stderr == ""
    assert old.root.exists()

    confirmed = runner.invoke(app, ["history", "prune", "--older-than", "30d", "--confirm", "--json"])
    confirmed_payload = json.loads(confirmed.stdout)
    assert confirmed.exit_code == 0
    assert confirmed_payload["mode"] == "delete"
    assert [record["run_id"] for record in confirmed_payload["deleted"]] == [old.run_id]
    assert confirmed_payload["freed_bytes"] > 0
    assert confirmed_payload["total_size_bytes"] > 0
    assert not old.root.exists()
    assert fresh.root.exists()


def test_prune_age_and_keep_last_are_independent_policies(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    old = _make_finished_run(tmp_path, "test", age_days=60)
    fresh = _make_finished_run(tmp_path, "test", age_days=1)
    other_old = _make_finished_run(tmp_path, "other", age_days=60)
    other_fresh = _make_finished_run(tmp_path, "other", age_days=2)

    by_age = runner.invoke(app, ["history", "prune", "--older-than", "30d", "--json"])
    by_age_payload = json.loads(by_age.stdout)
    assert by_age.exit_code == 0
    assert {record["run_id"] for record in by_age_payload["candidates"]} == {old.run_id, other_old.run_id}

    by_count = runner.invoke(app, ["history", "prune", "--keep-last", "1", "--confirm", "--json"])
    by_count_payload = json.loads(by_count.stdout)
    assert by_count.exit_code == 0
    assert {record["run_id"] for record in by_count_payload["deleted"]} == {old.run_id, other_old.run_id}
    assert fresh.root.exists()
    assert other_fresh.root.exists()

    combined = runner.invoke(
        app,
        ["history", "prune", "--older-than", "30d", "--keep-last", "1", "--pipeline", "test", "--json"],
    )
    combined_payload = json.loads(combined.stdout)
    assert combined.exit_code == 0
    assert combined_payload["filters"] == {"older_than_days": 30, "keep_last": 1, "pipeline": "test"}
    assert combined_payload["candidates"] == []
    assert fresh.root.exists()


def test_prune_json_and_text_report_skips_without_error_exit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    active = create_run(tmp_path, "test")
    finished = _make_finished_run(tmp_path, "test", age_days=60)

    result = runner.invoke(app, ["history", "prune", "--older-than", "30d", "--json"])
    payload = json.loads(result.stdout)
    assert result.exit_code == 0
    assert [record["run_id"] for record in payload["candidates"]] == [finished.run_id]
    assert {skip["run_id"] for skip in payload["skipped"]} == {active.run_id}
    assert all(skip["reason"] for skip in payload["skipped"])
    assert payload["errors"] == []
    assert active.root.exists()

    text = runner.invoke(app, ["history", "prune", "--older-than", "30d"])
    assert text.exit_code == 0
    assert active.run_id in text.stdout
    assert "Skipped records (1)" in text.stdout


def test_prune_reports_operation_errors_with_exit_code_1(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runs_dir = tmp_path / ".cdt" / "runs"
    runs_dir.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    runs_dir.rmdir()
    runs_dir.symlink_to(outside, target_is_directory=True)

    json_result = runner.invoke(app, ["history", "prune", "--keep-last", "1", "--json"])
    payload = json.loads(json_result.stdout)
    assert json_result.exit_code == 1
    assert payload["mode"] == "preview"
    assert len(payload["errors"]) == 1
    assert "symbolic link" in payload["errors"][0]["error"]

    text_result = runner.invoke(app, ["history", "prune", "--keep-last", "1"])
    assert text_result.exit_code == 1
    assert "Error:" in text_result.stderr


def test_prune_text_preview_lists_record_details_and_totals(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    old = _make_finished_run(tmp_path, "test", age_days=60, status="failed", exit_code=1)
    (old.root / "log").write_text("x" * 2048, encoding="utf-8")

    result = runner.invoke(app, ["history", "prune", "--older-than", "30d"])
    assert result.exit_code == 0
    assert old.run_id in result.stdout
    assert "test" in result.stdout
    assert "failed" in result.stdout
    assert "60.0d" in result.stdout
    assert "KiB" in result.stdout
    assert "would be deleted" in result.stdout
    assert "including logs and data needed to resume" in result.stdout
