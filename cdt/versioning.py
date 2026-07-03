from pathlib import Path

import typer


def _current_flutter_version(project_root: Path) -> str:
    pubspec = project_root / "pubspec.yaml"
    if not pubspec.exists():
        raise typer.BadParameter(f"pubspec.yaml not found: {pubspec}")

    lines = pubspec.read_text(encoding="utf-8").splitlines()
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("version:"):
            return stripped.split(":", 1)[1].strip()

    raise typer.BadParameter("No 'version:' field found in pubspec.yaml")


def _flutter_build_number(version: str) -> str:
    if "+" not in version:
        raise typer.BadParameter(f"Flutter version has no build number: {version}")
    build_number = version.rsplit("+", 1)[1].strip()
    if not build_number:
        raise typer.BadParameter(f"Flutter version has empty build number: {version}")
    return build_number


def _increment_flutter_build_number(project_root: Path) -> tuple[str, str]:
    pubspec = project_root / "pubspec.yaml"
    if not pubspec.exists():
        raise typer.BadParameter(f"pubspec.yaml not found: {pubspec}")

    lines = pubspec.read_text(encoding="utf-8").splitlines()
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("version:"):
            continue

        value = stripped.split(":", 1)[1].strip()
        if "+" not in value:
            raise typer.BadParameter("pubspec version must include build suffix, e.g. version: 1.1.22+419")

        base, build = value.rsplit("+", 1)
        try:
            build_num = int(build)
        except ValueError as exc:
            raise typer.BadParameter(f"Invalid build number in pubspec version: {value}") from exc

        new_value = f"{base}+{build_num + 1}"
        prefix = line[: line.index("version:")]
        lines[idx] = f"{prefix}version: {new_value}"
        pubspec.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return value, new_value

    raise typer.BadParameter("No 'version:' field found in pubspec.yaml")
