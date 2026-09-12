import json
from pathlib import Path

import pytest
import typer

from cdt.pipeline import PipelineContext
from cdt.services.pypi import PyPIRelease, PyPIUnavailableError
from cdt.steps.github import ReleaseWaitFailure, WaitReleaseStep

RUN_ID = "12345"
RUN_URL = f"https://github.com/example/cdt/actions/runs/{RUN_ID}"
RELEASE_URL = "https://github.com/example/cdt/releases/tag/v0.5.2"
FULL_ASSETS = ("cdt_release-0.5.2-py3-none-any.whl", "cdt_release-0.5.2.tar.gz", "SHA256SUMS")


class ScriptedCapture:
    """Scripted stand-in for the read-only gh/git capture helper, keyed by command markers.

    Every marker holds a list of (code, stdout, stderr) responses; calls consume them in order and
    repeat the last response once the list is exhausted.
    """

    def __init__(self, **marker_results):
        self.commands: list[list[str]] = []
        self._results = {marker: list(responses) for marker, responses in marker_results.items()}
        self._calls: dict[str, int] = {}

    def __call__(self, command: list[str], *, cwd: Path):
        self.commands.append(list(command))
        text = " ".join(command)
        for marker, responses in self._results.items():
            if marker in text:
                index = min(self._calls.get(marker, 0), len(responses) - 1)
                self._calls[marker] = self._calls.get(marker, 0) + 1
                return responses[index]
        raise AssertionError(f"Unexpected captured command: {text}")

    def commands_for(self, marker: str) -> list[list[str]]:
        return [command for command in self.commands if marker in " ".join(command)]


class FakeClock:
    """Deterministic replacement for the time module inside the step."""

    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeRunner:
    def run(self, command: list[str], *, cwd: Path) -> int:  # pragma: no cover - the step never runs commands
        raise AssertionError(f"Unexpected runner command: {command}")


def make_ctx(tmp_path: Path) -> PipelineContext:
    return PipelineContext(cwd=tmp_path, env={}, runner=FakeRunner())


def run_list_response(*, head_branch="v0.5.2", status="completed", conclusion="success", run_id=RUN_ID):
    payload = [
        {
            "databaseId": int(run_id),
            "headBranch": head_branch,
            "headSha": "abc1234567890",
            "status": status,
            "conclusion": conclusion,
            "url": RUN_URL if run_id == RUN_ID else f"https://github.com/example/cdt/actions/runs/{run_id}",
            "displayTitle": f"Release {head_branch}",
        }
    ]
    return (0, json.dumps(payload), "")


def release_view_response(*, draft=False, prerelease=False, assets=FULL_ASSETS):
    payload = {
        "isDraft": draft,
        "isPrerelease": prerelease,
        "url": RELEASE_URL,
        "assets": [{"name": name, "size": 1024} for name in assets],
    }
    return (0, json.dumps(payload), "")


REMOTE_RESPONSE = (0, "https://github.com/example/cdt.git", "")


def green_capture(**overrides):
    """Responses for the fully green path: run found, release published, gh exits zero."""
    responses: dict[str, list] = {
        "run list": [run_list_response()],
        "run view": [(0, json.dumps({"jobs": []}), "")],
        "release view": [release_view_response()],
        "remote get-url": [REMOTE_RESPONSE],
    }
    responses.update(overrides)
    return ScriptedCapture(**responses)


def fake_pypi(monkeypatch, *, files=FULL_ASSETS[:2], error=None, calls=None):
    """Replace cdt.steps.github.fetch_release_files with a scripted in-memory PyPI release."""

    def _fetch(package, version):
        if calls is not None:
            calls.append((package, version))
        if error is not None:
            raise error
        return PyPIRelease(
            package=package,
            version=version,
            files=tuple(sorted(files)),
            url=f"https://pypi.org/project/{package}/{version}/",
        )

    monkeypatch.setattr("cdt.steps.github.fetch_release_files", _fetch)


def patch_capture(monkeypatch, capture):
    """Patch the capture helper in every module the step reaches: github, release (repo derivation)."""
    monkeypatch.setattr("cdt.steps.github._capture", capture)
    monkeypatch.setattr("cdt.steps.release._capture", capture)


def run_step(tmp_path, monkeypatch, capture, *, pypi_files=FULL_ASSETS[:2], pypi_error=None, **options):
    clock = FakeClock()
    patch_capture(monkeypatch, capture)
    monkeypatch.setattr("cdt.steps.github.time", clock)
    fake_pypi(monkeypatch, files=pypi_files, error=pypi_error)
    ctx = make_ctx(tmp_path)
    defaults = {
        "workflow": "release.yml",
        "package": "cdt-release",
        "version": "0.5.2",
        "timeout": 60,
        "poll_interval": 10,
    }
    defaults.update(options)
    WaitReleaseStep(**defaults).run(ctx)
    return ctx, clock


