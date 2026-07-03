from pathlib import Path

import typer

from ..pipeline import PipelineContext
from ..runner import _prepare_git_clean_main
from ..sounds import _play_fail_sound
from ..versioning import _current_flutter_version


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
