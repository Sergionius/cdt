import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from urllib.parse import urlparse

import typer

from . import __version__

_GITHUB_API_LATEST = "https://api.github.com/repos/{owner}/{repo}/releases/latest"
_SAFE_TAG_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class SelfUpdateError(Exception):
    """Raised when self-update cannot proceed."""


def _owner_repo_from_url(repo_url: str) -> tuple[str, str]:
    parsed = urlparse(repo_url)
    path = parsed.path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = path.split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise SelfUpdateError(f"Unable to parse owner/repo from repository URL: {repo_url}")
    return parts[0], parts[1]


def _latest_release_tag(owner: str, repo: str) -> str:
    url = _GITHUB_API_LATEST.format(owner=owner, repo=repo)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": f"cdt/{__version__}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise SelfUpdateError(f"GitHub API error: {exc.code} {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise SelfUpdateError(f"Network error while contacting GitHub: {exc.reason}") from exc
    except TimeoutError as exc:
        raise SelfUpdateError("GitHub API request timed out.") from exc
    except json.JSONDecodeError as exc:
        raise SelfUpdateError("Unable to parse GitHub API response.") from exc

    tag = data.get("tag_name")
    if not tag:
        raise SelfUpdateError("GitHub release does not contain a tag_name.")
    return tag


def _detect_install_method() -> str | None:
    executable = sys.executable
    lowered = executable.lower()
    if "pipx" in lowered:
        return "pipx"

    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "show", "cdt"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        if result.returncode == 0:
            location = None
            editable = False
            for line in result.stdout.splitlines():
                if line.startswith("Location:"):
                    location = line.split(":", 1)[1].strip()
                if line.startswith("Editable project location:"):
                    editable = True
            if editable:
                return None
            if location and "pipx" in location.lower():
                return "pipx"
            return "pip"
    except (OSError, subprocess.TimeoutExpired):
        pass

    try:
        result = subprocess.run(
            ["pipx", "list", "--json"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        if result.returncode == 0:
            payload = json.loads(result.stdout)
            venvs = payload.get("venvs", {})
            if any(name.lower() in {"cdt", "cdt-cli"} for name in venvs):
                return "pipx"
            for venv_data in venvs.values():
                try:
                    packages = venv_data.get("metadata", {}).get("main_package", {})
                except AttributeError:
                    continue
                if isinstance(packages, dict) and packages.get("package") == "cdt":
                    return "pipx"
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        pass

    return None


def _validate_tag(tag: str) -> None:
    if not tag or not _SAFE_TAG_RE.match(tag):
        raise SelfUpdateError(f"Unsafe or invalid release tag: {tag}")


def _update_command(tag: str, method: str, *, owner: str, repo: str) -> list[str]:
    _validate_tag(tag)
    package_url = f"git+https://github.com/{owner}/{repo}.git@{tag}"
    if method == "pipx":
        return ["pipx", "install", "--force", package_url]
    if method == "pip":
        return [sys.executable, "-m", "pip", "install", "--force-reinstall", package_url]
    raise SelfUpdateError(f"Unsupported install method: {method}")


def _normalized_version(tag_or_version: str) -> str:
    return tag_or_version.lstrip("v")


def _manual_update_command(tag: str, *, owner: str, repo: str) -> str:
    _validate_tag(tag)
    return f"pipx install --force git+https://github.com/{owner}/{repo}.git@{tag}"


def run_self_update(*, repo_url: str, dry_run: bool = False) -> int:
    owner, repo = _owner_repo_from_url(repo_url)
    latest_tag = _latest_release_tag(owner, repo)

    typer.echo(f"Current version: {__version__}")
    typer.echo(f"Latest release: {latest_tag}")

    if _normalized_version(latest_tag) == _normalized_version(__version__):
        typer.echo("Already up to date.")
        return 0

    method = _detect_install_method()
    if method is None:
        manual_command = _manual_update_command(latest_tag, owner=owner, repo=repo)
        if dry_run:
            typer.echo(
                "Unable to detect installation method. "
                f"Manual update command:\n  {manual_command}"
            )
            return 0
        typer.echo(
            "Unable to detect installation method. "
            "Please reinstall manually with:\n"
            f"  {manual_command}",
            err=True,
        )
        return 1

    command = _update_command(latest_tag, method, owner=owner, repo=repo)
    typer.echo(f"Update command: {' '.join(command)}")

    if dry_run:
        typer.echo("Dry run: not executing update command.")
        return 0

    typer.echo("Running update...")
    try:
        result = subprocess.run(command, check=False, timeout=300)
    except FileNotFoundError as exc:
        typer.echo(f"Update failed: command not found: {exc.filename}", err=True)
        return 1
    except OSError as exc:
        typer.echo(f"Update failed: {exc}", err=True)
        return 1
    except subprocess.TimeoutExpired:
        typer.echo("Update timed out.", err=True)
        return 1

    if result.returncode != 0:
        typer.echo("Update command failed.", err=True)
    return result.returncode