# -- workflow run discovery and terminal conclusion -----------------------------------------------


def test_wait_release_finds_the_exact_tag_run_and_waits_for_green(tmp_path, monkeypatch):
    capture = green_capture()
    ctx, clock = run_step(tmp_path, monkeypatch, capture)

    assert ctx.release_results["github_run_id"] == RUN_ID
    assert ctx.release_results["github_run_url"] == RUN_URL
    assert clock.sleeps == []
    run_list = capture.commands_for("run list")[0]
    assert run_list[:3] == ["gh", "run", "list"]
    assert "--repo" in run_list and "example/cdt" in run_list
    assert "--workflow" in run_list and "release.yml" in run_list
    assert "--json" in run_list  # machine-readable output only


def test_wait_release_ignores_runs_of_other_branches_and_tags(tmp_path, monkeypatch):
    capture = green_capture(**{"run list": [run_list_response(head_branch="main"), run_list_response()]})
    ctx, clock = run_step(tmp_path, monkeypatch, capture)

    assert clock.sleeps == [10.0]
    assert ctx.release_results["github_run_id"] == RUN_ID


def test_wait_release_delays_discovery_until_the_run_appears(tmp_path, monkeypatch):
    capture = green_capture(**{"run list": [(0, "[]", ""), (0, "[]", ""), run_list_response()]})
    ctx, clock = run_step(tmp_path, monkeypatch, capture)

    assert len(capture.commands_for("run list")) == 3
    assert clock.sleeps == [10.0, 10.0]
    assert ctx.release_results["github_run_id"] == RUN_ID


def test_wait_release_polls_until_an_in_progress_run_completes(tmp_path, monkeypatch):
    capture = green_capture(
        **{"run list": [run_list_response(status="in_progress", conclusion=None), run_list_response()]}
    )
    ctx, clock = run_step(tmp_path, monkeypatch, capture)

    assert clock.sleeps == [10.0]
    assert ctx.release_results["github_run_id"] == RUN_ID


def test_wait_release_times_out_when_the_run_never_appears(tmp_path, monkeypatch):
    capture = green_capture(**{"run list": [(0, "[]", "")]})
    clock = FakeClock()
    patch_capture(monkeypatch, capture)
    monkeypatch.setattr("cdt.steps.github.time", clock)
    fake_pypi(monkeypatch)

    with pytest.raises(ReleaseWaitFailure) as exc_info:
        WaitReleaseStep(package="cdt-release", version="0.5.2", timeout=30, poll_interval=10).run(make_ctx(tmp_path))

    failure = exc_info.value
    assert failure.failure_kind == "timeout"
    assert failure.retryable is True
    assert "Timed out" in str(failure)
    assert "not automatically rerun" in str(failure)
    assert clock.sleeps == [10.0, 10.0, 10.0]
    assert not capture.commands_for("release view")


def test_wait_release_times_out_when_the_run_never_completes(tmp_path, monkeypatch):
    capture = green_capture(**{"run list": [run_list_response(status="in_progress", conclusion=None)]})
    clock = FakeClock()
    patch_capture(monkeypatch, capture)
    monkeypatch.setattr("cdt.steps.github.time", clock)
    fake_pypi(monkeypatch)

    with pytest.raises(ReleaseWaitFailure) as exc_info:
        WaitReleaseStep(package="cdt-release", version="0.5.2", timeout=30, poll_interval=10).run(make_ctx(tmp_path))

    assert exc_info.value.failure_kind == "timeout"


def test_wait_release_reports_failed_conclusion_with_run_url_and_job_summary(tmp_path, monkeypatch):
    jobs_payload = json.dumps(
        {
            "jobs": [
                {"name": "release", "conclusion": "failure"},
                {"name": "github-tag-smoke", "conclusion": "success"},
            ]
        }
    )
    capture = green_capture(
        **{
            "run list": [run_list_response(conclusion="failure")],
            "run view": [(0, jobs_payload, "")],
        }
    )

    with pytest.raises(ReleaseWaitFailure) as exc_info:
        run_step(tmp_path, monkeypatch, capture)

    failure = exc_info.value
    assert failure.failure_kind == "workflow_failed"
    assert failure.retryable is False
    message = str(failure)
    assert "concluded with 'failure'" in message
    assert RUN_URL in message
    assert "release (failure)" in message
    assert "github-tag-smoke" not in message.split("Failed jobs:")[1]
    assert not capture.commands_for("release view")
    assert not capture.commands_for("run rerun")


