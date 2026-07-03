import typer

from ..pipeline import PipelineContext
from ..sounds import _play_fail_sound
from ..versioning import _increment_flutter_build_number


class FlutterPubGetStep:
    name = "flutter.pub_get"

    def run(self, ctx: PipelineContext) -> None:
        if ctx.runner.run(["flutter", "pub", "get"], cwd=ctx.cwd) != 0:
            _play_fail_sound(ctx.env, ctx.cwd)
            raise typer.Exit(code=1)


def ensure_flutter_build_number(ctx: PipelineContext) -> None:
    if ctx.new_version:
        return
    ctx.old_version, ctx.new_version = _increment_flutter_build_number(ctx.cwd)
    typer.echo(f"==> pubspec version bumped: {ctx.old_version} -> {ctx.new_version}")
