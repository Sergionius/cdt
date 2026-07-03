import plistlib
import shutil
import subprocess
from pathlib import Path

import typer

from ..artifacts import ArtifactKind, BuildArtifact
from ..runner import _run
from ..sounds import _play_fail_sound


def _env_value(env: dict[str, str], key: str, fallback_key: str | None = None, default: str = "") -> str:
    value = env.get(key, "").strip()
    if value:
        return value
    if fallback_key:
        value = env.get(fallback_key, "").strip()
        if value:
            return value
    return default


def _resolve_project_path(cwd: Path, path_raw: str) -> Path:
    p = Path(path_raw).expanduser()
    if not p.is_absolute():
        p = cwd / p
    return p


def _resolve_marketing_version(cwd: Path, env: dict[str, str], scheme: str) -> str:
    explicit = _env_value(env, "IOS_MARKETING_VERSION", "NATIVE_IOS_MARKETING_VERSION")
    if explicit:
        return explicit

    configuration = _env_value(env, "IOS_CONFIGURATION", "NATIVE_IOS_CONFIGURATION", "Release") or "Release"
    workspace_raw = _env_value(env, "IOS_WORKSPACE", "NATIVE_IOS_WORKSPACE", "ios/Runner.xcworkspace")
    project_raw = _env_value(env, "IOS_PROJECT", "NATIVE_IOS_PROJECT")

    workspace = _resolve_project_path(cwd, workspace_raw)
    project = _resolve_project_path(cwd, project_raw) if project_raw else None

    cmd = ["xcodebuild", "-showBuildSettings", "-scheme", scheme, "-configuration", configuration]
    if workspace.exists():
        cmd.extend(["-workspace", str(workspace)])
    elif project and project.exists():
        cmd.extend(["-project", str(project)])
    else:
        raise typer.BadParameter(
            "Neither workspace nor project exists for resolving MARKETING_VERSION. "
            "Set IOS_WORKSPACE or IOS_PROJECT in .env"
        )

    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise typer.BadParameter("xcodebuild is not available") from exc

    if proc.returncode != 0:
        raise typer.BadParameter(f"Failed to read build settings for MARKETING_VERSION: {proc.stderr[:500]}")

    for line in proc.stdout.splitlines():
        s = line.strip()
        if s.startswith("MARKETING_VERSION ="):
            value = s.split("=", 1)[1].strip()
            if value:
                return value

    raise typer.BadParameter(
        "MARKETING_VERSION not found in build settings. "
        "Set IOS_MARKETING_VERSION explicitly in .env"
    )


def _increment_ios_build_number(cwd: Path, env: dict[str, str], scheme: str) -> tuple[str, str]:
    info_plist_raw = _env_value(env, "IOS_INFO_PLIST", "NATIVE_IOS_INFO_PLIST")
    if not info_plist_raw:
        raise typer.BadParameter("Missing IOS_INFO_PLIST in project .env")

    info_plist = _resolve_project_path(cwd, info_plist_raw)
    if not info_plist.exists():
        raise typer.BadParameter(f"Info.plist not found: {info_plist}")

    with info_plist.open("rb") as f:
        data = plistlib.load(f)

    short_raw = str(data.get("CFBundleShortVersionString", "")).strip()
    if short_raw.startswith("$(") and short_raw.endswith(")"):
        short = _resolve_marketing_version(cwd, env, scheme)
    else:
        short = short_raw or _resolve_marketing_version(cwd, env, scheme)

    build_raw = str(data.get("CFBundleVersion", "")).strip()
    if not build_raw:
        raise typer.BadParameter(f"CFBundleVersion is missing in Info.plist: {info_plist}")

    try:
        build_num = int(build_raw)
    except ValueError as exc:
        raise typer.BadParameter(f"CFBundleVersion must be integer, got: {build_raw}") from exc

    old_version = f"{short}+{build_num}"
    new_build = str(build_num + 1)
    data["CFBundleVersion"] = new_build
    new_version = f"{short}+{new_build}"

    with info_plist.open("wb") as f:
        plistlib.dump(data, f, sort_keys=False)

    return old_version, new_version


