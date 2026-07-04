import typer

from ..pipeline import PipelineContext
from ..platforms.ios_flutter import _build_ios_prod_ipa_command, _build_ios_test_ipa_command, _ios_ipa_artifact
from ..platforms.ios_xcode import (
    _increment_ios_build_number,
    _ios_xcode_build_ipa,
    _ios_xcode_ipa_artifact,
)
from ..sounds import _play_fail_sound


class IncrementIosBuildNumberStep:
    name = "ios.bump_xcode_build_number"

    def __init__(self, scheme_key: str = "IOS_TEST_SCHEME", fallback_scheme_key: str = "NATIVE_TEST_SCHEME"):
        self.scheme_key = scheme_key
        self.fallback_scheme_key = fallback_scheme_key

    def run(self, ctx: PipelineContext) -> None:
        scheme = ctx.require_env(self.scheme_key, self.fallback_scheme_key)
        ctx.values["ios_scheme"] = scheme
        ctx.old_version, ctx.new_version = _increment_ios_build_number(ctx.cwd, ctx.env, scheme)
        typer.echo(f"==> Info.plist version bumped: {ctx.old_version} -> {ctx.new_version}")


class IosXcodeBuildIpaStep:
    name = "ios.xcode_build_ipa"

    def __init__(self, mode: str = "test", artifact: str = "ipa"):
        self.mode = mode
        self.artifact = artifact

    def run(self, ctx: PipelineContext) -> None:
        scheme = ctx.values.get("ios_scheme", "").strip()
        if not scheme:
            raise typer.BadParameter("Missing pipeline value: ios_scheme")

        typer.echo(f"==> iOS {self.mode} build with scheme: {scheme}")
        ipa = _ios_xcode_build_ipa(ctx.cwd, ctx.env, scheme)
        ctx.register_artifact(self.artifact, _ios_xcode_ipa_artifact(ipa))


class IosFlutterBuildIpaStep:
    name = "ios.flutter_build_ipa"

    def __init__(
        self,
        profile: str = "test",
        artifact: str = "ipa",
        dart_defines=None,
        flavor: str | None = None,
        target: str | None = None,
        obfuscate: bool = True,
        split_debug_info: str | None = "obfsymbols",
        no_pub: bool = True,
        extra_args: list[str] | None = None,
    ):
        self.profile = profile
        self.artifact = artifact
        self.dart_defines = dart_defines
        self.flavor = flavor
        self.target = target
        self.obfuscate = obfuscate
        self.split_debug_info = split_debug_info
        self.no_pub = no_pub
        self.extra_args = extra_args

    def run(self, ctx: PipelineContext) -> None:
        options = {
            "dart_defines": self.dart_defines,
            "flavor": self.flavor,
            "target": self.target,
            "obfuscate": self.obfuscate,
            "split_debug_info": self.split_debug_info,
            "no_pub": self.no_pub,
            "extra_args": self.extra_args,
        }
        command = (
            _build_ios_prod_ipa_command(**options)
            if self.profile == "prod"
            else _build_ios_test_ipa_command(**options)
        )
        if ctx.runner.run(command, cwd=ctx.cwd) != 0:
            _play_fail_sound(ctx.env, ctx.cwd)
            raise typer.Exit(code=1)
        ctx.register_artifact(self.artifact, _ios_ipa_artifact(ctx.cwd))
