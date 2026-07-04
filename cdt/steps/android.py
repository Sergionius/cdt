import typer

from ..pipeline import PipelineContext
from ..platforms.android import (
    _android_aab_artifact,
    _android_apk_artifact,
    _build_android_aab_command,
    _build_android_apk_command,
)
from ..sounds import _play_fail_sound


class _AndroidBuildBase:
    artifact_kind = "aab"

    def __init__(
        self,
        profile: str = "test",
        artifact: str | None = None,
        dart_defines=None,
        flavor: str | None = None,
        target: str | None = None,
        obfuscate: bool = True,
        split_debug_info: str | None = "obfsymbols",
        no_shrink: bool = True,
        no_pub: bool = True,
        extra_args: list[str] | None = None,
        env: str | None = None,
    ):
        self.profile = env or profile
        self.artifact = artifact or self.artifact_kind
        self.dart_defines = dart_defines
        self.flavor = flavor
        self.target = target
        self.obfuscate = obfuscate
        self.split_debug_info = split_debug_info
        self.no_shrink = no_shrink
        self.no_pub = no_pub
        self.extra_args = extra_args

    def _options(self) -> dict:
        return {
            "profile": self.profile,
            "dart_defines": self.dart_defines,
            "flavor": self.flavor,
            "target": self.target,
            "obfuscate": self.obfuscate,
            "split_debug_info": self.split_debug_info,
            "no_shrink": self.no_shrink,
            "no_pub": self.no_pub,
            "extra_args": self.extra_args,
        }


class AndroidBuildAabStep(_AndroidBuildBase):
    name = "android.build_aab"
    artifact_kind = "aab"

    def run(self, ctx: PipelineContext) -> None:
        if ctx.runner.run(_build_android_aab_command(**self._options()), cwd=ctx.cwd) != 0:
            _play_fail_sound(ctx.env, ctx.cwd)
            raise typer.Exit(code=1)
        ctx.register_artifact(self.artifact, _android_aab_artifact(ctx.cwd))


class AndroidBuildApkStep(_AndroidBuildBase):
    name = "android.build_apk"
    artifact_kind = "apk"

    def run(self, ctx: PipelineContext) -> None:
        if ctx.runner.run(_build_android_apk_command(**self._options()), cwd=ctx.cwd) != 0:
            _play_fail_sound(ctx.env, ctx.cwd)
            raise typer.Exit(code=1)
        ctx.register_artifact(self.artifact, _android_apk_artifact(ctx.cwd))
