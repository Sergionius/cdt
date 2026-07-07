import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_install_docs_use_current_release_tag():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    version = re.search(r'^version = "([^"]+)"', pyproject, flags=re.MULTILINE).group(1)
    expected = f"git+https://github.com/Sergionius/cdt.git@v{version}"

    for path in (ROOT / "README.md", ROOT / "docs" / "getting-started.md"):
        text = path.read_text(encoding="utf-8")
        assert expected in text
        assert "git+https://github.com/Sergionius/cdt.git@v0.3.3" not in text


def test_release_script_updates_readme_and_getting_started_examples():
    text = (ROOT / "scripts" / "release.py").read_text(encoding="utf-8")

    assert "README.md" in text
    assert "docs/getting-started.md" in text
    assert "update_release_tag_examples(args.version)" in text
