from pathlib import Path

import typer

from ..artifacts import ArtifactKind, BuildArtifact
from .flutter_build import flutter_build_options, merge_dart_defines


def _profile_defines(profile: str | None) -> dict[str, str]:
    if profile and profile != "test":
        return {"ENV": profile}
    return {}


def _build_ios_ipa_command(
    *,
    profile: str = "test",
    dart_defines=None,
    flavor: str | None = None,
    target: str | None = None,
    obfuscate: bool = True,
    split_debug_info: str | None = "obfsymbols",
    no_pub: bool = True,
    extra_args: list[str] | None = None,
) -> list[str]:
    return [
        "flutter",
        "build",
        "ipa",
        *flutter_build_options(
            dart_defines=merge_dart_defines(_profile_defines(profile), dart_defines),
            flavor=flavor,
            target=target,
            obfuscate=obfuscate,
            split_debug_info=split_debug_info,
            no_pub=no_pub,
            extra_args=extra_args,
        ),
    ]


def _build_ios_test_ipa_command(**kwargs) -> list[str]:
    return _build_ios_ipa_command(profile="test", **kwargs)


def _build_ios_prod_ipa_command(**kwargs) -> list[str]:
    return _build_ios_ipa_command(profile="prod", **kwargs)


def _find_ipa(project_root: Path) -> Path:
    ipa_dir = project_root / "build" / "ios" / "ipa"
    if not ipa_dir.is_dir():
        raise typer.BadParameter(f"IPA output directory not found: {ipa_dir}")

    ipas = sorted(ipa_dir.glob("*.ipa"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not ipas:
        raise typer.BadParameter(f"No .ipa files found in: {ipa_dir}")
    return ipas[0]


def _ios_ipa_artifact(project_root: Path) -> BuildArtifact:
    return BuildArtifact(
        kind=ArtifactKind.IPA,
        path=_find_ipa(project_root),
        label="iOS IPA",
    )