def test_wait_release_reports_terminal_failure_even_when_job_details_are_unavailable(tmp_path, monkeypatch):
    capture = green_capture(
        **{
            "run list": [run_list_response(conclusion="failure")],
            "run view": [(1, "", "gh: run not found")],
        }
    )

    with pytest.raises(ReleaseWaitFailure) as exc_info:
        run_step(tmp_path, monkeypatch, capture)

    failure = exc_info.value
    assert failure.failure_kind == "workflow_failed"
    assert RUN_URL in str(failure)
    assert "Job details are unavailable" in str(failure)


# -- GitHub Release verification ------------------------------------------------------------------


def test_wait_release_requires_full_release_assets(tmp_path, monkeypatch):
    capture = green_capture()
    ctx, _clock = run_step(tmp_path, monkeypatch, capture)

    release_view = capture.commands_for("release view")[0]
    assert release_view[:3] == ["gh", "release", "view"]
    assert "v0.5.2" in release_view
    assert ctx.release_results["github_release_url"] == RELEASE_URL


@pytest.mark.parametrize("flag", ["draft", "prerelease"])
def test_wait_release_rejects_draft_and_prerelease_releases(tmp_path, monkeypatch, flag):
    capture = green_capture(**{"release view": [release_view_response(**{flag: True})]})

    with pytest.raises(ReleaseWaitFailure) as exc_info:
        run_step(tmp_path, monkeypatch, capture)

    failure = exc_info.value
    assert failure.failure_kind == "release_incomplete"
    assert failure.retryable is False
    assert flag in str(failure)


@pytest.mark.parametrize(
    "assets",
    [
        ("cdt_release-0.5.2.tar.gz",),  # no wheel, no checksums
        ("cdt_release-0.5.2-py3-none-any.whl", "SHA256SUMS"),  # no sdist
        ("cdt_release-0.5.2-py3-none-any.whl", "cdt_release-0.5.2.tar.gz"),  # no SHA256SUMS
    ],
)
def test_wait_release_requires_wheel_sdist_and_checksum_assets(tmp_path, monkeypatch, assets):
    capture = green_capture(**{"release view": [release_view_response(assets=assets)]})

    with pytest.raises(ReleaseWaitFailure) as exc_info:
        run_step(tmp_path, monkeypatch, capture)

    failure = exc_info.value
    assert failure.failure_kind == "release_incomplete"
    message = str(failure)
    assert "missing required assets" in message
    assert "SHA256SUMS" in message
    for asset in assets:  # the published assets are listed for the agent
        assert asset in message


def test_wait_release_classifies_unviewable_release_as_transient(tmp_path, monkeypatch):
    capture = green_capture(**{"release view": [(1, "", "gh: server error")]})

    with pytest.raises(ReleaseWaitFailure) as exc_info:
        run_step(tmp_path, monkeypatch, capture)

    failure = exc_info.value
    assert failure.failure_kind == "transient"
    assert failure.retryable is True


def test_wait_release_classifies_malformed_release_json_as_transient(tmp_path, monkeypatch):
    capture = green_capture(**{"release view": [(0, "[" + json.dumps({"isDraft": True}), "")]})

    with pytest.raises(ReleaseWaitFailure) as exc_info:
        run_step(tmp_path, monkeypatch, capture)

    failure = exc_info.value
    assert failure.failure_kind == "transient"
    assert "malformed JSON" in str(failure)


# -- structured classification for the driving agent ------------------------------------------------


@pytest.mark.parametrize(
    ("capture_kwargs", "expected_kind", "expected_retryable"),
    [
        ({"run list": [(0, "[]", "")]}, "timeout", True),
        ({"run list": [(1, "", "gh: auth required")]}, "transient", True),
        (
            {
                "run list": [run_list_response(conclusion="failure")],
                "run view": [(0, json.dumps({"jobs": [{"name": "release", "conclusion": "failure"}]}), "")],
            },
            "workflow_failed",
            False,
        ),
        ({"release view": [release_view_response(draft=True)]}, "release_incomplete", False),
    ],
)
def test_wait_release_classifies_failures_for_the_agent(
    tmp_path, monkeypatch, capture_kwargs, expected_kind, expected_retryable
):
    capture = green_capture(**capture_kwargs)

    with pytest.raises(ReleaseWaitFailure) as exc_info:
        run_step(tmp_path, monkeypatch, capture)

    failure = exc_info.value
    assert failure.failure_kind == expected_kind
    assert failure.retryable is expected_retryable
    assert str(failure).strip()  # the status error keeps a readable message for the agent


