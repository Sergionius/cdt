#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE_TAG_EXAMPLE_FILES = (
    ROOT / "README.md",
    ROOT / "docs" / "getting-started.md",
)
GITHUB_TAG_INSTALL_RE = re.compile(
    r'(git\+https://github\.com/Sergionius/cdt\.git@)v\d+\.\d+\.\d+(?:[.-][A-Za-z0-9]+)?'
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare a CDT release")
    parser.add_argument("version", help="Version without leading v, e.g. 0.3.3")
    parser.add_argument("--dry-run", action="store_true", help="Print planned actions without changing files")
    parser.add_argument(
        "--push",
        action="store_true",
        help="Push the release commit and tag after preparing them. Use only after explicit confirmation.",
    )
    args = parser.parse_args()

    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[.-][A-Za-z0-9]+)?", args.version):
        parser.error("version must look like 0.3.3")
    tag = f"v{args.version}"
    branch = current_branch() if args.push else None

    changes = [
        (ROOT / "pyproject.toml", r'version = "[^"]+"', f'version = "{args.version}"'),
        (ROOT / "cdt" / "__init__.py", r'__version__ = "[^"]+"', f'__version__ = "{args.version}"'),
    ]

    if args.dry_run:
        print(f"Would bump version to {args.version}")
        print(f"Would prepend CHANGELOG section {tag}")
        print("Would update README/docs GitHub install examples")
        print("Would run: ruff check ., pytest -q, clean dist, python -m build")
        print(f"Would commit and create annotated tag {tag}")
        if args.push:
            print(f"Would run: git pull --rebase origin {branch} after committing and before tagging")
            print(f"Would push commit and tag {tag}")
        else:
            print("Would not push. Re-run with --push after explicit confirmation to publish the release.")
        return 0

    for path, pattern, replacement in changes:
        text = path.read_text(encoding="utf-8")
        path.write_text(re.sub(pattern, replacement, text, count=1), encoding="utf-8")
    update_release_tag_examples(args.version)

    changelog = ROOT / "CHANGELOG.md"
    text = changelog.read_text(encoding="utf-8")
    if f"## {tag}" not in text:
        section = (
            f"## Unreleased\n\n- Nothing yet.\n\n"
            f"## {tag} - {date.today().isoformat()}\n\n- TODO: summarize changes.\n"
        )
        text = re.sub(r"## Unreleased\n\n(?:- Nothing yet\.\n\n)?", section, text, count=1)
        changelog.write_text(text, encoding="utf-8")

    run(["ruff", "check", "."])
    run(["pytest", "-q"])
    clean_dist()
    run(["python", "-m", "build"])

    release_files = [
        "pyproject.toml",
        "cdt/__init__.py",
        "CHANGELOG.md",
        "README.md",
        "docs/getting-started.md",
    ]
    run(["git", "diff", "--", *release_files])
    run(["git", "add", *release_files])
    if has_staged_changes():
        run(["git", "commit", "-m", f"Release {tag}"])
    else:
        print("No version or changelog changes to commit; using current HEAD.")
    if args.push:
        run(["git", "pull", "--rebase", "origin", branch])
    run(["git", "tag", "-a", tag, "-m", f"Release {tag}"])
    if args.push:
        run(["git", "push", "origin", "HEAD"])
        run(["git", "push", "origin", tag])
    else:
        print(f"Prepared local release commit and tag {tag}.")
        print("Nothing was pushed. Re-run with --push after explicit confirmation to publish the release.")
    return 0


def update_release_tag_examples(version: str) -> None:
    tag = f"v{version}"
    for path in RELEASE_TAG_EXAMPLE_FILES:
        text = path.read_text(encoding="utf-8")
        path.write_text(GITHUB_TAG_INSTALL_RE.sub(rf"\g<1>{tag}", text), encoding="utf-8")


def run(command: list[str]) -> None:
    subprocess.run(command, cwd=ROOT, check=True)


def clean_dist() -> None:
    shutil.rmtree(ROOT / "dist", ignore_errors=True)


def has_staged_changes() -> bool:
    result = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT, check=False)
    return result.returncode == 1


def current_branch() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    branch = result.stdout.strip()
    if not branch or branch == "HEAD":
        raise RuntimeError("Cannot push release from a detached HEAD. Check out a branch first.")
    return branch


if __name__ == "__main__":
    raise SystemExit(main())
