from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def test_ci_build_removes_dist_before_build():
    text = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")

    assert "rm -rf dist\n          python -m build" in text


def test_all_workflow_pytest_commands_are_quiet():
    for workflow in WORKFLOWS.glob("*.yml"):
        for line in workflow.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if "pytest" in stripped and stripped.startswith("run:"):
                assert " -q" in stripped or stripped.endswith("pytest -q"), workflow


def test_ci_cli_smoke_covers_agent_release_help():
    text = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")

    for command in (
        "python -m cdt agent-release --help",
        "python -m cdt agent-release start --help",
        "python -m cdt agent-release status --help",
        "python -m cdt agent-release stop --help",
    ):
        assert command in text


def test_release_uses_trusted_publishing_and_checksums():
    text = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")

    assert "id-token: write" in text
    assert "pypa/gh-action-pypi-publish@release/v1" in text
    assert "sha256sum *.whl *.tar.gz > SHA256SUMS" in text
    assert "dist/SHA256SUMS" in text


def test_release_github_tag_smoke_retries_with_sleep():
    text = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")

    assert "for attempt in 1 2 3; do" in text
    assert 'if [ "$attempt" = "3" ]; then' in text
    assert "sleep 10" in text
    assert "pip install 'git+https://github.com/Sergionius/cdt.git@${{ github.ref_name }}'" in text
