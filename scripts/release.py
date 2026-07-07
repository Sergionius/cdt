#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import subprocess
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare a CDT GitHub release")
    parser.add_argument("version", help="Version without leading v, e.g. 0.3.3")
    parser.add_argument("--dry-run", action="store_true", help="Print planned actions without changing files")
    args = parser.parse_args()

    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[.-][A-Za-z0-9]+)?", args.version):
        parser.error("version must look like 0.3.3")
    tag = f"v{args.version}"

    changes = [
        (ROOT / "pyproject.toml", r'version = "[^"]+"', f'version = "{args.version}"'),
        (ROOT / "cdt" / "__init__.py", r'__version__ = "[^"]+"', f'__version__ = "{args.version}"'),
    ]

    if args.dry_run:
        print(f"Would bump version to {args.version}")
        print(f"Would prepend CHANGELOG section {tag}")
        print("Would run: ruff check ., pytest, python -m build")
        print(f"Would commit and create annotated tag {tag}, then push commit and tag")
        return 0

    for path, pattern, replacement in changes:
        text = path.read_text(encoding="utf-8")
        path.write_text(re.sub(pattern, replacement, text, count=1), encoding="utf-8")

    changelog = ROOT / "CHANGELOG.md"
    text = changelog.read_text(encoding="utf-8")
    if f"## {tag}" not in text:
        section = f"## Unreleased\n\n## {tag} - {date.today().isoformat()}\n\n- TODO: summarize changes.\n"
        text = text.replace("## Unreleased\n", section, 1)
        changelog.write_text(text, encoding="utf-8")

    run(["ruff", "check", "."])
    run(["pytest"])
    run(["python", "-m", "build"])

    run(["git", "diff", "--", "pyproject.toml", "cdt/__init__.py", "CHANGELOG.md"])
    run(["git", "add", "pyproject.toml", "cdt/__init__.py", "CHANGELOG.md"])
    run(["git", "commit", "-m", f"Release {tag}"])
    run(["git", "tag", "-a", tag, "-m", f"Release {tag}"])
    run(["git", "push", "origin", "HEAD"])
    run(["git", "push", "origin", tag])
    return 0


def run(command: list[str]) -> None:
    subprocess.run(command, cwd=ROOT, check=True)


if __name__ == "__main__":
    raise SystemExit(main())
