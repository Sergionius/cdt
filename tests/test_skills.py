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


def test_cdt_release_skill_documents_retention_contract():
    skill_path = Path(__file__).resolve().parents[1] / "skills" / "cdt-release" / "SKILL.md"
    body = skill_path.read_text(encoding="utf-8").split("---", 2)[2]

    required_phrases = [
        "## Run history retention",
        "cdt history prune --older-than 30d --keep-last 10 --dry-run --json",
        "Retention previews need no approval",
        "explicit user approval",
        "--confirm",
        "Use `--json`",
        "Never use `rm`",
        "Never enable `experimental.run-retention` yourself",
        "needs no new approval",
    ]
    for phrase in required_phrases:
        assert phrase in body


def test_retention_contract_is_documented_for_users_and_agents():
    root = Path(__file__).resolve().parents[1]
    runs_doc = (root / "docs" / "runs.md").read_text(encoding="utf-8")
    agents_doc = (root / "docs" / "ai-agents.md").read_text(encoding="utf-8")
    readme = (root / "README.md").read_text(encoding="utf-8")
    skills_doc = (root / "docs" / "skills.md").read_text(encoding="utf-8")

    assert "cdt history prune --older-than 14d --confirm --json" in runs_doc
    assert "--keep-last 10 --pipeline test" in runs_doc
    assert "### Experimental auto-retention" in runs_doc
    assert "experimental.run-retention" in runs_doc
    assert "experimental.run-retention" in readme
    assert "cdt history prune" in readme
    assert "cdt history prune --older-than 30d --keep-last 10 --dry-run --json" in agents_doc
    assert "do not automatically update previously copied" in skills_doc
