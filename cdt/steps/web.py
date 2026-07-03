import shutil
from pathlib import Path

import typer

from ..pipeline import PipelineContext
from ..platforms.web import (
    _apply_web_cache_busting,
    _apply_web_inner_cache_busting,
    _build_flutter_web_command,
    _rename_web_inner_index,
    _web_artifact,
)
from ..sounds import _play_fail_sound
from ..versioning import _current_flutter_version, _flutter_build_number


class ResolveWebDeployPathsStep:
    name = "web.resolve_deploy_paths"

    def run(self, ctx: PipelineContext) -> None:
        web_repo_raw = ctx.env.get("WEB_REPOSITORY", "").strip()
        web_place_raw = ctx.env.get("WEB_BUILD_PLACE", "").strip()
        if not web_repo_raw or not web_place_raw:
            raise typer.BadParameter("Missing WEB_REPOSITORY or WEB_BUILD_PLACE in project .env")

        web_repo = _project_path(ctx.cwd, web_repo_raw)
        web_place = _project_path(ctx.cwd, web_place_raw)

        if not web_repo.is_dir():
            raise typer.BadParameter(f"WEB_REPOSITORY is not a directory: {web_repo}")

        ctx.values["web_repo"] = str(web_repo)
        ctx.values["web_place"] = str(web_place)


class BuildFlutterWebStep:
    name = "web.build"

    def __init__(self, env_name: str | None = None, env: str | None = None):
        self.env_name = env_name if env_name is not None else env

    def run(self, ctx: PipelineContext) -> None:
        build_cmd = _build_flutter_web_command(env_name=self.env_name)
        if ctx.runner.run(build_cmd, cwd=ctx.cwd) != 0:
            _play_fail_sound(ctx.env, ctx.cwd)
            raise typer.Exit(code=1)


class CopyWebBuildStep:
    name = "web.copy"

    def __init__(
        self,
        repository: str | None = None,
        destination: str | None = None,
        inner: bool | str | None = None,
    ):
        self.repository = repository
        self.destination = destination
        self.inner = inner

    def run(self, ctx: PipelineContext) -> None:
        if self.repository is not None:
            web_repo = _project_path(ctx.cwd, self.repository)
            if not web_repo.is_dir():
                raise typer.BadParameter(f"web.copy.repository is not a directory: {web_repo}")
            ctx.values["web_repo"] = str(web_repo)
        if self.destination is not None:
            ctx.values["web_place"] = str(_project_path(ctx.cwd, self.destination))

        artifact = _web_artifact(ctx.cwd / "build" / "web")
        ctx.register_artifact("web", artifact)
        build_web = artifact.path
        try:
            web_place = Path(ctx.values["web_place"])
        except KeyError as exc:
            raise typer.BadParameter("Missing web.copy.destination or pipeline value: web_place") from exc

        if _is_truthy(self.inner, ctx.env.get("WEB_INNER", "")):
            _rename_web_inner_index(build_web)
            typer.echo("==> WEB_INNER=true: renamed index.html -> index-inner.html")

        web_place.mkdir(parents=True, exist_ok=True)
        shutil.copytree(build_web, web_place, dirs_exist_ok=True)
        typer.echo(f"==> Copied web build to: {web_place}")


class ApplyWebCacheBustingStep:
    name = "web.cache_bust"

    def __init__(self, destination: str | None = None, inner: bool | str | None = None):
        self.destination = destination
        self.inner = inner

    def run(self, ctx: PipelineContext) -> None:
        if self.destination is not None:
            ctx.values["web_place"] = str(_project_path(ctx.cwd, self.destination))
        try:
            web_place = Path(ctx.values["web_place"])
        except KeyError as exc:
            raise typer.BadParameter("Missing web.cache_bust.destination or pipeline value: web_place") from exc
        version = _current_flutter_version(ctx.cwd)
        build_number = _flutter_build_number(version)
        ctx.values["flutter_version"] = version

        if _is_truthy(self.inner, ctx.env.get("WEB_INNER", "")):
            _apply_web_inner_cache_busting(web_place, web_place, build_number)
            typer.echo(f"==> WEB_INNER=true: applied cache busting v={build_number}")
        else:
            _apply_web_cache_busting(web_place, build_number)
            typer.echo(f"==> Applied cache busting v={build_number}")


def _project_path(cwd: Path, raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = cwd / path
    return path


def _is_truthy(value: bool | str | None, fallback: str) -> bool:
    raw = fallback if value is None else value
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() == "true"
