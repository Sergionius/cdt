"""Release preflight built-ins: verify that a version is safe to release."""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

import typer

from ..pipeline import PipelineContext
from .git import _capture

SEMVER_RE = re.compile(r"\d+\.\d+\.\d+(?:[.-][A-Za-z0-9]+)?")
PYPROJECT_NAME_RE = re.compile(r'(?m)^name = "([^"]+)"')
PYPROJECT_VERSION_RE = re.compile(r'(?m)^version = "([^"]+)"')
VERSION_FILE_RE = re.compile(r'(?m)^__version__ = "([^"]+)"')

PYPI_BASE_URL = "https://pypi.org/pypi"
PYPI_ATTEMPTS = 3
PYPI_BASE_DELAY_SECONDS = 0.5
PYPI_REQUEST_TIMEOUT_SEC = 10


def resolve_release_version(explicit: str | None, ctx: PipelineContext) -> str:
    """Return the explicitly passed release version (option or pipeline input), validated as semver."""
    version = (explicit or "").strip() or (ctx.inputs.get("version") or "").strip()
    if not version:
        raise typer.BadParameter(
            "This release step requires an explicit version. Configure 'version: ${inputs.version}' "
            "and pass --input version=X.Y.Z"
        )
    if SEMVER_RE.fullmatch(version) is None:
        raise typer.BadParameter(f"Release version {version!r} is not an explicit semver like 1.2.3")
    return version


def version_triple(version: str) -> tuple[int, int, int] | None:
    """Extract the leading X.Y.Z triple of a version string; None when absent."""
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", version)
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def read_pyproject_metadata(path: Path) -> tuple[str, str]:
    """Return the (name, version) declared by a pyproject.toml file."""
    text = path.read_text(encoding="utf-8")
    version_match = PYPROJECT_VERSION_RE.search(text)
    if version_match is None:
        raise typer.BadParameter(f"No 'version = ...' field found in {path}")
    name = PYPROJECT_NAME_RE.search(text)
    return (name.group(1) if name else "unknown"), version_match.group(1)


def github_repo_from_origin(ctx: PipelineContext, remote: str) -> str:
    """Derive the 'owner/repo' GitHub slug from the configured git remote URL."""
    code, url, _err = _capture(["git", "remote", "get-url", remote], cwd=ctx.cwd)
    if code != 0 or not url:
        raise typer.BadParameter(f"No git remote '{remote}' configured; cannot derive the GitHub repository")
    match = re.search(r"github\.com[/:]([^/]+)/([^/.]+?)(?:\.git)?/?$", url)
    if match is None:
        raise typer.BadParameter(f"Git remote '{remote}' is not a GitHub repository: {url}")
    return f"{match.group(1)}/{match.group(2)}"


def github_release_exists(repo: str, tag: str, *, cwd: Path) -> bool:
    """Check a GitHub Release through the installed gh CLI; fail closed on gh errors."""
    code, _out, err = _capture(["gh", "release", "view", tag, "--repo", repo, "--json", "tagName"], cwd=cwd)
    if code == 0:
        return True
    if "not found" in err.lower():
        return False
    raise typer.BadParameter(f"Could not check GitHub releases for {repo} via gh: {err or f'gh exited with {code}'}")


def pypi_release_exists(package: str, version: str) -> bool:
    """Check the public PyPI JSON API for an exact version, with bounded retries/backoff."""
    url = f"{PYPI_BASE_URL}/{package}/json"
    last_error = ""
    for attempt in range(1, PYPI_ATTEMPTS + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "cdt-release-preflight"})
            with urllib.request.urlopen(request, timeout=PYPI_REQUEST_TIMEOUT_SEC) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return version in payload.get("releases", {})
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return False
            last_error = f"HTTP {exc.code}"
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError, ValueError) as exc:
            last_error = str(exc) or type(exc).__name__
        if attempt < PYPI_ATTEMPTS:
            delay = PYPI_BASE_DELAY_SECONDS * (2 ** (attempt - 1))
            typer.echo(f"==> PyPI check failed ({last_error}); retrying in {delay:.1f}s", err=True)
            time.sleep(delay)
    raise typer.BadParameter(
        f"Could not verify PyPI availability for package '{package}' after {PYPI_ATTEMPTS} attempts: {last_error}"
    )