def test_wait_release_classifies_pypi_verification_failures(tmp_path, monkeypatch):
    error = PyPIUnavailableError("Could not verify PyPI version cdt-release 0.5.2 after 5 attempts: HTTP 404")

    with pytest.raises(ReleaseWaitFailure) as exc_info:
        run_step(tmp_path, monkeypatch, green_capture(), pypi_error=error)

    failure = exc_info.value
    assert failure.failure_kind == "transient"
    assert failure.retryable is True
    assert "PyPI did not expose" in str(failure)


def test_wait_release_requires_pypi_wheel_and_sdist_files(tmp_path, monkeypatch):
    with pytest.raises(ReleaseWaitFailure) as exc_info:
        run_step(tmp_path, monkeypatch, green_capture(), pypi_files=("cdt_release-0.5.2-py3-none-any.whl",))

    failure = exc_info.value
    assert failure.failure_kind == "release_incomplete"
    assert "sdist" in str(failure)


def test_wait_release_never_reruns_the_workflow(tmp_path, monkeypatch):
    capture = green_capture()
    run_step(tmp_path, monkeypatch, capture)

    gh_commands = [" ".join(command) for command in capture.commands if command[:1] == ["gh"]]
    assert gh_commands, "the step must talk to gh via machine-readable commands"
    assert all(command.startswith("gh run list") or command.startswith("gh release view") for command in gh_commands)
    assert not any("rerun" in command or "run watch" in command for command in gh_commands)


# -- options and version resolution ----------------------------------------------------------------


def test_wait_release_success_registers_confirmation_results(tmp_path, monkeypatch):
    capture = green_capture()
    ctx, _clock = run_step(tmp_path, monkeypatch, capture)

    assert ctx.release_results == {
        "github_run_url": RUN_URL,
        "github_run_id": RUN_ID,
        "github_release_url": RELEASE_URL,
        "pypi_release_url": "https://pypi.org/project/cdt-release/0.5.2/",
        "pypi_wheel": "cdt_release-0.5.2-py3-none-any.whl",
        "pypi_sdist": "cdt_release-0.5.2.tar.gz",
    }


def test_wait_release_resolves_repository_package_and_version_from_context(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "cdt-release"\nversion = "0.5.1"\n',
        encoding="utf-8",
    )
    capture = green_capture()
    patch_capture(monkeypatch, capture)
    monkeypatch.setattr("cdt.steps.github.time", FakeClock())
    fake_pypi(monkeypatch)
    ctx = make_ctx(tmp_path)
    ctx.inputs["version"] = "0.5.2"

    WaitReleaseStep(workflow="release.yml").run(ctx)  # no repository/package/version options

    assert ctx.release_results["pypi_release_url"] == "https://pypi.org/project/cdt-release/0.5.2/"
    assert capture.commands_for("remote get-url"), "repository must be derived from the git remote"
    assert "example/cdt" in " ".join(capture.commands_for("run list")[0])


def test_wait_release_uses_release_tag_context_from_previous_steps(tmp_path, monkeypatch):
    capture = green_capture()
    patch_capture(monkeypatch, capture)
    monkeypatch.setattr("cdt.steps.github.time", FakeClock())
    fake_pypi(monkeypatch)
    ctx = make_ctx(tmp_path)
    ctx.values["release_version"] = "0.5.2"

    WaitReleaseStep(package="cdt-release").run(ctx)

    assert "v0.5.2" in " ".join(capture.commands_for("release view")[0])


def test_wait_release_requires_an_explicit_version(tmp_path, monkeypatch):
    patch_capture(monkeypatch, green_capture())
    monkeypatch.setattr("cdt.steps.github.time", FakeClock())

    with pytest.raises(typer.BadParameter, match="requires an explicit version"):
        WaitReleaseStep(package="cdt-release").run(make_ctx(tmp_path))


@pytest.mark.parametrize("version", ["1.2", "v0.5.2", "latest"])
def test_wait_release_rejects_non_semver_versions(tmp_path, monkeypatch, version):
    patch_capture(monkeypatch, green_capture())
    monkeypatch.setattr("cdt.steps.github.time", FakeClock())

    with pytest.raises(typer.BadParameter, match="not an explicit semver"):
        WaitReleaseStep(package="cdt-release", version=version).run(make_ctx(tmp_path))


def test_wait_release_validates_timing_options(tmp_path):
    ctx = make_ctx(tmp_path)

    with pytest.raises(typer.BadParameter, match="timeout must be a positive number of seconds"):
        WaitReleaseStep(timeout=0).run(ctx)
    with pytest.raises(typer.BadParameter, match="poll_interval must be a positive number of seconds"):
        WaitReleaseStep(poll_interval=0).run(ctx)
    with pytest.raises(typer.BadParameter, match="cannot be greater than the timeout"):
        WaitReleaseStep(timeout=10, poll_interval=20).run(ctx)
