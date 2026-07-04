import shutil
from pathlib import Path

import typer

from ..artifacts import ArtifactKind, BuildArtifact
from .flutter_build import flutter_build_options, merge_dart_defines


def _profile_defines(profile: str | None) -> dict[str, str]:
    if profile == "prod":
        return {"ENV": "prod"}
    if profile and profile != "test":
        return {"ENV": profile}
    return {}


def _build_android_aab_command(
    *,
    profile: str = "test",
    dart_defines=None,
    flavor: str | None = None,
    target: str | None = None,
    obfuscate: bool = True,
    split_debug_info: str | None = "obfsymbols",
    no_shrink: bool = True,
    no_pub: bool = True,
    extra_args: list[str] | None = None,
) -> list[str]:
    return [
        "flutter",
        "build",
        "appbundle",
        *flutter_build_options(
            dart_defines=merge_dart_defines(_profile_defines(profile), dart_defines),
            flavor=flavor,
            target=target,
            obfuscate=obfuscate,
            split_debug_info=split_debug_info,
            no_shrink=no_shrink,
            no_pub=no_pub,
            extra_args=extra_args,
        ),
    ]


def _build_android_test_aab_command(**kwargs) -> list[str]:
    return _build_android_aab_command(profile="test", **kwargs)


def _build_android_apk_command(
    *,
    profile: str = "test",
    dart_defines=None,
    flavor: str | None = None,
    target: str | None = None,
    obfuscate: bool = True,
    split_debug_info: str | None = "obfsymbols",
    no_shrink: bool = True,
    no_pub: bool = True,
    extra_args: list[str] | None = None,
) -> list[str]:
    return [
        "flutter",
        "build",
        "apk",
        *flutter_build_options(
            dart_defines=merge_dart_defines(_profile_defines(profile), dart_defines),
            flavor=flavor,
            target=target,
            obfuscate=obfuscate,
            split_debug_info=split_debug_info,
            no_shrink=no_shrink,
            no_pub=no_pub,
            extra_args=extra_args,
        ),
    ]


def _build_android_prod_apk_command(**kwargs) -> list[str]:
    # Legacy flow helper keeps historical STORE=ru. Pipeline built-in android.build_apk does not.
    dart_defines = merge_dart_defines({"STORE": "ru"}, kwargs.pop("dart_defines", None))
    return _build_android_apk_command(profile="prod", dart_defines=dart_defines, **kwargs)


def _build_android_prod_aab_command(**kwargs) -> list[str]:
    return _build_android_aab_command(profile="prod", **kwargs)


def _find_android_aab(project_root: Path) -> Path:
    aab_dir = project_root / "build" / "app" / "outputs" / "bundle" / "release"
    if not aab_dir.is_dir():
        raise typer.BadParameter(f"Android AAB output directory not found: {aab_dir}")

    aabs = sorted(aab_dir.glob("*.aab"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not aabs:
        raise typer.BadParameter(f"No .aab files found in: {aab_dir}")
    return aabs[0]


def _find_android_apk(project_root: Path) -> Path:
    apk_path = project_root / "build" / "app" / "outputs" / "flutter-apk" / "app-release.apk"
    if not apk_path.exists():
        raise typer.BadParameter(f"Android APK not found: {apk_path}")
    return apk_path


def _android_aab_artifact(project_root: Path) -> BuildArtifact:
    return BuildArtifact(
        kind=ArtifactKind.AAB,
        path=_find_android_aab(project_root),
        label="Android AAB",
    )


def _android_apk_artifact(project_root: Path) -> BuildArtifact:
    return BuildArtifact(
        kind=ArtifactKind.APK,
        path=_find_android_apk(project_root),
        label="Android APK",
    )


def _copy_to_downloads(paths: list[Path], downloads_dir: Path | None = None) -> None:
    downloads = downloads_dir or Path.home() / "Downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    for src in paths:
        dst = downloads / src.name
        shutil.copy2(src, dst)
        typer.echo(f"==> Copied to Downloads: {dst}")


def copy_artifacts_to_downloads(
    artifacts: list[BuildArtifact],
    downloads_dir: Path | None = None,
) -> None:
    _copy_to_downloads([artifact.path for artifact in artifacts], downloads_dir=downloads_dir)
