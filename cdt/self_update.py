import json
import re
import shlex
import site
import subprocess
import sys
import textwrap
import urllib.error
import urllib.request
from urllib.parse import unquote, urlparse

import typer

from . import __version__

_GITHUB_API_LATEST = "https://api.github.com/repos/{owner}/{repo}/releases/latest"
_SAFE_TAG_RE = re.compile(r"^[A-Za-z0-9._+-]+$")


class SelfUpdateError(Exception):
    """Raised when self-update cannot proceed."""


def _is_pipx_path(path: str) -> bool:
    parts = path.replace("\\", "/").lower().split("/")
    for i, part in enumerate(parts[:-1]):
        if part == "pipx" and parts[i + 1] == "venvs":
            return True
    return False


def _owner_repo_from_url(repo_url: str) -> tuple[str, str]:
    parsed = urlparse(repo_url)
    if parsed.scheme != "https" or parsed.hostname != "github.com" or parsed.port not in (None, 443):
        raise SelfUpdateError(f"Unsupported repository URL (only https://github.com URLs are accepted): {repo_url}")
    path = unquote(parsed.path).strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = path.split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise SelfUpdateError(f"Unable to parse owner/repo from repository URL: {repo_url}")
    return parts[0], parts[1]


def _version_key(version: str) -> tuple[tuple[int, ...], int, tuple[tuple[int, int, str], ...]]:
    """Return a sortable key for a simple semver-like version string.

    A version without a pre-release segment is considered newer than the same
    release with a pre-release segment, e.g. ``0.4.0`` > ``0.4.0-dev``.
    Numeric pre-release identifiers are compared numerically.
    """
    version = version.lstrip("v")
    tokens = re.split(r"[.-]", version)
    release: list[int] = []
    pre: list[str] = []
    for token in tokens:
        if not token:
            continue
        if token.isdigit() and not pre:
            release.append(int(token))
        else:
            pre.append(token)

    def _pre_key(identifiers: list[str]) -> list[tuple[int, int, str]]:
        result: list[tuple[int, int, str]] = []
        for ident in identifiers:
            if ident.isdigit():
                result.append((0, int(ident), ""))
            else:
                result.append((1, 0, ident))
        return result

    # A release without a pre-release segment sorts after the same release
    # with a pre-release segment, so use a higher indicator for no pre-release.
    return tuple(release), (1 if not pre else 0), tuple(_pre_key(pre))


def _is_up_to_date(current: str, latest: str) -> bool:
    c_rel, c_pre_ind, c_pre = _version_key(current)
    l_rel, l_pre_ind, l_pre = _version_key(latest)
    length = max(len(c_rel), len(l_rel))
    c_rel = c_rel + (0,) * (length - len(c_rel))
    l_rel = l_rel + (0,) * (length - len(l_rel))
    return (c_rel, c_pre_ind, c_pre) >= (l_rel, l_pre_ind, l_pre)


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
    except UnicodeDecodeError as exc:
        raise SelfUpdateError("Unable to decode GitHub API response.") from exc
    except json.JSONDecodeError as exc:
        raise SelfUpdateError("Unable to parse GitHub API response.") from exc

    if not isinstance(data, dict):
        raise SelfUpdateError("Unexpected GitHub API response format.")

    tag = data.get("tag_name")
    if not isinstance(tag, str) or not tag.strip():
        raise SelfUpdateError("GitHub release does not contain a valid tag_name.")
    return tag.strip()


def _detect_install_method() -> tuple[str, bool] | None:
    executable = sys.executable
    if _is_pipx_path(executable):
        return "pipx", False

    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "show", "cdt"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
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
            if location:
                if _is_pipx_path(location):
                    return "pipx", False
                user_site = site.getusersitepackages()
                is_user = bool(user_site and location.startswith(user_site))
                return "pip", is_user
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError):
        pass

    try:
        result = subprocess.run(
            ["pipx", "list", "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=15,
        )
        if result.returncode == 0:
            payload = json.loads(result.stdout)
            if not isinstance(payload, dict):
                return None
            venvs = payload.get("venvs", {})
            if not isinstance(venvs, dict):
                return None
            if any(name.lower() in {"cdt", "cdt-cli"} for name in venvs):
                return "pipx", False
            for venv_data in venvs.values():
                if not isinstance(venv_data, dict):
                    continue
                metadata = venv_data.get("metadata", {})
                if not isinstance(metadata, dict):
                    continue
                main_package = metadata.get("main_package", {})
                if isinstance(main_package, dict) and main_package.get("package") == "cdt":
                    return "pipx", False
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, UnicodeDecodeError):
        pass

    return None


def _validate_tag(tag: str) -> None:
    if not isinstance(tag, str) or not tag.strip() or not _SAFE_TAG_RE.match(tag):
        raise SelfUpdateError(f"Unsafe or invalid release tag: {tag}")


def _update_command(tag: str, method: str, *, is_user: bool = False, owner: str, repo: str) -> list[str]:
    _validate_tag(tag)
    package_url = f"git+https://github.com/{owner}/{repo}.git@{tag}"
    if method == "pipx":
        return ["pipx", "install", "--force", package_url]
    if method == "pip":
        cmd = [sys.executable, "-m", "pip", "install", "--force-reinstall"]
        if is_user:
            cmd.append("--user")
        cmd.append(package_url)
        return cmd
    raise SelfUpdateError(f"Unsupported install method: {method}")


def _manual_update_command(tag: str, *, owner: str, repo: str) -> str:
    _validate_tag(tag)
    package_url = f"git+https://github.com/{owner}/{repo}.git@{tag}"
    quoted_url = shlex.quote(package_url)
    quoted_python = shlex.quote(sys.executable)
    return (
        f"pipx install --force {quoted_url}\n"
        f"{quoted_python} -m pip install --force-reinstall {quoted_url}\n"
        f"{quoted_python} -m pip install --force-reinstall --user {quoted_url}  "
        "# if installed with --user"
    )


def run_self_update(*, repo_url: str, dry_run: bool = False) -> int:
    owner, repo = _owner_repo_from_url(repo_url)
    latest_tag = _latest_release_tag(owner, repo)

    typer.echo(f"Current version: {__version__}")
    typer.echo(f"Latest release: {latest_tag}")

    if _is_up_to_date(__version__, latest_tag):
        typer.echo("Already up to date.")
        return 0

    method_info = _detect_install_method()
    if method_info is None:
        manual_command = _manual_update_command(latest_tag, owner=owner, repo=repo)
        indented = textwrap.indent(manual_command, "  ")
        if dry_run:
            typer.echo(
                "Unable to detect installation method. "
                f"Manual update command:\n{indented}"
            )
            return 0
        typer.echo(
            "Unable to detect installation method. "
            "Please reinstall manually with:\n"
            f"{indented}",
            err=True,
        )
        return 1

    method, is_user = method_info
    command = _update_command(latest_tag, method, is_user=is_user, owner=owner, repo=repo)
    typer.echo(f"Update command: {shlex.join(command)}")

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
