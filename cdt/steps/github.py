"""GitHub release confirmation built-in: wait for Actions, then verify the release and PyPI."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import typer

from ..pipeline import PipelineContext
from ..services.pypi import PyPIRelease, PyPIUnavailableError, fetch_release_files
from .git import _capture
from .release import github_repo_from_origin, read_pyproject_metadata, resolve_release_version

# Structured failure kinds reported to the driving agent; the step never reruns the workflow itself.
KIND_TIMEOUT = "timeout"
KIND_TRANSIENT = "transient"
KIND_WORKFLOW_FAILED = "workflow_failed"
KIND_RELEASE_INCOMPLETE = "release_incomplete"

_RUN_LIST_FIELDS = "databaseId,headBranch,headSha,status,conclusion,url,displayTitle"
_GOOD_JOB_CONCLUSIONS = {"success", "skipped", "neutral"}


class ReleaseWaitFailure(typer.BadParameter):
    """Failure of github.wait_release carrying a structured classification for the driving agent.

    The step never reruns the workflow automatically; ``failure_kind`` tells the agent what happened:

      - ``timeout``: the workflow run never reached a terminal conclusion before the timeout;
      - ``transient``: gh or PyPI could not be verified (network, auth, malformed gh output, indexing
        delay); the step itself can simply be retried;
      - ``workflow_failed``: the run reached a terminal failure conclusion; fix the code, do not just rerun;
      - ``release_incomplete``: a green workflow published an incomplete GitHub Release or PyPI distribution.
    """

    def __init__(
        self,
        message: str,
        *,
        failure_kind: str,
        retryable: bool = False,
        details: dict[str, str] | None = None,
    ):
        super().__init__(message)
        self.failure_kind = failure_kind
        self.retryable = retryable
        self.details = dict(details or {})


@dataclass(frozen=True)
class WorkflowRun:
    """A GitHub Actions run as reported by machine-readable `gh run list` output."""

    run_id: str
    status: str
    conclusion: str | None
    url: str


class WaitReleaseStep:
    """Wait for the release workflow run, then confirm the GitHub Release and the PyPI distribution."""

    name = "github.wait_release"

    def __init__(
        self,
        repository: str | None = None,
        workflow: str = "release.yml",
        package: str | None = None,
        version: str | None = None,
        timeout: float = 1800.0,
        poll_interval: float = 15.0,
        pyproject: str = "pyproject.toml",
        remote: str = "origin",
        tag_prefix: str = "v",
    ):
        self.repository = repository
        self.workflow = workflow
        self.package = package
        self.version = version
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.pyproject = pyproject
        self.remote = remote
        self.tag_prefix = tag_prefix

    def run(self, ctx: PipelineContext) -> None:
        self._validate_timing()
        version = resolve_release_version(self.version or ctx.values.get("release_version") or ctx.new_version, ctx)
        tag = f"{self.tag_prefix}{version}"
        repo = self.repository or github_repo_from_origin(ctx, self.remote)
        package = self.package or read_pyproject_metadata(ctx.project_path(self.pyproject))[0]

        typer.echo(f"==> Waiting for workflow '{self.workflow}' of {tag} in {repo} (timeout {self.timeout:.0f}s)")
        run = self._wait_for_green_run(ctx, repo, tag)
        release_url = self._require_published_release(ctx, repo, tag)
        pypi = self._require_published_pypi(package, version)

        ctx.register_release_results(
            {
                "github_run_url": run.url,
                "github_run_id": run.run_id,
                "github_release_url": release_url,
                "pypi_release_url": pypi.url,
                "pypi_wheel": pypi.wheels[0],
                "pypi_sdist": pypi.sdists[0],
            }
        )
        typer.echo(f"==> Release {version} confirmed: {release_url} and {pypi.url}")

    def _validate_timing(self) -> None:
        if self.timeout <= 0:
            raise typer.BadParameter("github.wait_release timeout must be a positive number of seconds")
        if self.poll_interval <= 0:
            raise typer.BadParameter("github.wait_release poll_interval must be a positive number of seconds")
        if self.poll_interval > self.timeout:
            raise typer.BadParameter("github.wait_release poll_interval cannot be greater than the timeout")

    # -- GitHub Actions workflow run ---------------------------------------------------------------

    def _wait_for_green_run(self, ctx: PipelineContext, repo: str, tag: str) -> WorkflowRun:
        """Poll `gh run list` until the run of the exact tag reaches a terminal green conclusion."""
        deadline = time.monotonic() + self.timeout
        while True:
            run = self._find_run(ctx, repo, tag)
            if run is None:
                typer.echo(f"==> No workflow run for {tag} yet; polling again in {self.poll_interval:.0f}s")
            elif run.status != "completed":
                typer.echo(
                    f"==> Workflow run {run.run_id} is {run.status or 'pending'}; "
                    f"polling again in {self.poll_interval:.0f}s"
                )
            elif run.conclusion == "success":
                return run
            else:
                raise self._terminal_failure(ctx, repo, run, tag)
            if time.monotonic() >= deadline:
                raise ReleaseWaitFailure(
                    f"Timed out after {self.timeout:.0f}s waiting for workflow '{self.workflow}' run of {tag} "
                    f"in {repo}; the workflow was not automatically rerun",
                    failure_kind=KIND_TIMEOUT,
                    retryable=True,
                    details={"repository": repo, "workflow": self.workflow, "tag": tag},
                )
            time.sleep(self.poll_interval)

    def _find_run(self, ctx: PipelineContext, repo: str, tag: str) -> WorkflowRun | None:
        """Find the workflow run of the exact release tag via machine-readable `gh run list` output."""
        command = [
            "gh",
            "run",
            "list",
            "--repo",
            repo,
            "--workflow",
            self.workflow,
            "--json",
            _RUN_LIST_FIELDS,
            "--limit",
            "50",
        ]
        code, out, err = _capture(command, cwd=ctx.cwd)
        if code != 0:
            raise ReleaseWaitFailure(
                f"gh run list failed for {repo}: {err or f'gh exited with {code}'}",
                failure_kind=KIND_TRANSIENT,
                retryable=True,
                details={"repository": repo, "workflow": self.workflow},
            )
        payload = self._parse_gh_json(ctx, out, "gh run list")
        for entry in payload:
            if not isinstance(entry, dict) or not entry.get("databaseId"):
                continue
            if str(entry.get("headBranch") or "") != tag:
                continue
            conclusion = entry.get("conclusion")
            return WorkflowRun(
                run_id=str(entry["databaseId"]),
                status=str(entry.get("status") or ""),
                conclusion=conclusion if isinstance(conclusion, str) else None,
                url=str(entry.get("url") or ""),
            )
        return None

    def _terminal_failure(self, ctx: PipelineContext, repo: str, run: WorkflowRun, tag: str) -> ReleaseWaitFailure:
        """Build the terminal workflow-failure classification with the run URL and failed job summary."""
        run_url = run.url or f"https://github.com/{repo}/actions/runs/{run.run_id}"
        lines = [
            f"Workflow run {run.run_id} for {tag} concluded with '{run.conclusion or 'unknown'}'; "
            "the workflow was not automatically rerun.",
            f"Run URL: {run_url}",
        ]
        failed_jobs = self._failed_jobs(ctx, repo, run.run_id)
        if failed_jobs:
            lines.append("Failed jobs:")
            lines.extend(f"  - {name} ({conclusion})" for name, conclusion in failed_jobs)
        else:
            lines.append("Job details are unavailable (gh run view failed); inspect the run URL above.")
        return ReleaseWaitFailure(
            "\n".join(lines),
            failure_kind=KIND_WORKFLOW_FAILED,
            retryable=False,
            details={"run_url": run_url, "run_id": run.run_id, "conclusion": run.conclusion or "unknown"},
        )

    def _failed_jobs(self, ctx: PipelineContext, repo: str, run_id: str) -> list[tuple[str, str]]:
        """Return (name, conclusion) of failed jobs from `gh run view`; empty when details are unavailable."""
        command = ["gh", "run", "view", run_id, "--repo", repo, "--json", "jobs"]
        code, out, _err = _capture(command, cwd=ctx.cwd)
        if code != 0:
            return []
        try:
            payload = json.loads(out or "{}")
        except json.JSONDecodeError:
            return []
        jobs = payload.get("jobs") if isinstance(payload, dict) else None
        if not isinstance(jobs, list):
            return []
        failed: list[tuple[str, str]] = []
        for job in jobs:
            if not isinstance(job, dict):
                continue
            conclusion = job.get("conclusion")
            if isinstance(conclusion, str) and conclusion not in _GOOD_JOB_CONCLUSIONS:
                failed.append((str(job.get("name") or "unnamed job"), conclusion))
        return failed

    # -- GitHub Release ----------------------------------------------------------------------------

    def _require_published_release(self, ctx: PipelineContext, repo: str, tag: str) -> str:
        """Require a full (not draft/prerelease) GitHub Release with wheel, sdist and SHA256SUMS assets."""
        command = ["gh", "release", "view", tag, "--repo", repo, "--json", "isDraft,isPrerelease,assets,url"]
        code, out, err = _capture(command, cwd=ctx.cwd)
        if code != 0:
            raise ReleaseWaitFailure(
                f"Could not view GitHub Release {tag} in {repo} after a green workflow: "
                f"{err or f'gh exited with {code}'}",
                failure_kind=KIND_TRANSIENT,
                retryable=True,
                details={"repository": repo, "tag": tag},
            )
        payload = self._parse_gh_json(ctx, out, "gh release view")
        if payload.get("isDraft") or payload.get("isPrerelease"):
            state = "draft" if payload.get("isDraft") else "prerelease"
            raise ReleaseWaitFailure(
                f"GitHub Release {tag} in {repo} is a {state} release; expected a full published release",
                failure_kind=KIND_RELEASE_INCOMPLETE,
                retryable=False,
                details={"repository": repo, "tag": tag},
            )
        assets = [
            str(asset["name"]) for asset in payload.get("assets", []) if isinstance(asset, dict) and asset.get("name")
        ]
        wheel = next((name for name in assets if name.endswith(".whl")), None)
        sdist = next((name for name in assets if name.endswith(".tar.gz")), None)
        checksum = next((name for name in assets if name.lower().startswith("sha256sums")), None)
        missing = [
            label for label, found in (("wheel", wheel), ("sdist", sdist), ("SHA256SUMS", checksum)) if not found
        ]
        if missing:
            raise ReleaseWaitFailure(
                f"GitHub Release {tag} in {repo} is missing required assets: {', '.join(missing)}. "
                f"Published assets: {', '.join(assets) if assets else 'none'}",
                failure_kind=KIND_RELEASE_INCOMPLETE,
                retryable=False,
                details={"repository": repo, "tag": tag, "assets": ", ".join(assets)},
            )
        return str(payload.get("url") or f"https://github.com/{repo}/releases/tag/{tag}")

    # -- PyPI --------------------------------------------------------------------------------------

    def _require_published_pypi(self, package: str, version: str) -> PyPIRelease:
        """Require the exact version on PyPI with wheel and sdist files, tolerating indexing delay."""
        try:
            release = fetch_release_files(package, version)
        except PyPIUnavailableError as exc:
            raise ReleaseWaitFailure(
                f"PyPI did not expose {package} {version}: {exc}",
                failure_kind=KIND_TRANSIENT,
                retryable=True,
                details={"pypi_package": package, "version": version},
            ) from exc
        missing = [label for label, files in (("wheel", release.wheels), ("sdist", release.sdists)) if not files]
        if missing:
            raise ReleaseWaitFailure(
                f"PyPI release {package} {version} is missing required files: {', '.join(missing)}. "
                f"Published files: {', '.join(release.files) if release.files else 'none'}",
                failure_kind=KIND_RELEASE_INCOMPLETE,
                retryable=False,
                details={"pypi_package": package, "version": version},
            )
        return release

    # -- helpers -----------------------------------------------------------------------------------

    def _parse_gh_json(self, ctx: PipelineContext, out: str, source: str) -> Any:
        """Parse machine-readable gh output, classifying malformed JSON as a transient failure."""
        try:
            payload = json.loads(out or "null")
        except json.JSONDecodeError as exc:
            raise ReleaseWaitFailure(
                f"{source} produced malformed JSON: {exc}",
                failure_kind=KIND_TRANSIENT,
                retryable=True,
            ) from exc
        if source == "gh run list":
            if not isinstance(payload, list):
                raise ReleaseWaitFailure(
                    f"{source} produced malformed JSON: expected a list of runs",
                    failure_kind=KIND_TRANSIENT,
                    retryable=True,
                )
            return payload
        if not isinstance(payload, dict):
            raise ReleaseWaitFailure(
                f"{source} produced malformed JSON: expected an object",
                failure_kind=KIND_TRANSIENT,
                retryable=True,
            )
        return payload
