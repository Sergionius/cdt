import pytest
import typer

from cdt.versioning import _current_flutter_version, _flutter_build_number, _increment_flutter_build_number


def test_increment_flutter_build_number(tmp_path):
    pubspec = tmp_path / "pubspec.yaml"
    pubspec.write_text("name: app\nversion: 1.2.3+10\n", encoding="utf-8")

    old_version, new_version = _increment_flutter_build_number(tmp_path)

    assert old_version == "1.2.3+10"
    assert new_version == "1.2.3+11"
    assert pubspec.read_text(encoding="utf-8") == "name: app\nversion: 1.2.3+11\n"


def test_current_flutter_version_errors_without_pubspec(tmp_path):
    with pytest.raises(typer.BadParameter, match="pubspec.yaml not found"):
        _current_flutter_version(tmp_path)


def test_current_flutter_version_errors_without_version_field(tmp_path):
    (tmp_path / "pubspec.yaml").write_text("name: app\n", encoding="utf-8")

    with pytest.raises(typer.BadParameter, match="No 'version:' field found in pubspec.yaml"):
        _current_flutter_version(tmp_path)


def test_increment_flutter_build_number_errors_without_build_suffix(tmp_path):
    (tmp_path / "pubspec.yaml").write_text("name: app\nversion: 1.2.3\n", encoding="utf-8")

    with pytest.raises(typer.BadParameter, match="pubspec version must include build suffix"):
        _increment_flutter_build_number(tmp_path)


def test_flutter_build_number_errors_without_build_suffix():
    with pytest.raises(typer.BadParameter, match="Flutter version has no build number"):
        _flutter_build_number("1.2.3")
