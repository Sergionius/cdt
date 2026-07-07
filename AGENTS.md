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

## Branch naming

When creating branches, use short intent-based names:

- `feature/<short-kebab-feature>`
- `fix/<short-kebab-bug>`
- `docs/<short-kebab-topic>`
- `chore/<short-kebab-task>`

Rules:

- use at most 3-4 meaningful words after the prefix;
- do not include dates;
- do not copy full plan titles;
- avoid implementation details unless they are the user-facing feature name;
- branch from latest `main`;
- keep one PR focused on one coherent feature, fix, or docs task.

Before pushing, check:

```bash
git branch --show-current
git merge-base --is-ancestor origin/main HEAD
git diff --name-only origin/main...HEAD
```

Good examples: `feature/agent-release`, `feature/run-status-file`, `fix/release-worker-exit`.
Bad examples: `feature/agent-release-token-efficient-cicd`, `feature/2026-07-07-token-efficient-cdt-releases-cicd-robustness`.

## PR merge cleanup

Delete feature/fix/docs/chore branches after merging PRs unless the user explicitly asks to keep them.

Preferred GitHub CLI merge command:

```bash
gh pr merge <PR> --merge --delete-branch
```

After merging, prune local refs and remove the local topic branch if it remains:

```bash
git fetch --prune
git branch -d <branch>
```

The GitHub repository setting `delete_branch_on_merge` should stay enabled so web UI merges also delete merged head branches.

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
