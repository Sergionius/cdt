# Contributing to CDT

## Setup

```bash
git clone https://github.com/Sergionius/cdt.git
cd cdt
python -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

## Checks

Run before opening a pull request:

```bash
.venv/bin/ruff check .
.venv/bin/pytest -q
.venv/bin/pytest --cov=cdt --cov-report=term
.venv/bin/python -m build
.venv/bin/twine check dist/*
```

Changes to the CLI should include tests for both readable human output and structured JSON where applicable. Release execution changes should cover failure, interruption, status persistence, and production confirmation.

## Design expectations

- Keep `cdt run <pipeline>` simple for direct human use.
- Keep agent interfaces structured and low-noise.
- Preserve explicit, reviewable `cdt.yaml` behavior.
- Do not add network services or persistent infrastructure without a demonstrated requirement.
- Never include credentials, signing material, or real application artifacts in fixtures.
- Update README, relevant docs, examples, JSON Schema, and changelog together.

## Pull requests

Use a short intent-based branch name such as `feature/run-history` or `fix/stale-run-status`. Keep each pull request focused and describe behavior changes, safety implications, tests, and compatibility considerations.

Report security vulnerabilities privately as described in [SECURITY.md](SECURITY.md).
