import re
import subprocess
from pathlib import Path

import typer

from ..pipeline import PipelineContext
from ..runner import CommandExecutionError, _prepare_git_clean_main
from ..sounds import _play_fail_sound
from ..versioning import _current_flutter_version

_RELEASE_TAG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")


def _capture(command: list[str], *, cwd: Path) -> tuple[int, str, str]:
    """Run a read-only git/gh inspection command and capture stdout/stderr."""
    result = subprocess.run(command, cwd=cwd, check=False, capture_output=True, text=True)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _git_output(command: list[str], *, cwd: Path) -> str:
    """Run a read-only git inspection command and return its stdout, failing with a readable error."""
    code, out, err = _capture(command, cwd=cwd)
    if code != 0:
        joined = " ".join(command)
        raise typer.BadParameter(f"Git command failed: {joined}: {err or 'no error output'}")
    return out


class PrepareGitMainStep:
    name = "git.prepare_clean_main"

    def run(self, ctx: PipelineContext) -> None:
        typer.echo("==> Preparing git state: restore + clean + checkout main/master")
        _prepare_git_clean_main(ctx.cwd)


class GitAddCommitPushStep:
    name = "git.commit_push"

    def __init__(self, repository: str | None = None, message: str | None = None):
        self.repository = repository
        self.message = message

    def run(self, ctx: PipelineContext) -> None:
        if self.repository is not None:
            web_repo = ctx.project_path(self.repository)
        else:
            try:
                web_repo = Path(ctx.values["web_repo"])
            except KeyError as exc:
                raise typer.BadParameter("Missing git.commit_push.repository or pipeline value: web_repo") from exc
        message = self.message or ctx.values.get("flutter_version") or _current_flutter_version(ctx.cwd)

        if ctx.runner.run(["git", "add", "."], cwd=web_repo) != 0:
            _play_fail_sound(ctx.env, ctx.cwd)
            raise typer.Exit(code=1)
        if ctx.runner.run(["git", "commit", "-m", message], cwd=web_repo) != 0:
            _play_fail_sound(ctx.env, ctx.cwd)
            raise typer.Exit(code=1)
        if ctx.runner.run(["git", "push"], cwd=web_repo) != 0:
            _play_fail_sound(ctx.env, ctx.cwd)
            raise typer.Exit(code=1)

        typer.echo(f"✅ Deploy completed. Web repo committed with message: {message}")


class RequireSyncedMainStep:
    name = "git.require_synced_main"

    def __init__(self, remote: str = "origin", branch: str = "main"):
        self.remote = remote
        self.branch = branch

    def run(self, ctx: PipelineContext) -> None:
        code, _out, _err = _capture(["git", "rev-parse", "--is-inside-work-tree"], cwd=ctx.cwd)
        if code != 0:
            raise typer.BadParameter(f"Not a git repository: {ctx.cwd}")
        code, _url, _err = _capture(["git", "remote", "get-url", self.remote], cwd=ctx.cwd)
        if code != 0:
            raise typer.BadParameter(f"No git remote '{self.remote}' configured in {ctx.cwd}")

        current = _git_output(["git", "branch", "--show-current"], cwd=ctx.cwd)
        if current != self.branch:
            label = current or "DETACHED HEAD"
            raise typer.BadParameter(
                f"Release requires the current branch to be '{self.branch}'; current branch is '{label}'"
            )

        status = _git_output(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ctx.cwd)
        if status:
            listed = "\n".join(status.splitlines()[:10])
            raise typer.BadParameter(
                "Working tree has uncommitted changes to tracked files; commit, stash, or restore them first:\n"
                + listed
            )

        fetch_branch = ["git", "fetch", self.remote, self.branch]
        code = ctx.runner.run(fetch_branch, cwd=ctx.cwd)
        if code != 0:
            raise CommandExecutionError(
                f"Failed to fetch {self.remote}/{self.branch}", command=fetch_branch, exit_code=code
            )
        fetch_tags = ["git", "fetch", self.remote, "--tags"]
        code = ctx.runner.run(fetch_tags, cwd=ctx.cwd)
        if code != 0:
            raise CommandExecutionError(f"Failed to fetch tags from {self.remote}", command=fetch_tags, exit_code=code)

        head = _git_output(["git", "rev-parse", "HEAD"], cwd=ctx.cwd)
        upstream = f"{self.remote}/{self.branch}"
        code, remote_head, _err = _capture(["git", "rev-parse", "--verify", upstream], cwd=ctx.cwd)
        if code != 0 or not remote_head:
            raise typer.BadParameter(f"Cannot resolve {upstream}; is the remote branch published?")
        if head != remote_head:
            raise typer.BadParameter(
                f"Local HEAD {head[:12]} does not match {upstream} {remote_head[:12]}. "
                f"Sync '{self.branch}' with {self.remote} before releasing; diverged states are rejected."
            )

        typer.echo(f"==> Git state verified: clean tracked tree, branch '{self.branch}' at {upstream} ({head[:12]})")


