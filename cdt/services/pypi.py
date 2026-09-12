"""PyPI JSON API client used to confirm published release distributions."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlsplit

import typer

PYPI_BASE_URL = "https://pypi.org/pypi"
PYPI_ATTEMPTS = 5
PYPI_BASE_DELAY_SECONDS = 1.0
PYPI_REQUEST_TIMEOUT_SEC = 10
_USER_AGENT = "cdt-release-verification"


class PyPIUnavailableError(RuntimeError):
    """The PyPI JSON API could not confirm a version after bounded retries."""


@dataclass(frozen=True)
class PyPIRelease:
    """A single exact version published on PyPI together with its distribution files."""

    package: str
    version: str
    files: tuple[str, ...]
    url: str

    @property
    def wheels(self) -> tuple[str, ...]:
        return tuple(name for name in self.files if name.endswith(".whl"))

    @property
    def sdists(self) -> tuple[str, ...]:
        return tuple(name for name in self.files if name.endswith(".tar.gz"))


def fetch_release_files(
    package: str,
    version: str,
    *,
    attempts: int = PYPI_ATTEMPTS,
    base_delay: float = PYPI_BASE_DELAY_SECONDS,
    base_url: str = PYPI_BASE_URL,
) -> PyPIRelease:
    """Fetch the distribution files of an exact version with bounded retries/backoff.

    A missing version (HTTP 404) is retried like any other failure because PyPI indexing can lag
    behind a fresh release; transient HTTP/network failures are retried as well. Once the attempts
    are exhausted, :class:`PyPIUnavailableError` is raised.
    """
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    api_url = f"{base_url}/{package}/{version}/json"
    host = f"{urlsplit(base_url).scheme}://{urlsplit(base_url).netloc}"
    project_url = f"{host}/project/{package}/{version}/"

    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(api_url, headers={"User-Agent": _USER_AGENT})
            with urllib.request.urlopen(request, timeout=PYPI_REQUEST_TIMEOUT_SEC) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("unexpected JSON payload: expected an object")
            files = tuple(
                sorted(
                    {
                        str(item["filename"])
                        for item in payload.get("urls", [])
                        if isinstance(item, dict) and item.get("filename")
                    }
                )
            )
            return PyPIRelease(package=package, version=version, files=files, url=project_url)
        except urllib.error.HTTPError as exc:
            last_error = "version is not indexed yet (HTTP 404)" if exc.code == 404 else f"HTTP {exc.code}"
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError, ValueError) as exc:
            last_error = str(exc) or type(exc).__name__
        if attempt < attempts:
            delay = base_delay * (2 ** (attempt - 1))
            typer.echo(
                f"==> PyPI check for {package} {version} failed ({last_error}); retrying in {delay:.1f}s",
                err=True,
            )
            time.sleep(delay)
    raise PyPIUnavailableError(
        f"Could not verify PyPI version {package} {version} after {attempts} attempts: {last_error}"
    )
