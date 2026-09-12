import sys
from pathlib import Path

import pytest
import typer

from cdt.artifacts import ArtifactKind
from cdt.pipeline import PipelineContext
from cdt.runner import CommandExecutionError
from cdt.steps.python import BuildDistributionStep, PrepareReleaseStep, PytestStep, RuffCheckStep


class FakeRunner:
    """Records commands; exit_codes maps a command token (e.g. 'twine') to a failing exit code."""

    def __init__(self, exit_codes: dict[str, int] | None = None, on_run=None):
        self.commands: list[list[str]] = []
        self.exit_codes = exit_codes or {}
        self.on_run = on_run

    def run(self, command: list[str], *, cwd: Path) -> int:
        self.commands.append(list(command))
        if self.on_run is not None:
            self.on_run(list(command))
        for token, code in self.exit_codes.items():
            if token in command:
                return code
        return 0


def make_ctx(tmp_path: Path, runner=None, run_dir: Path | None = None) -> PipelineContext:
    return PipelineContext(cwd=tmp_path, env={}, runner=runner or FakeRunner(), run_dir=run_dir)


def _write_release_project(tmp_path: Path, *, changelog: str, tag_reference: bool = True) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '\n'.join(
            [
                "[project]",
                'name = "demo"',
                'version = "0.5.1"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    package = tmp_path / "cdt"
    package.mkdir(exist_ok=True)
    (package / "__init__.py").write_text('__version__ = "0.5.1"\n', encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    if tag_reference:
        (tmp_path / "README.md").write_text(
            "pip install git+https://github.com/example/cdt.git@v0.5.1\n", encoding="utf-8"
        )


UNRELEASED = "\n".join(
    [
        "# Changelog",
        "",
        "## Unreleased",
        "",
        "- Added the thing.",
        "- TODO: summarize later.",
        "",
        "## v0.5.1 - 2026-09-01",
        "",
        "- Previous release.",
        "",
    ]
)


def test_ruff_check_runs_approved_command(tmp_path):
    runner = FakeRunner()

    RuffCheckStep().run(make_ctx(tmp_path, runner))

    assert runner.commands == [["ruff", "check", "."]]


def test_ruff_check_failure_keeps_command_and_exit_code(tmp_path):
    runner = FakeRunner(exit_codes={"ruff": 3})

    with pytest.raises(CommandExecutionError) as exc_info:
        RuffCheckStep().run(make_ctx(tmp_path, runner))

    assert exc_info.value.command == ["ruff", "check", "."]
    assert exc_info.value.exit_code == 3


def test_pytest_runs_approved_command(tmp_path):
    runner = FakeRunner()

    PytestStep().run(make_ctx(tmp_path, runner))

    assert runner.commands == [["pytest", "-q"]]


def test_pytest_failure_keeps_command_and_exit_code(tmp_path):
    runner = FakeRunner(exit_codes={"pytest": 2})

    with pytest.raises(CommandExecutionError) as exc_info:
        PytestStep().run(make_ctx(tmp_path, runner))

    assert exc_info.value.command == ["pytest", "-q"]
    assert exc_info.value.exit_code == 2


def test_prepare_release_updates_versions_changelog_and_tag_references(tmp_path, monkeypatch):
    _write_release_project(tmp_path, changelog=UNRELEASED)
    monkeypatch.setattr("cdt.steps.python.datetime", _FrozenDateTime)
    ctx = make_ctx(tmp_path)

    PrepareReleaseStep(
        version="0.5.2",
        tag_reference_files=["README.md"],
        tag_reference_regex=r"(git\+https://github\.com/example/cdt\.git@)v\d+\.\d+\.\d+",
    ).run(ctx)

    assert 'version = "0.5.2"' in (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    assert '__version__ = "0.5.2"' in (tmp_path / "cdt" / "__init__.py").read_text(encoding="utf-8")
    changelog = (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## Unreleased\n\n- Nothing yet.\n" in changelog
    assert "## v0.5.2 - 2026-09-12\n\n- Added the thing." in changelog
    assert "TODO: summarize later." not in changelog
    assert "- Previous release." in changelog
    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert "git+https://github.com/example/cdt.git@v0.5.2" in readme
    assert ctx.old_version == "0.5.1"
    assert ctx.new_version == "0.5.2"
    assert ctx.values["release_version"] == "0.5.2"
    assert ctx.values["release_tag"] == "v0.5.2"
    assert ctx.rollback_pending


def test_prepare_release_snapshots_files_in_run_directory(tmp_path):
    _write_release_project(tmp_path, changelog=UNRELEASED, tag_reference=False)
    run_dir = tmp_path / ".cdt" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    ctx = make_ctx(tmp_path, run_dir=run_dir)

    PrepareReleaseStep(version="0.5.2").run(ctx)

    snapshots = list((run_dir / "snapshots").iterdir())
    assert len(snapshots) == 3
    restored = "\n".join(path.read_text(encoding="utf-8") for path in snapshots)
    assert '__version__ = "0.5.1"' in restored
    assert "# Changelog" in restored


def test_prepare_release_validation_errors_do_not_modify_files(tmp_path):
    _write_release_project(tmp_path, changelog=UNRELEASED)
    before = {
        path.name: path.read_text(encoding="utf-8")
        for path in [tmp_path / "pyproject.toml", tmp_path / "cdt" / "__init__.py", tmp_path / "CHANGELOG.md"]
    }
    ctx = make_ctx(tmp_path)
    step = PrepareReleaseStep(
        version="0.5.2",
        tag_reference_files=["README.md"],
    )

    with pytest.raises(typer.BadParameter, match="tag_reference_regex is required"):
        step.run(ctx)

    after = {
        path.name: path.read_text(encoding="utf-8")
        for path in [tmp_path / "pyproject.toml", tmp_path / "cdt" / "__init__.py", tmp_path / "CHANGELOG.md"]
    }
    assert before == after
    assert not ctx.rollback_pending


def test_prepare_release_rejects_non_semver_version(tmp_path):
    _write_release_project(tmp_path, changelog=UNRELEASED, tag_reference=False)

    with pytest.raises(typer.BadParameter, match="not an explicit semver"):
        PrepareReleaseStep(version="latest").run(make_ctx(tmp_path))


def test_prepare_release_rejects_missing_release_file(tmp_path):
    _write_release_project(tmp_path, changelog=UNRELEASED, tag_reference=False)
    (tmp_path / "cdt" / "__init__.py").unlink()

    with pytest.raises(typer.BadParameter, match="Release file not found"):
        PrepareReleaseStep(version="0.5.2").run(make_ctx(tmp_path))


def test_prepare_release_rejects_ambiguous_version_occurrences(tmp_path):
    _write_release_project(tmp_path, changelog=UNRELEASED, tag_reference=False)
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(pyproject.read_text(encoding="utf-8") + '[tool.demo]\nversion = "9.9.9"\n', encoding="utf-8")

    with pytest.raises(typer.BadParameter, match="Expected exactly one version assignment"):
        PrepareReleaseStep(version="0.5.2").run(make_ctx(tmp_path))


def test_prepare_release_rejects_missing_version_assignment(tmp_path):
    _write_release_project(tmp_path, changelog=UNRELEASED, tag_reference=False)
    (tmp_path / "cdt" / "__init__.py").write_text("OTHER = 1\n", encoding="utf-8")

    with pytest.raises(typer.BadParameter, match="Expected exactly one __version__ assignment"):
        PrepareReleaseStep(version="0.5.2").run(make_ctx(tmp_path))


@pytest.mark.parametrize(
    "changelog",
    [
        "# Changelog\n\n## v0.5.1 - 2026-09-01\n\n- No unreleased section.\n",
        "# Changelog\n\n## Unreleased\n\n- One.\n\n## Unreleased\n\n- Two.\n\n## v0.5.1 - 2026-09-01\n",
        "# Changelog\n\n## Unreleased\n\n- Nothing yet.\n\n## v0.5.1 - 2026-09-01\n",
        "# Changelog\n\n## Unreleased\n\n- TODO: only placeholders.\n\n## v0.5.1 - 2026-09-01\n",
    ],
)
def test_prepare_release_rejects_invalid_unreleased_sections(tmp_path, changelog):
    _write_release_project(tmp_path, changelog=changelog, tag_reference=False)

    with pytest.raises(typer.BadParameter):
        PrepareReleaseStep(version="0.5.2").run(make_ctx(tmp_path))


def test_prepare_release_requires_exactly_one_tag_reference(tmp_path):
    _write_release_project(tmp_path, changelog=UNRELEASED)
    (tmp_path / "README.md").write_text(
        "install git+https://github.com/example/cdt.git@v0.5.1\n"
        "or git+https://github.com/example/cdt.git@v0.4.0\n",
        encoding="utf-8",
    )

    with pytest.raises(typer.BadParameter, match="Expected exactly one tag reference"):
        PrepareReleaseStep(
            version="0.5.2",
            tag_reference_files=["README.md"],
            tag_reference_regex=r"(git\+https://github\.com/example/cdt\.git@)v\d+\.\d+\.\d+",
        ).run(make_ctx(tmp_path))


def test_prepare_release_rejects_regex_without_capture_group(tmp_path):
    _write_release_project(tmp_path, changelog=UNRELEASED)

    with pytest.raises(typer.BadParameter, match="exactly one capture group"):
        PrepareReleaseStep(
            version="0.5.2",
            tag_reference_files=["README.md"],
            tag_reference_regex=r"git\+https://github\.com/example/cdt\.git@v\d+\.\d+\.\d+",
        ).run(make_ctx(tmp_path))


def _make_dist(tmp_path: Path, wheel: bool = True, sdist: bool = True) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir(exist_ok=True)
    if wheel:
        (dist / "demo-0.5.2-py3-none-any.whl").write_text("wheel", encoding="utf-8")
    if sdist:
        (dist / "demo-0.5.2.tar.gz").write_text("sdist", encoding="utf-8")
    return dist


def test_build_distribution_builds_checks_and_registers_artifacts(tmp_path):
    runner = FakeRunner(on_run=lambda command: _make_dist(tmp_path) if "build" in command else None)
    ctx = make_ctx(tmp_path, runner)
    stale = tmp_path / "dist"
    stale.mkdir()
    (stale / "stale.txt").write_text("old", encoding="utf-8")

    BuildDistributionStep().run(ctx)

    assert ctx.artifacts["wheel"].kind is ArtifactKind.WHEEL
    assert ctx.artifacts["wheel"].path == tmp_path / "dist" / "demo-0.5.2-py3-none-any.whl"
    assert ctx.artifacts["sdist"].kind is ArtifactKind.SDIST
    assert ctx.artifacts["sdist"].path == tmp_path / "dist" / "demo-0.5.2.tar.gz"
    assert not (tmp_path / "dist" / "stale.txt").exists()


def test_build_distribution_uses_current_python_and_checks_with_twine(tmp_path, monkeypatch):
    ctx = make_ctx(tmp_path, FakeRunner(on_run=lambda command: _make_dist(tmp_path) if "build" in command else None))
    monkeypatch.setattr(sys, "executable", "/current/python")

    BuildDistributionStep().run(ctx)

    runner = ctx.runner
    assert runner.commands == [
        ["/current/python", "-m", "build"],
        [
            "/current/python",
            "-m",
            "twine",
            "check",
            str(tmp_path / "dist" / "demo-0.5.2-py3-none-any.whl"),
            str(tmp_path / "dist" / "demo-0.5.2.tar.gz"),
        ],
    ]


def test_build_distribution_reports_build_failure_with_command_and_exit_code(tmp_path):
    runner = FakeRunner(exit_codes={"build": 5})
    ctx = make_ctx(tmp_path, runner)

    with pytest.raises(CommandExecutionError) as exc_info:
        BuildDistributionStep().run(ctx)

    assert exc_info.value.command == [sys.executable, "-m", "build"]
    assert exc_info.value.exit_code == 5
    assert ctx.artifacts == {}


@pytest.mark.parametrize(("wheel", "sdist"), [(False, True), (True, False), (False, False)])
def test_build_distribution_requires_one_wheel_and_one_sdist(tmp_path, wheel, sdist):
    ctx = make_ctx(
        tmp_path,
        FakeRunner(
            on_run=lambda command: _make_dist(tmp_path, wheel=wheel, sdist=sdist) if "build" in command else None
        ),
    )

    with pytest.raises(typer.BadParameter, match="Expected exactly one wheel and one sdist"):
        BuildDistributionStep().run(ctx)


def test_build_distribution_reports_twine_failure_with_command_and_exit_code(tmp_path):
    runner = FakeRunner(
        exit_codes={"twine": 1},
        on_run=lambda command: _make_dist(tmp_path) if "build" in command else None,
    )
    ctx = make_ctx(tmp_path, runner)

    with pytest.raises(CommandExecutionError) as exc_info:
        BuildDistributionStep().run(ctx)

    assert "twine" in exc_info.value.command
    assert exc_info.value.exit_code == 1


@pytest.mark.parametrize("dist_dir", ["/tmp/elsewhere", "../outside", "nested/../.."])
def test_build_distribution_rejects_dist_dir_outside_project(tmp_path, dist_dir):
    ctx = make_ctx(tmp_path)

    with pytest.raises(typer.BadParameter, match="relative path inside the project"):
        BuildDistributionStep(dist_dir=dist_dir).run(ctx)


class _FrozenDateTime:
    @staticmethod
    def now(timezone=None):  # noqa: ARG002 - signature parity with datetime.now
        return _FrozenDateTime()


    @staticmethod
    def date():
        return _FrozenDate()


class _FrozenDate:
    @staticmethod
    def isoformat():
        return "2026-09-12"
