import json
import re
import sys
from pathlib import Path

from typer.testing import CliRunner

from cdt.cli import app
from cdt.pipeline.registry import _clear_steps_for_tests
from cdt.runs import list_runs, read_json, run_paths

runner = CliRunner()
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _compact_visible_text(output: str) -> str:
    visible = ANSI_RE.sub("", output).translate({ord(ch): None for ch in "│╭─╮╰╯"})
    return re.sub(r"\s+", "", visible)


def setup_function():
    _clear_steps_for_tests()
    sys.modules.pop("cdt_steps.resume", None)
    sys.modules.pop("cdt_steps", None)


def teardown_function():
    _clear_steps_for_tests()
    sys.modules.pop("cdt_steps.resume", None)
    sys.modules.pop("cdt_steps", None)


def _write_project(tmp_path, steps_yaml: str) -> None:
    package = tmp_path / "cdt_steps"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "resume.py").write_text(
        "\n".join(
            [
                "from cdt.sdk import step",
                "",
                "@step('demo.touch')",
                "def touch(ctx, output: str):",
                "    path = ctx.cwd / output",
                "    path.write_text('ran', encoding='utf-8')",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "cdt.yaml").write_text(
        "version: 1\nplugins:\n  - cdt_steps.resume\npipelines:\n  demo:\n    steps:\n" + steps_yaml,
        encoding="utf-8",
    )


def test_skip_completed_distinguishes_duplicate_anonymous_parallel_groups(tmp_path, monkeypatch):
    _write_project(
        tmp_path,
        "\n".join(
            [
                "      - parallel:",
                "          steps:",
                "            - demo.touch: {output: first-ios.txt}",
                "            - demo.touch: {output: first-android.txt}",
                "      - parallel:",
                "          steps:",
                "            - demo.touch: {output: second-ios.txt}",
                "            - demo.touch: {output: second-android.txt}",
            ]
        )
        + "\n",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    resume_status = tmp_path / "resume.json"
    output_status = tmp_path / "out.json"
    resume_status.write_text(json.dumps({"completed_steps": ["0", "0/0", "0/1"], "artifacts": []}), encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "run",
            "demo",
            "--resume-status-file",
            str(resume_status),
            "--status-file",
            str(output_status),
            "--skip-completed",
        ],
    )

    assert result.exit_code == 0, result.output
    assert not (tmp_path / "first-ios.txt").exists()
    assert not (tmp_path / "first-android.txt").exists()
    assert (tmp_path / "second-ios.txt").exists()
    assert (tmp_path / "second-android.txt").exists()


def test_resume_from_parallel_name_fails_when_ambiguous(tmp_path, monkeypatch):
    _write_project(
        tmp_path,
        "\n".join(
            [
                "      - parallel:",
                "          steps:",
                "            - demo.touch: {output: first.txt}",
                "      - parallel:",
                "          steps:",
                "            - demo.touch: {output: second.txt}",
            ]
        )
        + "\n",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    status = tmp_path / "status.json"
    status.write_text(json.dumps({"completed_steps": [], "artifacts": []}), encoding="utf-8")

    result = runner.invoke(app, ["run", "demo", "--resume-status-file", str(status), "--resume-from", "parallel"])

    assert result.exit_code != 0
    assert "Ambiguous resume step: parallel matches step ids 0, 1" in result.output


def test_resume_from_top_level_step_id_works(tmp_path, monkeypatch):
    _write_project(
        tmp_path,
        "\n".join(
            [
                "      - demo.touch: {output: first.txt}",
                "      - demo.touch: {output: second.txt}",
                "      - demo.touch: {output: third.txt}",
            ]
        )
        + "\n",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    status = tmp_path / "status.json"
    status.write_text(json.dumps({"completed_steps": [], "artifacts": []}), encoding="utf-8")

    result = runner.invoke(app, ["run", "demo", "--resume-status-file", str(status), "--resume-from", "2"])

    assert result.exit_code == 0, result.output
    assert not (tmp_path / "first.txt").exists()
    assert not (tmp_path / "second.txt").exists()
    assert (tmp_path / "third.txt").exists()


def test_resume_from_parallel_child_step_id_runs_only_selected_branch(tmp_path, monkeypatch):
    _write_project(
        tmp_path,
        "\n".join(
            [
                "      - parallel:",
                "          steps:",
                "            - demo.touch: {output: skipped.txt}",
                "            - demo.touch: {output: selected.txt}",
            ]
        )
        + "\n",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    status = tmp_path / "status.json"
    status.write_text(json.dumps({"completed_steps": [], "artifacts": []}), encoding="utf-8")

    result = runner.invoke(
        app,
        ["run", "demo", "--resume-status-file", str(status), "--resume-from", "0/1"],
    )

    assert result.exit_code == 0, result.output
    assert not (tmp_path / "skipped.txt").exists()
    assert (tmp_path / "selected.txt").exists()


def test_resume_from_nested_sequence_step_skips_prior_step_and_sibling_branch(tmp_path, monkeypatch):
    _write_project(
        tmp_path,
        "\n".join(
            [
                "      - parallel:",
                "          steps:",
                "            - demo.touch: {output: ios.txt}",
                "            - sequence:",
                "                steps:",
                "                  - demo.touch: {output: aab.txt}",
                "                  - demo.touch: {output: apk.txt}",
            ]
        )
        + "\n",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    status = tmp_path / "status.json"
    status.write_text(json.dumps({"completed_steps": [], "artifacts": []}), encoding="utf-8")

    result = runner.invoke(
        app,
        ["run", "demo", "--resume-status-file", str(status), "--resume-from", "0/1/1"],
    )

    assert result.exit_code == 0, result.output
    assert not (tmp_path / "ios.txt").exists()
    assert not (tmp_path / "aab.txt").exists()
    assert (tmp_path / "apk.txt").exists()


def test_resume_requires_resume_status_file_even_with_status_file(tmp_path, monkeypatch):
    _write_project(tmp_path, "      - demo.touch: {output: ran.txt}\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))

    result = runner.invoke(app, ["run", "demo", "--status-file", str(tmp_path / "out.json"), "--skip-completed"])

    assert result.exit_code != 0
    assert not (tmp_path / "ran.txt").exists()


def test_status_file_is_output_only_when_resuming(tmp_path, monkeypatch):
    _write_project(
        tmp_path,
        "\n".join(
            [
                "      - demo.touch: {output: skipped.txt}",
                "      - demo.touch: {output: ran.txt}",
            ]
        )
        + "\n",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    resume_status = tmp_path / "input.json"
    output_status = tmp_path / "nested" / "output.json"
    resume_status.write_text(json.dumps({"completed_steps": ["0"], "artifacts": []}), encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "run",
            "demo",
            "--resume-status-file",
            str(resume_status),
            "--status-file",
            str(output_status),
            "--skip-completed",
        ],
    )

    assert result.exit_code == 0, result.output
    assert not (tmp_path / "skipped.txt").exists()
    assert (tmp_path / "ran.txt").exists()
    assert json.loads(output_status.read_text(encoding="utf-8"))["status"] == "success"


def _write_input_project(tmp_path: Path) -> None:
    package = tmp_path / "cdt_steps"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "resume.py").write_text(
        "\n".join(
            [
                "from cdt.sdk import step",
                "",
                "@step('demo.touch')",
                "def touch(ctx, output: str):",
                "    path = ctx.cwd / output",
                "    path.write_text('ran', encoding='utf-8')",
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
                "  - cdt_steps.resume",
                "pipelines:",
                "  demo:",
                "    inputs:",
                "      version:",
                "        required: true",
                "    steps:",
                "      - demo.touch: {output: ran.txt}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def test_resume_accepts_matching_inputs(tmp_path, monkeypatch):
    _write_input_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    status = tmp_path / "status.json"
    status.write_text(
        json.dumps({"completed_steps": [], "inputs": {"version": "0.5.2"}, "artifacts": []}),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "run",
            "demo",
            "--input",
            "version=0.5.2",
            "--resume-status-file",
            str(status),
            "--skip-completed",
        ],
    )

    assert result.exit_code == 0, result.output
    assert (tmp_path / "ran.txt").exists()


def test_resume_rejects_continuation_with_different_version(tmp_path, monkeypatch):
    _write_input_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    status = tmp_path / "status.json"
    status.write_text(
        json.dumps({"completed_steps": [], "inputs": {"version": "0.5.2"}, "artifacts": []}),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "run",
            "demo",
            "--input",
            "version=0.5.3",
            "--resume-status-file",
            str(status),
            "--skip-completed",
        ],
    )

    assert result.exit_code != 0
    normalized = _compact_visible_text(result.output)
    assert "Resumeinputsdonotmatchtheoriginalrun" in normalized
    assert "Areleasecannotbecontinuedwithdifferentinputs" in normalized
    assert not (tmp_path / "ran.txt").exists()


def test_resume_rejects_inputs_when_original_run_had_none(tmp_path, monkeypatch):
    _write_input_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    status = tmp_path / "status.json"
    status.write_text(json.dumps({"completed_steps": [], "artifacts": []}), encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "run",
            "demo",
            "--input",
            "version=0.5.2",
            "--resume-status-file",
            str(status),
            "--skip-completed",
        ],
    )

    assert result.exit_code != 0
    assert "Resume inputs do not match the original run" in result.output
    assert not (tmp_path / "ran.txt").exists()


def test_old_name_based_resume_status_is_rejected(tmp_path, monkeypatch):
    _write_project(tmp_path, "      - demo.touch: {output: ran.txt}\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    status = tmp_path / "status.json"
    status.write_text(json.dumps({"completed_steps": ["demo.touch"], "artifacts": []}), encoding="utf-8")

    result = runner.invoke(app, ["run", "demo", "--resume-status-file", str(status), "--skip-completed"])

    assert result.exit_code != 0
    assert "Resume status file uses step names from an older CDT version" in result.output


def _write_release_project(tmp_path) -> None:
    package = tmp_path / "cdt_steps"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "resume.py").write_text(
        "\n".join(
            [
                "import typer",
                "from cdt.sdk import step",
                "",
                "@step('demo.guard')",
                "def guard(ctx):",
                "    if (ctx.cwd / 'fail-marker').exists():",
                "        raise typer.BadParameter('boom')",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "0.5.1"\n', encoding="utf-8")
    init_dir = tmp_path / "cdt"
    init_dir.mkdir(exist_ok=True)
    (init_dir / "__init__.py").write_text('__version__ = "0.5.1"\n', encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## Unreleased\n\n- Real change.\n\n## v0.5.1 - 2026-09-01\n\n- Old.\n",
        encoding="utf-8",
    )
    (tmp_path / "cdt.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "plugins:",
                "  - cdt_steps.resume",
                "pipelines:",
                "  release:",
                "    inputs:",
                "      version:",
                "        required: true",
                "    steps:",
                '      - python.prepare_release: {version: "${inputs.version}"}',
                "      - demo.guard",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def test_rolled_back_release_reruns_fresh_with_same_inputs(tmp_path, monkeypatch):
    _write_release_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    (tmp_path / "fail-marker").write_text("", encoding="utf-8")

    failed = runner.invoke(app, ["run", "release", "--input", "version=0.5.2"])
    runs = list_runs(tmp_path)
    failed_status = read_json(run_paths(tmp_path, runs[0]["run_id"]).status)

    assert failed.exit_code != 0
    assert failed_status["rolled_back"] is True
    assert 'version = "0.5.1"' in (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    assert "## v0.5.2" not in (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8")

    (tmp_path / "fail-marker").unlink()

    rejected = runner.invoke(
        app,
        [
            "run",
            "release",
            "--input",
            "version=0.5.3",
            "--resume-status-file",
            str(run_paths(tmp_path, runs[0]["run_id"]).status),
            "--status-file",
            str(tmp_path / "out.json"),
            "--skip-completed",
        ],
    )

    assert rejected.exit_code != 0
    normalized = _compact_visible_text(rejected.output)
    assert "Resumeinputsdonotmatchtheoriginalrun" in normalized

    fresh = runner.invoke(app, ["run", "release", "--input", "version=0.5.2"])

    assert fresh.exit_code == 0, fresh.output
    assert 'version = "0.5.2"' in (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    assert "## v0.5.2 - " in (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8")
