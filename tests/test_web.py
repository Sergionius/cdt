import pytest
import typer

from cdt.artifacts import ArtifactKind
from cdt.platforms.web import (
    _apply_web_cache_busting,
    _apply_web_inner_cache_busting,
    _build_flutter_web_command,
    _rename_web_inner_index,
    _web_artifact,
)


def test_apply_web_cache_busting_updates_bootstrap_and_removes_main_script(tmp_path):
    (tmp_path / "index.html").write_text(
        '<html>\n'
        '<script src="flutter_bootstrap.js"></script>\n'
        '<script defer src="main.dart.js" type="application/javascript"></script>\n'
        "</html>\n",
        encoding="utf-8",
    )
    (tmp_path / "flutter_bootstrap.js").write_text('load("main.dart.js");', encoding="utf-8")

    _apply_web_cache_busting(tmp_path, "11")

    index = (tmp_path / "index.html").read_text(encoding="utf-8")
    bootstrap = (tmp_path / "flutter_bootstrap.js").read_text(encoding="utf-8")
    assert 'flutter_bootstrap.js?v=11' in index
    assert 'script defer src="main.dart.js"' not in index
    assert "main.dart.js?v=11" in bootstrap


def test_web_inner_renames_index_and_updates_wrapper(tmp_path):
    (tmp_path / "index.html").write_text(
        '<script src="flutter_bootstrap.js?v=1"></script>\n'
        '<script defer src="main.dart.js?v=1" type="application/javascript"></script>\n',
        encoding="utf-8",
    )
    (tmp_path / "flutter_bootstrap.js").write_text('load("main.dart.js?v=1");', encoding="utf-8")
    (tmp_path / "index-wrapper.html").write_text('<iframe src="index-inner.html?v=1"></iframe>', encoding="utf-8")

    _rename_web_inner_index(tmp_path)
    _apply_web_inner_cache_busting(tmp_path, tmp_path, "12")

    assert not (tmp_path / "index.html").exists()
    assert (tmp_path / "index-inner.html").exists()
    inner = (tmp_path / "index-inner.html").read_text(encoding="utf-8")
    wrapper = (tmp_path / "index-wrapper.html").read_text(encoding="utf-8")
    assert "flutter_bootstrap.js?v=12" in inner
    assert 'script defer src="main.dart.js' not in inner
    assert "index-inner.html?v=12" in wrapper


def test_build_flutter_web_command_for_dev_and_prod():
    assert _build_flutter_web_command() == ["flutter", "build", "web", "--release"]
    assert _build_flutter_web_command(env_name="prod") == [
        "flutter",
        "build",
        "web",
        "--release",
        "--dart-define=ENV=prod",
    ]


def test_web_artifact_wraps_build_directory(tmp_path):
    build_web = tmp_path / "build" / "web"
    build_web.mkdir(parents=True)

    artifact = _web_artifact(build_web)

    assert artifact.kind == ArtifactKind.WEB
    assert artifact.path == build_web
    assert artifact.label == "Flutter Web"


def test_web_artifact_errors_when_build_directory_missing(tmp_path):
    with pytest.raises(typer.BadParameter, match="Web build output not found"):
        _web_artifact(tmp_path / "build" / "web")
