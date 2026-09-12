import json
import urllib.error
from pathlib import Path

import pytest
import typer

from cdt.pipeline import PipelineContext
from cdt.runner import CommandExecutionError
from cdt.steps.git import ReleaseCommitStep, ReleaseTagPushStep, RequireSyncedMainStep
from cdt.steps.release import RequireVersionAvailableStep


class FakeCapture:
    """Scripted stand-in for the read-only git/gh capture helper, keyed by command markers."""

    def __init__(self, **responses):
        self.commands: list[list[str]] = []
        self.responses = responses

    def __call__(self, command: list[str], *, cwd: Path):
        self.commands.append(list(command))
        text = " ".join(command)
        for marker, result in self.responses.items():
            if marker in text:
                return result
        raise AssertionError(f"Unexpected captured command: {text}")


class FakeRunner:
    def __init__(self, exit_codes: dict[str, int] | None = None):
        self.commands: list[list[str]] = []
        self.exit_codes = exit_codes or {}

    def run(self, command: list[str], *, cwd: Path) -> int:
        self.commands.append(list(command))
        for token, code in self.exit_codes.items():
            if token in command:
                return code
        return 0


def make_ctx(tmp_path: Path) -> PipelineContext:
    return PipelineContext(cwd=tmp_path, env={}, runner=FakeRunner())


def _write_project(tmp_path: Path, *, version: str = "0.5.1", changelog: str | None = None) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\n" 'name = "cdt-release"\n' f'version = "{version}"\n',
        encoding="utf-8",
    )
    (tmp_path / "CHANGELOG.md").write_text(
        changelog if changelog is not None else "# Changelog\n\n## Unreleased\n\n- Fresh.\n",
        encoding="utf-8",
    )


AVAILABLE_RESPONSES = dict(
    **{
        "remote get-url": (0, "https://github.com/example/cdt.git", ""),
        "tag --list": (0, "", ""),
        "ls-remote": (0, "", ""),
        "gh release view": (1, "", "release v0.5.2 not found"),
    }
)


