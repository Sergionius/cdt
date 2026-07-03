import plistlib
from pathlib import Path

import pytest
import typer

from cdt.artifacts import ArtifactKind
from cdt.platforms.ios_xcode import (
    _increment_ios_build_number,
    _ios_xcode_ipa_artifact,
    _resolve_export_options_plist,
    _resolve_project_path,
)


def _write_plist(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        plistlib.dump(data, f, sort_keys=False)


def _read_plist(path: Path) -> dict[str, object]:
    with path.open("rb") as f:
        return plistlib.load(f)


def test_resolve_project_path_relative_to_cwd(tmp_path):
    assert _resolve_project_path(tmp_path, "ios/App.plist") == tmp_path / "ios" / "App.plist"


def test_resolve_project_path_expands_home(tmp_path):
    resolved = _resolve_project_path(tmp_path, "~/file")

    assert resolved.is_absolute()
    assert resolved.name == "file"
    assert "~" not in str(resolved)


def test_resolve_export_options_plist_returns_existing_env_file(tmp_path):
    export_options = tmp_path / "ios" / "ExportOptions.plist"
    _write_plist(export_options, {"method": "app-store"})

    resolved = _resolve_export_options_plist(
        tmp_path,
        {"IOS_EXPORT_OPTIONS_PLIST": "ios/ExportOptions.plist"},
        tmp_path / "build" / "ios",
    )

    assert resolved == export_options


def test_resolve_export_options_plist_generates_file_from_env(tmp_path):
    xcode_dir = tmp_path / "build" / "ios" / "App"
    xcode_dir.mkdir(parents=True)

    resolved = _resolve_export_options_plist(
        tmp_path,
        {
            "IOS_EXPORT_METHOD": "ad-hoc",
            "IOS_SIGNING_STYLE": "manual",
            "IOS_TEAM_ID": "ABCDE12345",
        },
        xcode_dir,
    )

    assert resolved == xcode_dir / "ExportOptions.auto.plist"
    assert _read_plist(resolved) == {
        "method": "ad-hoc",
        "signingStyle": "manual",
        "uploadSymbols": True,
        "compileBitcode": False,
        "teamID": "ABCDE12345",
    }


def test_increment_ios_build_number_updates_bundle_version(tmp_path):
    info_plist = tmp_path / "ios" / "Runner" / "Info.plist"
    _write_plist(
        info_plist,
        {
            "CFBundleShortVersionString": "1.2.3",
            "CFBundleVersion": "41",
        },
    )

    old_version, new_version = _increment_ios_build_number(
        tmp_path,
        {"IOS_INFO_PLIST": "ios/Runner/Info.plist"},
        "Runner",
    )

    assert old_version == "1.2.3+41"
    assert new_version == "1.2.3+42"
    assert _read_plist(info_plist)["CFBundleVersion"] == "42"


def test_increment_ios_build_number_supports_legacy_env_key(tmp_path):
    info_plist = tmp_path / "ios" / "Runner" / "Info.plist"
    _write_plist(
        info_plist,
        {
            "CFBundleShortVersionString": "1.2.3",
            "CFBundleVersion": "41",
        },
    )

    old_version, new_version = _increment_ios_build_number(
        tmp_path,
        {"NATIVE_IOS_INFO_PLIST": "ios/Runner/Info.plist"},
        "Runner",
    )

    assert old_version == "1.2.3+41"
    assert new_version == "1.2.3+42"


def test_increment_ios_build_number_errors_without_info_plist_env(tmp_path):
    with pytest.raises(typer.BadParameter, match="Missing IOS_INFO_PLIST"):
        _increment_ios_build_number(tmp_path, {}, "Runner")


def test_increment_ios_build_number_errors_when_plist_missing(tmp_path):
    with pytest.raises(typer.BadParameter, match="Info.plist not found"):
        _increment_ios_build_number(
            tmp_path,
            {"IOS_INFO_PLIST": "ios/Runner/Info.plist"},
            "Runner",
        )


def test_increment_ios_build_number_errors_when_build_is_not_integer(tmp_path):
    info_plist = tmp_path / "ios" / "Runner" / "Info.plist"
    _write_plist(
        info_plist,
        {
            "CFBundleShortVersionString": "1.2.3",
            "CFBundleVersion": "beta",
        },
    )

    with pytest.raises(typer.BadParameter, match="CFBundleVersion must be integer"):
        _increment_ios_build_number(
            tmp_path,
            {"IOS_INFO_PLIST": "ios/Runner/Info.plist"},
            "Runner",
        )


def test_ios_xcode_ipa_artifact_wraps_path(tmp_path):
    ipa = tmp_path / "build" / "ios" / "App" / "export" / "App.ipa"

    artifact = _ios_xcode_ipa_artifact(ipa)

    assert artifact.kind == ArtifactKind.IPA
    assert artifact.path == ipa
    assert artifact.label == "iOS Xcode IPA"
