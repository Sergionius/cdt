# CDT v0.3.3 improvements plan

## Goal

Automate the CDT release pipeline, harden `cdt self-update` for production use, improve first-time user onboarding, and add runtime diagnostics and better pipeline error UX — all while keeping the package name `cdt` and GitHub-only release strategy unchanged.

## Phases

Run tasks in three phases. Phase 0 is a quick cleanup; Phase 1 is the highest-value release automation; Phase 2 contains the remaining self-update, docs, and UX work.

## Phase 0 — Cleanup (do first)

### Task 0.1: Fix stale README wording

**What:** Rewrite the sentence on README line 66 that currently says "Version 0.3.0 adds static planning metadata...". It should be version-neutral or describe the feature without pinning it to v0.3.0.

**Acceptance:**
- No stale version-specific claim about v0.3.0 in README.
- `ruff check .` passes.
- A maintainer reading the README cannot mistake the text for an outdated changelog entry.

### Task 0.2: Add CLI tests for `cdt self-update`

**What:** Add 1–2 integration tests in `tests/test_cli.py` that invoke `cdt self-update` through `click.testing.CliRunner` with mocked network/API state.

**Acceptance:**
- Test `cdt self-update --check` (or equivalent current behavior) with a mocked latest version.
- Test `cdt self-update` with a network/rate-limit error and assert exit code and stderr message.
- All tests pass; no real network calls in tests.

## Phase 1 — Release pipeline and CI hardening (highest priority)

### Task 1.1: Add GitHub Actions release workflow

**What:** Create `.github/workflows/release.yml` that triggers on `v*` tags and:
- checks out the repo at the tag;
- sets up Python 3.11+;
- installs dev dependencies;
- runs `ruff check .`;
- runs `pytest`;
- runs `python -m build`;
- runs `twine check dist/*`;
- creates a GitHub Release with `gh release create` (or `softprops/action-gh-release`);
- attaches `dist/*.whl` and `dist/*.tar.gz` to the release.

**Acceptance:**
- Pushing a `v*` tag produces a release with the correct assets.
- Workflow fails loudly if tests, lint, build, or twine check fail.
- Release notes use the tag name and a brief auto-generated body (or the relevant CHANGELOG section if present).

### Task 1.2: Add PR workflow

**What:** Create `.github/workflows/pr.yml` that runs on every PR and on pushes to `main`:
- `ruff check .`;
- `pytest`;
- `python -m build`;
- `twine check dist/*`;
- smoke test: install wheel into a fresh venv and run `cdt --version` and `cdt --help`.

**Acceptance:**
- PR status checks fail on lint, test, build, or smoke-test failure.
- Smoke test uses the wheel built in the same job, not an editable install.
- No PyPI upload attempt (CDT name is taken on PyPI).

### Task 1.3: Document the new release process

**What:** Update `README.md` and `CHANGELOG.md` to describe how releases are now created automatically via GitHub Actions after pushing a `v*` tag.

**Acceptance:**
- README section "Releasing" explains: push tag, CI creates release, attaches assets.
- CHANGELOG v0.3.3 section mentions "Automated release pipeline via GitHub Actions".

## Phase 2 — Self-update, docs, diagnostics, UX

### Task 2.1: Add `cdt self-update --check`

**What:** Implement `cdt self-update --check` that only checks whether a newer version is available and exits without modifying anything.

**Acceptance:**
- `cdt self-update --check` prints current version, latest version, and whether an update is available.
- Exit code `0` if up to date or only check performed; non-zero if a real self-update command would fail here.
- No filesystem or package changes.

### Task 2.2: Add `cdt self-update --json`

**What:** Add machine-readable output for `cdt self-update` and `cdt self-update --check` via `--json`.

**Acceptance:**
- `cdt self-update --check --json` outputs a JSON object with `current`, `latest`, `update_available`, and `status` fields.
- Same JSON shape is used by the real update command (with extra fields for `updated_to`, `message`, `error` if applicable).
- Output is valid JSON parseable by `jq`.

### Task 2.3: Handle GitHub API rate limits

**What:** Before calling the GitHub API, check the `X-RateLimit-Remaining` / `X-RateLimit-Reset` headers (or response on 403). Print a clear, actionable error message.

