import re
from pathlib import Path

import typer

from ..artifacts import ArtifactKind, BuildArtifact


def _build_flutter_web_command(*, env_name: str | None = None) -> list[str]:
    command = ["flutter", "build", "web", "--release"]
    if env_name:
        command.append(f"--dart-define=ENV={env_name}")
    return command


def _apply_web_cache_busting(web_place: Path, build_number: str) -> None:
    index_html = web_place / "index.html"
    flutter_bootstrap = web_place / "flutter_bootstrap.js"

    if not index_html.exists():
        raise typer.BadParameter(f"index.html not found: {index_html}")
    if not flutter_bootstrap.exists():
        raise typer.BadParameter(f"flutter_bootstrap.js not found: {flutter_bootstrap}")

    index_content = index_html.read_text(encoding="utf-8")
    index_content = re.sub(
        r'flutter_bootstrap\.js(?:\?v=[^"]*)?',
        f"flutter_bootstrap.js?v={build_number}",
        index_content,
    )
    index_content = re.sub(
        r'\n\s*<script defer src="main\.dart\.js(?:\?v=[^"]*)?" type="application/javascript"></script>',
        "",
        index_content,
    )
    index_html.write_text(index_content, encoding="utf-8")

    _apply_flutter_bootstrap_cache_busting(flutter_bootstrap, build_number)


def _apply_flutter_bootstrap_cache_busting(flutter_bootstrap: Path, build_number: str) -> None:
    bootstrap_content = flutter_bootstrap.read_text(encoding="utf-8")
    bootstrap_content = re.sub(
        r'main\.dart\.js(?:\?v=[^"]*)?',
        f"main.dart.js?v={build_number}",
        bootstrap_content,
    )
    flutter_bootstrap.write_text(bootstrap_content, encoding="utf-8")


def _apply_web_inner_cache_busting(build_web: Path, web_place: Path, build_number: str) -> None:
    index_inner = build_web / "index-inner.html"
    flutter_bootstrap = build_web / "flutter_bootstrap.js"
    index_wrapper = web_place / "index-wrapper.html"

    if not index_inner.exists():
        raise typer.BadParameter(f"index-inner.html not found: {index_inner}")
    if not flutter_bootstrap.exists():
        raise typer.BadParameter(f"flutter_bootstrap.js not found: {flutter_bootstrap}")
    if not index_wrapper.exists():
        raise typer.BadParameter(f"index-wrapper.html not found: {index_wrapper}")

    inner_content = index_inner.read_text(encoding="utf-8")
    inner_content = re.sub(
        r'flutter_bootstrap\.js(?:\?v=[^"]*)?',
        f"flutter_bootstrap.js?v={build_number}",
        inner_content,
    )
    inner_content = re.sub(
        r'\n\s*<script defer src="main\.dart\.js(?:\?v=[^"]*)?" type="application/javascript"></script>',
        "",
        inner_content,
    )
    index_inner.write_text(inner_content, encoding="utf-8")

    _apply_flutter_bootstrap_cache_busting(flutter_bootstrap, build_number)

    wrapper_content = index_wrapper.read_text(encoding="utf-8")
    wrapper_content = re.sub(
        r'index-inner\.html(?:\?v=[^"]*)?',
        f"index-inner.html?v={build_number}",
        wrapper_content,
    )
    index_wrapper.write_text(wrapper_content, encoding="utf-8")


def _rename_web_inner_index(build_web: Path) -> None:
    index_path = build_web / "index.html"
    if not index_path.exists():
        raise typer.BadParameter(f"index.html not found for WEB_INNER rename: {index_path}")
    inner_path = build_web / "index-inner.html"
    if inner_path.exists():
        inner_path.unlink()
    index_path.rename(inner_path)


def _web_artifact(build_web: Path) -> BuildArtifact:
    if not build_web.is_dir():
        raise typer.BadParameter(f"Web build output not found: {build_web}")
    return BuildArtifact(
        kind=ArtifactKind.WEB,
        path=build_web,
        label="Flutter Web",
    )
