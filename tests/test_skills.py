import re
from pathlib import Path

import yaml


def test_cdt_release_skill_is_agent_skill_compatible():
    skill_path = Path(__file__).resolve().parents[1] / "skills" / "cdt-release" / "SKILL.md"
    content = skill_path.read_text(encoding="utf-8")

    assert content.startswith("---\n")
    _, frontmatter, body = content.split("---", 2)
    metadata = yaml.safe_load(frontmatter)

    assert metadata["name"] == "cdt-release"
    assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", metadata["name"])
    assert metadata["description"]
    assert len(metadata["description"]) <= 1024

    required_phrases = [
        "Confirm `cdt.yaml` exists",
        "cdt pipeline list",
        "cdt pipeline inspect <pipeline>",
        "Never run production-like work",
        "Production confirmation",
        "status: success",
        ".cdt/runs/<run-id>/output.log",
        "cdt agent-release status --run <run-id>",
        "Do not retry immediately if version files",
    ]
    for phrase in required_phrases:
        assert phrase in body


def test_ai_agent_docs_link_to_cdt_release_skill():
    root = Path(__file__).resolve().parents[1]

    assert "skills/cdt-release/SKILL.md" in (root / "docs" / "ai-agents.md").read_text(encoding="utf-8")
    assert "skills/cdt-release/SKILL.md" in (root / "docs" / "skills.md").read_text(encoding="utf-8")
    assert "skills/cdt-release/SKILL.md" in (root / "README.md").read_text(encoding="utf-8")


def test_repository_agent_rules_reference_cdt_release_skill():
    root = Path(__file__).resolve().parents[1]
    agents = (root / "AGENTS.md").read_text(encoding="utf-8")
    rules = (root / ".agents" / "rules" / "cdt-release.md").read_text(encoding="utf-8")
    manifest = (root / "MANIFEST.in").read_text(encoding="utf-8")

    assert "skills/cdt-release/SKILL.md" in agents
    assert ".agents/rules/cdt-release.md" in agents
    assert "Load `skills/cdt-release/SKILL.md`" in rules
    assert "Подтверждаю production release: cdt run <pipeline>" in rules
    assert "recursive-include .agents *.md" in manifest
