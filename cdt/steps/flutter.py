import typer

from ..pipeline import PipelineContext
from ..sounds import _play_fail_sound
from ..versioning import _increment_flutter_build_number


class IncrementFlutterBuildNumberStep:
    name = "flutter.increment_build_number"

    def run(self, ctx: PipelineContext) -> None:
        ctx.old_version, ctx.new_version = _increment_flutter_build_number(ctx.cwd)
        ctx.values["flutter.version.old"] = ctx.old_version
        ctx.values["flutter.version"] = ctx.new_version
        ctx.values["flutter.build_number"] = ctx.new_version.rsplit("+", 1)[1]
        # Backwards-compatible aliases for older pipelines/tests.
        ctx.values["flutter_version_old"] = ctx.old_version
        ctx.values["flutter_version"] = ctx.new_version
        typer.echo(f"==> pubspec version bumped: {ctx.old_version} -> {ctx.new_version}")


class FlutterPubGetStep:
    name = "flutter.pub_get"

    def run(self, ctx: PipelineContext) -> None:
        if ctx.runner.run(["flutter", "pub", "get"], cwd=ctx.cwd) != 0:
            _play_fail_sound(ctx.env, ctx.cwd)
            raise typer.Exit(code=1)


def ensure_flutter_build_number(ctx: PipelineContext) -> None:
    if not ctx.new_version:
        raise typer.BadParameter(
            "Missing pipeline value: flutter.version. Add flutter.increment_build_number before build steps."
        )