**Acceptance:**
- If rate-limited, `cdt self-update` prints remaining calls, reset time (UTC and local), and a suggestion to retry later or use `GITHUB_TOKEN`.
- Non-zero exit code.
- Tests cover this with mocked headers.

### Task 2.4: Explicit package manager support for self-update

**What:** Add `--manager {pipx,pip,uv}` to `cdt self-update`. If the manager is not specified, try to detect `pipx`/pip/uv from the environment, but if detection is ambiguous, print a clear message asking the user to specify `--manager`.

**Acceptance:**
- `cdt self-update --manager pipx` reinstalls from the latest GitHub tag using `pipx`.
- `cdt self-update --manager pip` reinstalls via `pip install`.
- `cdt self-update --manager uv` reinstalls via `uv tool install`.
- Unsupported or undetected manager yields a non-zero exit with a helpful message instead of a silent failure.
- `uv` support is implemented or explicitly fails with a clear message if the command is not available.

### Task 2.5: Add smoke test for GitHub tag install

**What:** Add a CI job (or a manual test script) that installs the latest CDT from a GitHub tag in a clean environment and verifies `cdt --version`.

**Acceptance:**
- A reproducible script or workflow step exists that installs `git+https://github.com/Sergionius/cdt.git@v<tag>` and checks version.
- It runs on the release workflow after the GitHub Release is published (or on a separate scheduled/manual workflow).
- It does not run on every PR to keep PR checks fast.

### Task 2.6: Add `docs/getting-started.md`

**What:** Create a short "Getting started in 5 minutes" guide at `docs/getting-started.md`.

**Acceptance:**
- Guide includes: pipx install from GitHub tag, `cdt --version`, `cdt pipeline list`, `cdt run test --dry-run`.
- README links to `docs/getting-started.md`.
- Commands in the guide are copy-pasteable and use the current latest tag (or a placeholder like `vX.Y.Z`).

### Task 2.7: Add `cdt doctor`

**What:** Implement a new CLI command `cdt doctor` that reports environment health.

**Acceptance:**
- `cdt doctor` checks and prints:
  - Python version;
  - CDT version;
  - detected installation manager (pipx/pip/uv/unknown);
  - whether `pipx` is in PATH;
  - GitHub API reachability;
  - whether `cdt.yaml` exists in the current directory;
  - whether `cdt.yaml` is valid YAML (and optionally valid against pipeline schema).
- Exit code `0` if all critical checks pass, non-zero if any critical check fails.
- Includes tests.

### Task 2.8: Improve pipeline error UX

**What:** Improve error messages in the pipeline runner.

**Acceptance:**
- Unknown step name: suggest the closest matching step names (Levenshtein distance or simple prefix/suffix matching).
- YAML parse error: show the file path, line/column if available, and a short example of valid YAML.
- Failed step: print a concise summary with step name, command, exit code, and whether artifacts were produced.
- Tests cover each of the three scenarios.

### Task 2.9: Add `scripts/release.py` version helper

**What:** Create a helper script `scripts/release.py <version>` that:
- bumps `version` in `pyproject.toml`;
- bumps `__version__` in `cdt/__init__.py`;
- prepends a vX.Y.Z section to `CHANGELOG.md` (with a placeholder or current date);
- runs tests, lint, and build;
- commits changes and creates an annotated tag `vX.Y.Z`;
- pushes the commit and tag to origin (CI then creates the GitHub Release).

**Acceptance:**
- `python scripts/release.py 0.3.3` updates version, changelog, runs checks, and creates/pushes the tag.
- If any check fails, the script exits without creating a tag or commit.
- `--dry-run` flag prints what would change without side effects.
- README documents the script.

## Non-goals

- Publishing to PyPI. The name `cdt` is already taken, so all releases remain GitHub-only.
- Renaming the package or CLI command from `cdt`.
- Fully automatic detection of `pipx`/`uv`/`pip` in self-update without a fallback flag. Auto-detection is allowed, but the `--manager` flag must be the reliable path.
- Adding new pipeline features or new DSL syntax in this iteration.

## Notes

- Keep each phase as a separate PR or worktree session if possible, so Phase 1 can be released as v0.3.3 quickly.
- All new commands must be covered by `pytest` tests and pass `ruff check .`.
- Maintain the existing minimal, safe scope; do not expand into a full plugin architecture or multi-package refactor.
