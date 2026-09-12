"""Python toolchain and self-release preparation built-ins."""

from __future__ import annotations

import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import typer

from ..artifacts import ArtifactKind, BuildArtifact
from ..pipeline import PipelineContext
from ..runner import CommandExecutionError
from .release import resolve_release_version

PYPROJECT_VERSION_ASSIGNMENT_RE = re.compile(r'(?m)^version = "[^"]+"')
VERSION_FILE_ASSIGNMENT_RE = re.compile(r'(?m)^__version__ = "[^"]+"')
UNRELEASED_HEADING_RE = re.compile(r"(?m)^## Unreleased\s*$")
NEXT_SECTION_RE = re.compile(r"(?m)^## ")
NOTHING_YET_RE = re.compile(r"^-\s*Nothing yet\.?\s*$")


class RuffCheckStep:
    name = "python.ruff_check"

    def run(self, ctx: PipelineContext) -> None:
        command = ["ruff", "check", "."]
        typer.echo("==> Running ruff check .")
        code = ctx.runner.run(command, cwd=ctx.cwd)
        if code != 0:
            raise CommandExecutionError(
                "Ruff check failed; fix the lint errors before releasing", command=command, exit_code=code
            )


class PytestStep:
    name = "python.pytest"

    def run(self, ctx: PipelineContext) -> None:
        command = ["pytest", "-q"]
        typer.echo("==> Running pytest -q")
        code = ctx.runner.run(command, cwd=ctx.cwd)
        if code != 0:
            raise CommandExecutionError(
                "pytest failed; fix the failing tests before releasing", command=command, exit_code=code
            )


class PrepareReleaseStep:
    name = "python.prepare_release"

    def __init__(
        self,
        version: str | None = None,
        pyproject: str = "pyproject.toml",
        version_file: str = "cdt/__init__.py",
        changelog: str = "CHANGELOG.md",
        tag_reference_files: list[str] | None = None,
        tag_reference_regex: str | None = None,
    ):
        self.version = version
        self.pyproject = pyproject
        self.version_file = version_file
        self.changelog = changelog
        self.tag_reference_files = list(tag_reference_files or [])
        self.tag_reference_regex = tag_reference_regex

    def run(self, ctx: PipelineContext) -> None:
        version = resolve_release_version(self.version, ctx)
        tag = f"v{version}"
        date = datetime.now(timezone.utc).date().isoformat()

        pyproject_path = ctx.project_path(self.pyproject)
        version_file_path = ctx.project_path(self.version_file)
        changelog_path = ctx.project_path(self.changelog)
        for path in (pyproject_path, version_file_path, changelog_path):
            if not path.is_file():
                raise typer.BadParameter(f"Release file not found: {path}")

        pyproject_text = pyproject_path.read_text(encoding="utf-8")
        version_file_text = version_file_path.read_text(encoding="utf-8")
        changelog_text = changelog_path.read_text(encoding="utf-8")

        package_version = _single_version(pyproject_text, PYPROJECT_VERSION_ASSIGNMENT_RE, "pyproject.toml", "version")
        _single_version(version_file_text, VERSION_FILE_ASSIGNMENT_RE, self.version_file, "__version__")
        updated_changelog = _transition_changelog(changelog_text, tag, date)

        tag_regex = self._tag_reference_regex()
        tag_updates: dict[Path, str] = {}
        for raw_path in self.tag_reference_files:
            path = ctx.project_path(raw_path)
            if not path.is_file():
                raise typer.BadParameter(f"Tag reference file not found: {raw_path}")
            text = path.read_text(encoding="utf-8")
            matches = list(tag_regex.finditer(text))
            if len(matches) != 1:
                raise typer.BadParameter(
                    f"Expected exactly one tag reference in {raw_path}, found {len(matches)}; refusing to update it"
                )
            tag_updates[path] = tag_regex.sub(lambda match: match.group(1) + tag, text, count=1)

        # Every check passed: snapshot the exact current content, then write.
        mutable = [pyproject_path, version_file_path, changelog_path, *tag_updates.keys()]
        for path in mutable:
            ctx.register_rollback_file(path)

        pyproject_path.write_text(
            PYPROJECT_VERSION_ASSIGNMENT_RE.sub(f'version = "{version}"', pyproject_text, count=1), encoding="utf-8"
        )
        version_file_path.write_text(
            VERSION_FILE_ASSIGNMENT_RE.sub(f'__version__ = "{version}"', version_file_text, count=1), encoding="utf-8"
        )
        changelog_path.write_text(updated_changelog, encoding="utf-8")
        for path, text in tag_updates.items():
            path.write_text(text, encoding="utf-8")

        ctx.old_version = package_version
        ctx.new_version = version
        ctx.values["release_version"] = version
        ctx.values["release_tag"] = tag
        typer.echo(f"==> Prepared release files for {version} (was {package_version}); changelog section {tag} created")

    def _tag_reference_regex(self) -> re.Pattern:
        if not self.tag_reference_files:
            return re.compile(r"(?!)")  # never matches; unused
        if not self.tag_reference_regex:
            raise typer.BadParameter("tag_reference_regex is required when tag_reference_files are configured")
        try:
            compiled = re.compile(self.tag_reference_regex)
        except re.error as exc:
            raise typer.BadParameter(f"tag_reference_regex is not a valid regex: {exc}") from exc
        if compiled.groups != 1:
            raise typer.BadParameter("tag_reference_regex must contain exactly one capture group for the tag prefix")
        return compiled


