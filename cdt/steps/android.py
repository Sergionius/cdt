import typer

from ..pipeline import PipelineContext
from ..platforms.android import (
    _android_aab_artifact,
    _build_android_prod_aab_command,
    _build_android_test_aab_command,
)
from ..sounds import _play_fail_sound
from .flutter import ensure_flutter_build_number


class AndroidBuildAabStep:
    name = "android.build_aab"

    def __init__(
        self,
        env: str = "test",
        artifact: str = "aab",
        dart_defines=None,
        flavor: str | None = None,
        target: str | None = None,
        obfuscate: bool = True,
        split_debug_info: str | None = "obfsymbols",
        no_shrink: bool = True,
        no_pub: bool = True,
        extra_args: list[str] | None = None,
    ):
        self.env = env
        self.artifact = artifact
        self.dart_defines = dart_defines
        self.flavor = flavor
        self.target = target
        self.obfuscate = obfuscate
        self.split_debug_info = split_debug_info
        self.no_shrink = no_shrink
        self.no_pub = no_pub
        self.extra_args = extra_args

    def run(self, ctx: PipelineContext) -> None:
        ensure_flutter_build_number(ctx)
        options = {
            "dart_defines": self.dart_defines,
            "flavor": self.flavor,
            "target": self.target,
            "obfuscate": self.obfuscate,
            "split_debug_info": self.split_debug_info,
            "no_shrink": self.no_shrink,
            "no_pub": self.no_pub,
            "extra_args": self.extra_args,
        }
        command = (
            _build_android_prod_aab_command(**options)
            if self.env == "prod"
            else _build_android_test_aab_command(**options)
        )
        if ctx.runner.run(command, cwd=ctx.cwd) != 0:
            _play_fail_sound(ctx.env, ctx.cwd)
            raise typer.Exit(code=1)
        ctx.register_artifact(self.artifact, _android_aab_artifact(ctx.cwd))