class ReleaseCommitStep:
    name = "git.release_commit"

    def __init__(self, files: list[str], message: str | None = None):
        self.files = files
        self.message = message

    def run(self, ctx: PipelineContext) -> None:
        files = [item.strip() for item in (self.files or []) if item and item.strip()]
        if not files:
            raise typer.BadParameter(
                "git.release_commit requires an explicit non-empty 'files' list; broad staging is not allowed"
            )
        normalized = [Path(item).as_posix() for item in files]
        expected = set(normalized)
        for item in normalized:
            if not ctx.project_path(item).is_file():
                raise typer.BadParameter(f"Release file not found: {item}")

        for item in normalized:
            command = ["git", "add", "--", item]
            code = ctx.runner.run(command, cwd=ctx.cwd)
            if code != 0:
                raise CommandExecutionError(f"Failed to stage release file {item}", command=command, exit_code=code)

        staged_output = _git_output(["git", "diff", "--cached", "--name-only"], cwd=ctx.cwd)
        staged = {line.strip() for line in staged_output.splitlines() if line.strip()}
        unexpected = sorted(staged - expected)
        if unexpected:
            raise typer.BadParameter(
                "Refusing to commit: unexpected staged files are present: " + ", ".join(unexpected)
            )
        if not staged:
            raise typer.BadParameter("No release file changes are staged; nothing to commit")

        message = self.message or _release_commit_message(ctx)
        commit_command = ["git", "commit", "-m", message]
        code = ctx.runner.run(commit_command, cwd=ctx.cwd)
        if code != 0:
            raise CommandExecutionError("Failed to create the release commit", command=commit_command, exit_code=code)

        committed = {
            line.strip()
            for line in _git_output(["git", "show", "--name-only", "--format="], cwd=ctx.cwd).splitlines()
            if line.strip()
        }
        if committed != staged:
            raise typer.BadParameter(
                f"Release commit contents do not match the staged release files: expected {sorted(staged)}, "
                f"got {sorted(committed)}"
            )
        subject = _git_output(["git", "log", "-1", "--format=%s"], cwd=ctx.cwd)
        if subject != message:
            raise typer.BadParameter(f"Release commit subject mismatch: expected {message!r}, got {subject!r}")

        ctx.close_rollback_boundary()
        typer.echo(f"==> Release commit created: {message} ({len(staged)} file(s))")


def _release_commit_message(ctx: PipelineContext) -> str:
    tag = str(ctx.values.get("release_tag") or "").strip()
    if not tag and ctx.new_version:
        tag = f"v{ctx.new_version}"
    if not tag:
        raise typer.BadParameter(
            "git.release_commit requires a 'message' option or release version context from earlier release steps"
        )
    return f"Release {tag}"


class ReleaseTagPushStep:
    name = "git.release_tag_push"

    def __init__(
        self,
        tag: str | None = None,
        remote: str = "origin",
        branch: str = "main",
        message: str | None = None,
    ):
        self.tag = tag
        self.remote = remote
        self.branch = branch
        self.message = message

    def run(self, ctx: PipelineContext) -> None:
        tag = (self.tag or "").strip() or str(ctx.values.get("release_tag") or "").strip()
        if not tag and ctx.new_version:
            tag = f"v{ctx.new_version}"
        if not tag:
            raise typer.BadParameter(
                "git.release_tag_push requires a 'tag' option or release version context from earlier release steps"
            )
        if _RELEASE_TAG_RE.fullmatch(tag) is None or ".." in tag:
            raise typer.BadParameter(f"Invalid release tag: {tag!r}")

        release_commit = _git_output(["git", "rev-parse", "HEAD"], cwd=ctx.cwd)
        message = self.message or f"Release {tag}"
        self._ensure_local_tag(ctx, tag, release_commit, message)

        if self._remote_tag_matches(ctx, tag):
            typer.echo(f"==> Release tag {tag} is already pushed to {self.remote}; nothing to do (idempotent resume)")
            return

        push_command = ["git", "push", "--atomic", self.remote, self.branch, tag]
        code = ctx.runner.run(push_command, cwd=ctx.cwd)
        if code != 0:
            raise CommandExecutionError(
                f"Atomic push of '{self.branch}' and tag {tag} failed", command=push_command, exit_code=code
            )
        typer.echo(f"==> Pushed '{self.branch}' and tag {tag} to {self.remote} atomically")

    def _ensure_local_tag(self, ctx: PipelineContext, tag: str, release_commit: str, message: str) -> None:
        code, existing, _err = _capture(["git", "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}"], cwd=ctx.cwd)
        if code == 0 and existing:
            existing_commit = _git_output(["git", "rev-parse", "--verify", f"{tag}^{{commit}}"], cwd=ctx.cwd)
            if existing_commit != release_commit:
                raise typer.BadParameter(
                    f"Local tag {tag} already exists and points at {existing_commit[:12]}, "
                    f"not the release commit {release_commit[:12]}; moving existing tags is not allowed"
                )
            return  # Already created by a previous attempt; safe to continue (idempotent resume).
        create_command = ["git", "tag", "-a", tag, "-m", message, release_commit]
        code = ctx.runner.run(create_command, cwd=ctx.cwd)
        if code != 0:
            raise CommandExecutionError(
                f"Failed to create annotated tag {tag} on {release_commit[:12]}",
                command=create_command,
                exit_code=code,
            )

    def _remote_tag_matches(self, ctx: PipelineContext, tag: str) -> bool:
        """True when the remote already holds exactly the local tag; raises on a conflicting remote tag."""
        code, out, _err = _capture(["git", "ls-remote", self.remote, f"refs/tags/{tag}"], cwd=ctx.cwd)
        if code != 0:
            raise typer.BadParameter(f"Could not list tags on git remote '{self.remote}'")
        remote_sha = ""
        for line in out.splitlines():
            sha, _, ref = line.partition("\t")
            if ref.strip() == f"refs/tags/{tag}":
                remote_sha = sha.strip()
                break
        if not remote_sha:
            return False
        local_sha = _git_output(["git", "rev-parse", "--verify", f"refs/tags/{tag}"], cwd=ctx.cwd)
        if remote_sha == local_sha:
            return True
        raise typer.BadParameter(
            f"Remote tag {tag} on '{self.remote}' already exists with a different target "
            f"({remote_sha[:12]}); moving existing tags is not allowed"
        )