def _single_version(text: str, pattern: re.Pattern, label: str, field: str) -> str:
    """Validate exactly one version occurrence and return the current value."""
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        raise typer.BadParameter(
            f"Expected exactly one {field} assignment in {label}, found {len(matches)}; refusing to modify {label}"
        )
    value = re.search(r'"([^"]+)"', matches[0].group(0))
    assert value is not None
    return value.group(1)


def _transition_changelog(changelog_text: str, tag: str, date: str) -> str:
    """Fold the real Unreleased entries into a dated release section; restore a fresh Unreleased."""
    entries = _unreleased_entries(changelog_text)
    match = UNRELEASED_HEADING_RE.search(changelog_text)
    assert match is not None  # guaranteed by _unreleased_entries
    rest = changelog_text[match.end() :]
    next_heading = NEXT_SECTION_RE.search(rest)
    section_end = match.end() + next_heading.start() if next_heading else len(changelog_text)
    section = "## Unreleased\n\n- Nothing yet.\n\n" f"## {tag} - {date}\n\n" + "\n".join(entries) + "\n\n"
    return changelog_text[: match.start()] + section + changelog_text[section_end:]


def _unreleased_entries(changelog_text: str) -> list[str]:
    headings = list(UNRELEASED_HEADING_RE.finditer(changelog_text))
    if len(headings) != 1:
        raise typer.BadParameter(f"Expected exactly one '## Unreleased' section, found {len(headings)}")
    rest = changelog_text[headings[0].end() :]
    next_heading = NEXT_SECTION_RE.search(rest)
    body = rest[: next_heading.start()] if next_heading else rest
    entries = [line for line in body.splitlines() if line.strip()]
    real = [line for line in entries if NOTHING_YET_RE.fullmatch(line.strip()) is None]
    movable = [line for line in real if "TODO" not in line]
    skipped = len(real) - len(movable)
    if not movable:
        detail = f"; {skipped} TODO placeholder(s) found" if skipped else ""
        raise typer.BadParameter("The '## Unreleased' section has no real entries to release" + detail)
    return movable


class BuildDistributionStep:
    name = "python.build_distribution"

    def __init__(
        self,
        dist_dir: str = "dist",
        python: str | None = None,
        wheel_artifact: str = "wheel",
        sdist_artifact: str = "sdist",
    ):
        self.dist_dir = dist_dir
        self.python = python
        self.wheel_artifact = wheel_artifact
        self.sdist_artifact = sdist_artifact

    def run(self, ctx: PipelineContext) -> None:
        dist_dir = self._resolve_dist_dir(ctx)
        if dist_dir.exists():
            shutil.rmtree(dist_dir)

        python = self.python or sys.executable or "python3"
        build_command = [python, "-m", "build"]
        typer.echo("==> Building wheel and sdist with python -m build")
        code = ctx.runner.run(build_command, cwd=ctx.cwd)
        if code != 0:
            raise CommandExecutionError(
                "Distribution build failed; check the build output above", command=build_command, exit_code=code
            )

        wheels = sorted(dist_dir.glob("*.whl"))
        sdists = sorted(dist_dir.glob("*.tar.gz"))
        if len(wheels) != 1 or len(sdists) != 1:
            raise typer.BadParameter(
                f"Expected exactly one wheel and one sdist in {dist_dir}; found "
                f"wheels: {[path.name for path in wheels]}, sdists: {[path.name for path in sdists]}"
            )
        wheel, sdist = wheels[0], sdists[0]

        twine_command = [python, "-m", "twine", "check", str(wheel), str(sdist)]
        code = ctx.runner.run(twine_command, cwd=ctx.cwd)
        if code != 0:
            raise CommandExecutionError(
                "twine check rejected the built distributions", command=twine_command, exit_code=code
            )

        ctx.register_artifact(self.wheel_artifact, BuildArtifact(ArtifactKind.WHEEL, wheel, wheel.name, step=self.name))
        ctx.register_artifact(self.sdist_artifact, BuildArtifact(ArtifactKind.SDIST, sdist, sdist.name, step=self.name))
        typer.echo(f"==> Built and verified distributions: {wheel.name}, {sdist.name}")

    def _resolve_dist_dir(self, ctx: PipelineContext) -> Path:
        raw = Path(self.dist_dir)
        if raw.is_absolute() or ".." in raw.parts:
            raise typer.BadParameter(
                f"build_distribution dist_dir must be a relative path inside the project: {self.dist_dir}"
            )
        return ctx.cwd / raw
