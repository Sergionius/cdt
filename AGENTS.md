# Instructions for AI agents working with CDT

## Commands

```bash
pytest
pytest --cov=cdt --cov-report=term
ruff check .
python -m build
```

Use `python -m build` before packaging or release changes to verify package metadata and included files. For release notes, check or update `CHANGELOG.md`.

Use focused tests while editing, for example:

```bash
pytest tests/test_skills.py
```

## Agent skills

When asked to send, publish, upload, or run a release through CDT, load:

```text
skills/cdt-release/SKILL.md
```

This includes requests such as `cdt run test`, TestFlight/AppTester/Firebase uploads, Pachca notifications, and Russian requests like "отправь тестовый релиз" or "залей тестовую сборку".

## Release safety rules

Follow `.agents/rules/cdt-release.md` for every `cdt run` prepared or executed by an agent.

Hard requirements:

- inspect `cdt.yaml` before running a pipeline;
- run `cdt pipeline list` and `cdt pipeline inspect <pipeline>` as preflight;
- write long release output to `.cdt/agent-release-<pipeline>.log`;
- never run production-like pipelines without exact human confirmation;
- provide a concise structured summary after the run.