def _resolve_export_options_plist(cwd: Path, env: dict[str, str], xcode_dir: Path) -> Path:
    export_plist_raw = _env_value(env, "IOS_EXPORT_OPTIONS_PLIST", "NATIVE_IOS_EXPORT_OPTIONS_PLIST")
    if export_plist_raw:
        export_plist = _resolve_project_path(cwd, export_plist_raw)
        if export_plist.exists():
            return export_plist
        typer.echo(f"⚠️ ExportOptions.plist not found at {export_plist}, generating temporary one")

    export_method = _env_value(env, "IOS_EXPORT_METHOD", "NATIVE_IOS_EXPORT_METHOD", "app-store") or "app-store"
    signing_style = _env_value(env, "IOS_SIGNING_STYLE", "NATIVE_IOS_SIGNING_STYLE", "automatic") or "automatic"
    team_id = _env_value(env, "IOS_TEAM_ID", "NATIVE_IOS_TEAM_ID")

    export_data: dict[str, object] = {
        "method": export_method,
        "signingStyle": signing_style,
        "uploadSymbols": True,
        "compileBitcode": False,
    }
    if team_id:
        export_data["teamID"] = team_id

    tmp_plist = xcode_dir / "ExportOptions.auto.plist"
    with tmp_plist.open("wb") as f:
        plistlib.dump(export_data, f, sort_keys=False)

    typer.echo(f"==> Generated temporary ExportOptions.plist: {tmp_plist}")
    return tmp_plist


def _ios_xcode_build_ipa(cwd: Path, env: dict[str, str], scheme: str) -> Path:
    if not scheme:
        raise typer.BadParameter("Scheme is empty for iOS build")

    configuration = _env_value(env, "IOS_CONFIGURATION", "NATIVE_IOS_CONFIGURATION", "Release") or "Release"
    workspace_raw = _env_value(env, "IOS_WORKSPACE", "NATIVE_IOS_WORKSPACE", "ios/Runner.xcworkspace")
    project_raw = _env_value(env, "IOS_PROJECT", "NATIVE_IOS_PROJECT")

    workspace = _resolve_project_path(cwd, workspace_raw)
    project = _resolve_project_path(cwd, project_raw) if project_raw else None

    xcode_dir = cwd / "build" / "ios" / scheme
    archive_path = xcode_dir / f"{scheme}.xcarchive"
    export_dir = xcode_dir / "export"
    xcode_dir.mkdir(parents=True, exist_ok=True)

    if archive_path.exists():
        shutil.rmtree(archive_path, ignore_errors=True)
    if export_dir.exists():
        shutil.rmtree(export_dir, ignore_errors=True)

    export_plist = _resolve_export_options_plist(cwd, env, xcode_dir)

    archive_cmd = [
        "xcodebuild",
        "archive",
        "-scheme",
        scheme,
        "-configuration",
        configuration,
        "-archivePath",
        str(archive_path),
    ]
    if workspace.exists():
        archive_cmd.extend(["-workspace", str(workspace)])
    elif project and project.exists():
        archive_cmd.extend(["-project", str(project)])
    else:
        raise typer.BadParameter(
            "Neither workspace nor project exists for iOS build. "
            "Set IOS_WORKSPACE or IOS_PROJECT in .env"
        )

    if _run(archive_cmd, cwd=cwd) != 0:
        _play_fail_sound(env, cwd)
        raise typer.Exit(code=1)

    export_cmd = [
        "xcodebuild",
        "-exportArchive",
        "-archivePath",
        str(archive_path),
        "-exportOptionsPlist",
        str(export_plist),
        "-exportPath",
        str(export_dir),
    ]
    if _run(export_cmd, cwd=cwd) != 0:
        _play_fail_sound(env, cwd)
        raise typer.Exit(code=1)

    ipas = sorted(export_dir.glob("*.ipa"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not ipas:
        raise typer.BadParameter(f"No .ipa found after iOS export in: {export_dir}")
    return ipas[0]


def _ios_xcode_ipa_artifact(ipa_path: Path) -> BuildArtifact:
    return BuildArtifact(
        kind=ArtifactKind.IPA,
        path=ipa_path,
        label="iOS Xcode IPA",
    )