class RequireVersionAvailableStep:
    name = "release.require_version_available"

    def __init__(
        self,
        version: str | None = None,
        pyproject: str = "pyproject.toml",
        changelog: str = "CHANGELOG.md",
        tag_prefix: str = "v",
        github_repo: str | None = None,
        pypi_package: str | None = None,
        remote: str = "origin",
    ):
        self.version = version
        self.pyproject = pyproject
        self.changelog = changelog
        self.tag_prefix = tag_prefix
        self.github_repo = github_repo
        self.pypi_package = pypi_package
        self.remote = remote

    def run(self, ctx: PipelineContext) -> None:
        version = resolve_release_version(self.version, ctx)
        tag = f"{self.tag_prefix}{version}"

        pyproject_path = ctx.project_path(self.pyproject)
        if not pyproject_path.is_file():
            raise typer.BadParameter(f"pyproject.toml not found: {pyproject_path}")
        package_name, package_version = read_pyproject_metadata(pyproject_path)

        self._require_newer(version, package_version)
        self._require_absent_from_changelog(ctx, tag)
        self._require_absent_local_tag(ctx, tag)
        self._require_absent_remote_tag(ctx, tag)
        repo = self.github_repo or github_repo_from_origin(ctx, self.remote)
        if github_release_exists(repo, tag, cwd=ctx.cwd):
            raise typer.BadParameter(f"GitHub Release {tag} already exists in {repo}; choose a different version")
        package = self.pypi_package or package_name
        if pypi_release_exists(package, version):
            raise typer.BadParameter(f"Version {version} is already published on PyPI for package '{package}'")

        ctx.old_version = package_version
        ctx.new_version = version
        ctx.values["release_version"] = version
        ctx.values["release_tag"] = tag
        typer.echo(f"==> Version {version} is available (package '{package}' is at {package_version}; tag {tag} free)")

    def _require_newer(self, version: str, package_version: str) -> None:
        candidate = version_triple(version)
        current = version_triple(package_version)
        if candidate is None or current is None or candidate <= current:
            raise typer.BadParameter(
                f"Release version {version} must be strictly newer than the current package version {package_version}"
            )

    def _require_absent_from_changelog(self, ctx: PipelineContext, tag: str) -> None:
        changelog_path = ctx.project_path(self.changelog)
        if not changelog_path.is_file():
            raise typer.BadParameter(f"Changelog not found: {changelog_path}")
        text = changelog_path.read_text(encoding="utf-8")
        if re.search(rf"(?m)^## {re.escape(tag)}\b", text):
            raise typer.BadParameter(f"Changelog already contains a '## {tag}' section")

    def _require_absent_local_tag(self, ctx: PipelineContext, tag: str) -> None:
        code, out, _err = _capture(["git", "tag", "--list", tag], cwd=ctx.cwd)
        if code != 0:
            raise typer.BadParameter("Could not list local git tags")
        if out:
            raise typer.BadParameter(f"Local git tag {tag} already exists; moving existing tags is not allowed")

    def _require_absent_remote_tag(self, ctx: PipelineContext, tag: str) -> None:
        command = ["git", "ls-remote", "--tags", self.remote, f"refs/tags/{tag}"]
        code, out, _err = _capture(command, cwd=ctx.cwd)
        if code != 0:
            raise typer.BadParameter(f"Could not list tags on git remote '{self.remote}'")
        if out:
            raise typer.BadParameter(f"Tag {tag} already exists on remote '{self.remote}'; choose a different version")