def _install_pypi(monkeypatch, *, http_error: int | None = None, versions: tuple[str, ...] = (), fail_times: int = 0):
    """Install a fake urlopen for the PyPI JSON API; returns the list of requested URLs and retry sleeps."""
    requested: list[str] = []
    state = {"failures": fail_times}

    def fake_urlopen(request, timeout=None):
        requested.append(request.full_url)
        if state["failures"] > 0:
            state["failures"] -= 1
            raise urllib.error.URLError("connection reset")
        if http_error is not None:
            raise urllib.error.HTTPError(request.full_url, http_error, "error", None, None)
        releases = {version: [] for version in versions}
        return _FakeResponse(json.dumps({"releases": releases}).encode("utf-8"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    sleeps: list[float] = []
    monkeypatch.setattr("cdt.steps.release.time.sleep", lambda seconds: sleeps.append(seconds))
    return requested, sleeps


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_require_version_available_success_stores_release_context(tmp_path, monkeypatch):
    _write_project(tmp_path)
    capture = FakeCapture(**AVAILABLE_RESPONSES)
    monkeypatch.setattr("cdt.steps.release._capture", capture)
    requested, _sleeps = _install_pypi(monkeypatch, http_error=404)
    ctx = make_ctx(tmp_path)

    RequireVersionAvailableStep(version="0.5.2").run(ctx)

    assert ctx.new_version == "0.5.2"
    assert ctx.old_version == "0.5.1"
    assert ctx.values["release_tag"] == "v0.5.2"
    assert requested == ["https://pypi.org/pypi/cdt-release/json"]
    assert [command[:3] for command in capture.commands] == [
        ["git", "tag", "--list"],
        ["git", "ls-remote", "--tags"],
        ["git", "remote", "get-url"],
        ["gh", "release", "view"],
    ]


def test_require_version_available_uses_pipeline_input_version(tmp_path, monkeypatch):
    _write_project(tmp_path)
    monkeypatch.setattr("cdt.steps.release._capture", FakeCapture(**AVAILABLE_RESPONSES))
    _install_pypi(monkeypatch, http_error=404)
    ctx = make_ctx(tmp_path)
    ctx.inputs["version"] = "0.6.0"

    RequireVersionAvailableStep().run(ctx)

    assert ctx.new_version == "0.6.0"


def test_require_version_available_requires_explicit_version(tmp_path):
    _write_project(tmp_path)

    with pytest.raises(typer.BadParameter, match="requires an explicit version"):
        RequireVersionAvailableStep().run(make_ctx(tmp_path))


@pytest.mark.parametrize("version", ["1.2", "v0.5.2", "latest"])
def test_require_version_available_rejects_non_semver(tmp_path, version):
    _write_project(tmp_path)

    with pytest.raises(typer.BadParameter, match="not an explicit semver"):
        RequireVersionAvailableStep(version=version).run(make_ctx(tmp_path))


@pytest.mark.parametrize("version", ["0.5.1", "0.5.0", "0.4.9"])
def test_require_version_available_rejects_versions_not_newer(tmp_path, version):
    _write_project(tmp_path)

    with pytest.raises(typer.BadParameter, match="must be strictly newer"):
        RequireVersionAvailableStep(version=version).run(make_ctx(tmp_path))


def test_require_version_available_rejects_version_already_in_changelog(tmp_path, monkeypatch):
    _write_project(tmp_path, changelog="# Changelog\n\n## v0.5.2 - 2026-01-01\n\n- Already released.\n")
    monkeypatch.setattr("cdt.steps.release._capture", FakeCapture(**AVAILABLE_RESPONSES))

    with pytest.raises(typer.BadParameter, match="already contains a '## v0.5.2' section"):
        RequireVersionAvailableStep(version="0.5.2").run(make_ctx(tmp_path))


def test_require_version_available_rejects_existing_local_tag(tmp_path, monkeypatch):
    _write_project(tmp_path)
    responses = dict(AVAILABLE_RESPONSES)
    responses["tag --list"] = (0, "v0.5.2", "")
    monkeypatch.setattr("cdt.steps.release._capture", FakeCapture(**responses))

    with pytest.raises(typer.BadParameter, match="Local git tag v0.5.2 already exists"):
        RequireVersionAvailableStep(version="0.5.2").run(make_ctx(tmp_path))


def test_require_version_available_rejects_existing_remote_tag(tmp_path, monkeypatch):
    _write_project(tmp_path)
    responses = dict(AVAILABLE_RESPONSES)
    responses["ls-remote"] = (0, "abc123\trefs/tags/v0.5.2", "")
    monkeypatch.setattr("cdt.steps.release._capture", FakeCapture(**responses))

    with pytest.raises(typer.BadParameter, match="already exists on remote 'origin'"):
        RequireVersionAvailableStep(version="0.5.2").run(make_ctx(tmp_path))


def test_require_version_available_rejects_existing_github_release(tmp_path, monkeypatch):
    _write_project(tmp_path)
    responses = dict(AVAILABLE_RESPONSES)
    responses["gh release view"] = (0, '{"tagName": "v0.5.2"}', "")
    monkeypatch.setattr("cdt.steps.release._capture", FakeCapture(**responses))

    with pytest.raises(typer.BadParameter, match="GitHub Release v0.5.2 already exists"):
        RequireVersionAvailableStep(version="0.5.2").run(make_ctx(tmp_path))


def test_require_version_available_fails_closed_on_gh_errors(tmp_path, monkeypatch):
    _write_project(tmp_path)
    responses = dict(AVAILABLE_RESPONSES)
    responses["gh release view"] = (1, "", "gh: auth required")
    monkeypatch.setattr("cdt.steps.release._capture", FakeCapture(**responses))

    with pytest.raises(typer.BadParameter, match="Could not check GitHub releases"):
        RequireVersionAvailableStep(version="0.5.2").run(make_ctx(tmp_path))


def test_require_version_available_rejects_version_published_on_pypi(tmp_path, monkeypatch):
    _write_project(tmp_path)
    monkeypatch.setattr("cdt.steps.release._capture", FakeCapture(**AVAILABLE_RESPONSES))
    _install_pypi(monkeypatch, versions=("0.5.2",))

    with pytest.raises(typer.BadParameter, match="already published on PyPI"):
        RequireVersionAvailableStep(version="0.5.2").run(make_ctx(tmp_path))


def test_require_version_available_retries_transient_pypi_failures(tmp_path, monkeypatch):
    _write_project(tmp_path)
    monkeypatch.setattr("cdt.steps.release._capture", FakeCapture(**AVAILABLE_RESPONSES))
    _requested, sleeps = _install_pypi(monkeypatch, http_error=404, fail_times=2)
    ctx = make_ctx(tmp_path)

    RequireVersionAvailableStep(version="0.5.2").run(ctx)

    assert ctx.new_version == "0.5.2"
    assert sleeps == [0.5, 1.0]


def test_require_version_available_fails_after_exhausted_pypi_retries(tmp_path, monkeypatch):
    _write_project(tmp_path)
    monkeypatch.setattr("cdt.steps.release._capture", FakeCapture(**AVAILABLE_RESPONSES))
    _install_pypi(monkeypatch, fail_times=99)

    with pytest.raises(typer.BadParameter, match="Could not verify PyPI availability.*after 3 attempts"):
        RequireVersionAvailableStep(version="0.5.2").run(make_ctx(tmp_path))


SHA_HEAD = "1111111111111111111111111111111111111111"
SHA_UPSTREAM = "1111111111111111111111111111111111111111"


def test_require_synced_main_fetches_and_verifies_state(tmp_path, monkeypatch):
    capture = FakeCapture(
        **{
            "is-inside-work-tree": (0, "true", ""),
            "remote get-url": (0, "https://github.com/example/cdt.git", ""),
            "branch --show-current": (0, "main", ""),
            "status --porcelain": (0, "", ""),
            "rev-parse HEAD": (0, SHA_HEAD, ""),
            "rev-parse --verify origin/main": (0, SHA_UPSTREAM, ""),
        }
    )
    monkeypatch.setattr("cdt.steps.git._capture", capture)
    runner = FakeRunner()
    ctx = PipelineContext(cwd=tmp_path, env={}, runner=runner)

    RequireSyncedMainStep().run(ctx)

    assert runner.commands == [
        ["git", "fetch", "origin", "main"],
        ["git", "fetch", "origin", "--tags"],
    ]


@pytest.mark.parametrize(
    ("responses", "match"),
    [
        (
            {"is-inside-work-tree": (1, "", "fatal")},
            "Not a git repository",
        ),
        (
            {
                "is-inside-work-tree": (0, "true", ""),
                "remote get-url": (1, "", "error"),
            },
            "No git remote 'origin' configured",
        ),
        (
            {
                "is-inside-work-tree": (0, "true", ""),
                "remote get-url": (0, "url", ""),
                "branch --show-current": (0, "feature/x", ""),
            },
            "current branch is 'feature/x'",
        ),
        (
            {
                "is-inside-work-tree": (0, "true", ""),
                "remote get-url": (0, "url", ""),
                "branch --show-current": (0, "main", ""),
                "status --porcelain": (0, " M cdt/__init__.py", ""),
            },
            "uncommitted changes to tracked files",
        ),
        (
            {
                "is-inside-work-tree": (0, "true", ""),
                "remote get-url": (0, "url", ""),
                "branch --show-current": (0, "main", ""),
                "status --porcelain": (0, "", ""),
                "rev-parse HEAD": (0, "1111", ""),
                "rev-parse --verify origin/main": (1, "", "unknown revision"),
            },
            "Cannot resolve origin/main",
        ),
        (
            {
                "is-inside-work-tree": (0, "true", ""),
                "remote get-url": (0, "url", ""),
                "branch --show-current": (0, "main", ""),
                "status --porcelain": (0, "", ""),
                "rev-parse HEAD": (0, "111111111111", ""),
                "rev-parse --verify origin/main": (0, "222222222222", ""),
            },
            "does not match origin/main",
        ),
    ],
)
def test_require_synced_main_rejects_unsafe_states(tmp_path, monkeypatch, responses, match):
    monkeypatch.setattr("cdt.steps.git._capture", FakeCapture(**responses))

    with pytest.raises(typer.BadParameter, match=match):
        RequireSyncedMainStep().run(PipelineContext(cwd=tmp_path, env={}, runner=FakeRunner()))


def test_release_commit_stages_only_configured_files_and_verifies_commit(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("version\n", encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text("changelog\n", encoding="utf-8")
    staged = ["CHANGELOG.md", "pyproject.toml"]
    capture = FakeCapture(
        **{
            "diff --cached --name-only": (0, "\n".join(staged), ""),
            "show --name-only": (0, "\n".join(staged), ""),
            "log -1": (0, "Release v0.5.2", ""),
        }
    )
    monkeypatch.setattr("cdt.steps.git._capture", capture)
    runner = FakeRunner()
    ctx = PipelineContext(cwd=tmp_path, env={}, runner=runner)
    ctx.new_version = "0.5.2"
    ctx.register_rollback_file(tmp_path / "pyproject.toml")

    ReleaseCommitStep(files=["pyproject.toml", "CHANGELOG.md"]).run(ctx)

    assert runner.commands == [
        ["git", "add", "--", "pyproject.toml"],
        ["git", "add", "--", "CHANGELOG.md"],
        ["git", "commit", "-m", "Release v0.5.2"],
    ]
    assert ctx.rollback_closed
    assert not ctx.rollback_pending


def test_release_commit_requires_explicit_files(tmp_path):
    with pytest.raises(typer.BadParameter, match="explicit non-empty 'files' list"):
        ReleaseCommitStep(files=[]).run(make_ctx(tmp_path))


def test_release_commit_rejects_missing_file(tmp_path):
    with pytest.raises(typer.BadParameter, match="Release file not found: pyproject.toml"):
        ReleaseCommitStep(files=["pyproject.toml"]).run(make_ctx(tmp_path))


def test_release_commit_rejects_unexpected_staged_files(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("version\n", encoding="utf-8")
    capture = FakeCapture(**{"diff --cached --name-only": (0, "pyproject.toml\nsneaky.txt", "")})
    monkeypatch.setattr("cdt.steps.git._capture", capture)

    with pytest.raises(typer.BadParameter, match="unexpected staged files are present: sneaky.txt"):
        ReleaseCommitStep(files=["pyproject.toml"]).run(make_ctx(tmp_path))


def test_release_commit_rejects_empty_staged_set(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("version\n", encoding="utf-8")
    monkeypatch.setattr("cdt.steps.git._capture", FakeCapture(**{"diff --cached --name-only": (0, "", "")}))

    with pytest.raises(typer.BadParameter, match="No release file changes are staged"):
        ReleaseCommitStep(files=["pyproject.toml"]).run(make_ctx(tmp_path))


def test_release_commit_reports_staging_failure(tmp_path):
    (tmp_path / "pyproject.toml").write_text("version\n", encoding="utf-8")
    runner = FakeRunner(exit_codes={"add": 128})
    ctx = PipelineContext(cwd=tmp_path, env={}, runner=runner)

    with pytest.raises(CommandExecutionError) as exc_info:
        ReleaseCommitStep(files=["pyproject.toml"]).run(ctx)

    assert exc_info.value.command == ["git", "add", "--", "pyproject.toml"]
    assert exc_info.value.exit_code == 128


def test_release_commit_rejects_commit_content_mismatch(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("version\n", encoding="utf-8")
    capture = FakeCapture(
        **{
            "diff --cached --name-only": (0, "pyproject.toml", ""),
            "show --name-only": (0, "", ""),
        }
    )
    monkeypatch.setattr("cdt.steps.git._capture", capture)
    ctx = make_ctx(tmp_path)
    ctx.new_version = "0.5.2"

    with pytest.raises(typer.BadParameter, match="do not match the staged release files"):
        ReleaseCommitStep(files=["pyproject.toml"]).run(ctx)


def _tag_push_capture(
    *,
    local_tag: str | None = None,
    tag_commit: str = SHA_HEAD,
    remote_line: str = "",
    head: str = SHA_HEAD,
):
    responses = {
        "rev-parse HEAD": (0, head, ""),
    }
    if local_tag is not None:
        responses["v0.5.2^{commit}"] = (0, tag_commit, "")
        responses["rev-parse --verify --quiet"] = (0, "tagobjectsha", "")
        responses["rev-parse --verify refs/tags"] = (0, "tagobjectsha", "")
    else:
        responses["rev-parse --verify --quiet"] = (1, "", "")
    responses["ls-remote"] = (0, remote_line, "")
    return FakeCapture(**responses)


def test_release_tag_push_creates_tag_and_pushes_atomically(tmp_path, monkeypatch):
    monkeypatch.setattr("cdt.steps.git._capture", _tag_push_capture(remote_line=""))
    runner = FakeRunner()
    ctx = PipelineContext(cwd=tmp_path, env={}, runner=runner)
    ctx.values["release_tag"] = "v0.5.2"

    ReleaseTagPushStep(branch="main").run(ctx)

    assert runner.commands == [
        ["git", "tag", "-a", "v0.5.2", "-m", "Release v0.5.2", SHA_HEAD],
        ["git", "push", "--atomic", "origin", "main", "v0.5.2"],
    ]


def test_release_tag_push_is_idempotent_when_tag_already_pushed(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "cdt.steps.git._capture",
        _tag_push_capture(local_tag="v0.5.2", remote_line="tagobjectsha\trefs/tags/v0.5.2"),
    )
    runner = FakeRunner()
    ctx = PipelineContext(cwd=tmp_path, env={}, runner=runner)
    ctx.values["release_tag"] = "v0.5.2"

    ReleaseTagPushStep().run(ctx)

    assert runner.commands == []


def test_release_tag_push_pushes_when_remote_tag_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("cdt.steps.git._capture", _tag_push_capture(local_tag="v0.5.2", remote_line=""))
    runner = FakeRunner()
    ctx = PipelineContext(cwd=tmp_path, env={}, runner=runner)
    ctx.values["release_tag"] = "v0.5.2"

    ReleaseTagPushStep().run(ctx)

    assert runner.commands == [["git", "push", "--atomic", "origin", "main", "v0.5.2"]]


def test_release_tag_push_rejects_conflicting_local_tag(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "cdt.steps.git._capture",
        _tag_push_capture(local_tag="v0.5.2", tag_commit="9999999999999999999999999999999999999999"),
    )
    ctx = PipelineContext(cwd=tmp_path, env={}, runner=FakeRunner())
    ctx.values["release_tag"] = "v0.5.2"

    with pytest.raises(typer.BadParameter, match="moving existing tags is not allowed"):
        ReleaseTagPushStep().run(ctx)


def test_release_tag_push_rejects_conflicting_remote_tag(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "cdt.steps.git._capture",
        _tag_push_capture(local_tag="v0.5.2", remote_line="fedcba\trefs/tags/v0.5.2"),
    )
    ctx = PipelineContext(cwd=tmp_path, env={}, runner=FakeRunner())
    ctx.values["release_tag"] = "v0.5.2"

    with pytest.raises(typer.BadParameter, match="already exists with a different target"):
        ReleaseTagPushStep().run(ctx)


def test_release_tag_push_reports_push_failure(tmp_path, monkeypatch):
    monkeypatch.setattr("cdt.steps.git._capture", _tag_push_capture(remote_line=""))
    runner = FakeRunner(exit_codes={"push": 128})
    ctx = PipelineContext(cwd=tmp_path, env={}, runner=runner)
    ctx.values["release_tag"] = "v0.5.2"

    with pytest.raises(CommandExecutionError) as exc_info:
        ReleaseTagPushStep().run(ctx)

    assert exc_info.value.command == ["git", "push", "--atomic", "origin", "main", "v0.5.2"]
    assert exc_info.value.exit_code == 128


def test_release_tag_push_requires_tag(tmp_path):
    with pytest.raises(typer.BadParameter, match="requires a 'tag' option or release version context"):
        ReleaseTagPushStep().run(make_ctx(tmp_path))


def test_release_tag_push_rejects_invalid_tag(tmp_path):
    ctx = make_ctx(tmp_path)

    with pytest.raises(typer.BadParameter, match="Invalid release tag"):
        ReleaseTagPushStep(tag="bad tag!").run(ctx)


def test_rollback_boundary_lifecycle(tmp_path):
    target = tmp_path / "release-file.txt"
    target.write_text("original", encoding="utf-8")
    ctx = make_ctx(tmp_path)

    ctx.register_rollback_file(target)
    assert ctx.rollback_pending

    target.write_text("modified", encoding="utf-8")
    restored = ctx.perform_rollback()

    assert restored == [target.resolve()]
    assert target.read_text(encoding="utf-8") == "original"
    assert ctx.rolled_back
    assert not ctx.rollback_pending

    target.write_text("touched after rollback", encoding="utf-8")
    ctx.close_rollback_boundary()
    assert not ctx.rollback_pending
    with pytest.raises(typer.BadParameter, match="rollback boundary is closed"):
        ctx.register_rollback_file(target)


def test_rollback_restore_is_exact_including_bytes(tmp_path):
    target = tmp_path / "release-file.bin"
    target.write_bytes(b"\x00\x01trailing \n\n")
    ctx = make_ctx(tmp_path)

    ctx.register_rollback_file(target)
    target.write_bytes(b"replaced")
    ctx.perform_rollback()

    assert target.read_bytes() == b"\x00\x01trailing \n\n"
