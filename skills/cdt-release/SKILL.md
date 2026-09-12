---
name: cdt-release
description: Use when asked to build, send, upload, deploy, or publish a release through CDT, including TestFlight, App Distribution, Firebase, Pachca, and commands such as cdt run test or cdt run ios-test.
---

# CDT Release Skill

Use this skill for every request that executes a CDT release pipeline.

## Safety contract

- Treat `cdt run` as real execution.
- Inspect `cdt.yaml`; never infer platform coverage from a pipeline name.
- Never run production-like work unless the user explicitly requests production and confirms the exact command.
- Pipelines declared with `risk: production` require exact CDT confirmation, but agent review is still required.
- Do not paste full build logs. Detached runs redact known credentials before writing `.cdt/runs/<run-id>/output.log`; use `cdt logs <run-id> --tail 80` only on failure for defense-in-depth redaction.
- Prefer one unified test pipeline for a multi-platform release so platforms share one version/build number.
- Request sufficient filesystem, network, keychain, and build-tool permissions before the first mutating attempt.

## Preflight

Before every real run:

1. Confirm `cdt.yaml` exists.
2. Run:

   ```bash
   cdt pipeline list
   cdt pipeline inspect <pipeline> --json
   cdt pipeline plan <pipeline> --json
   cdt pipeline preflight <pipeline> --json
   ```

3. Verify that the selected pipeline matches the requested environment and platforms.
4. Review upload, deploy, push, hook, and production steps.
5. For a new build, check whether versioning and dependency steps are intentionally present.
6. Stop with one exact mismatch sentence if the pipeline does not match the request.

Planning and dry-run commands are non-executing and do not create run records.

## Production confirmation

For production-like work, ask once for this exact form:

```text
Подтверждаю production release: cdt run <pipeline>
```

Do not accept ambiguous replies such as “ok”, “да”, “go”, or “continue”. After approval, start with:

```bash
cdt agent-release start <pipeline> --confirm <pipeline> --json
```

Never add `--confirm` unless the exact command was approved.

## Long-running protocol

Start detached execution:

```bash
cdt agent-release start <pipeline> --id <ID> --json
```

Save `run_id` and `log` from the response, then wait outside the chat polling loop:

```bash
cdt agent-release status --run <run-id> --wait --timeout 40m --json
```

Rules:

- Announce only the pipeline, run ID, and log path before waiting.
- Do not narrate healthy polling or progress lines.
- Do not use `tail`, `grep`, or full log reads while status is healthy.
- Build the final response from status JSON and `git status --short`.
- Read 40–80 relevant trailing log lines only on failure.
- If status is `timeout`, `stale`, or `blocked`, do not kill or retry without authorization.
- Stop an authorized detached run with `cdt agent-release stop --run <run-id> --json`.

For an older CDT without `agent-release`, run `cdt run <pipeline>` with an explicit status file and redirect output to a project-local ignored log.

## Failure handling

1. Do not retry immediately if version files or generated project files changed.
2. Check `git status --short`.
3. Read only the relevant log tail with `cdt logs <run-id> --tail 80`; do not read `output.log` directly when the command is available.
4. Report failed step, error, changed version files, artifacts produced, and one recommended next action.
5. Retry only after approval, except for a pure harness permission failure when elevated execution was already authorized.

## CDT self-release pipeline

When the user asks to release CDT itself, use the repository's production `release` pipeline from the root `cdt.yaml`.

- Propose the next version yourself (for example, from `CHANGELOG.md` and `pyproject.toml`), but always pass it explicitly: `cdt run release --input version=X.Y.Z --confirm release`. Never let a pipeline guess or auto-select a version.
- Run the preflight first (`cdt pipeline list`, `cdt pipeline inspect release`, `cdt pipeline preflight release`, then `cdt run release --input version=X.Y.Z --dry-run`) and only after that ask for the exact confirmation of the full command including the version input.
- Wait for the terminal release result. A pushed tag or commit is not a successful release: the release counts as done only when the run status is `success`, meaning `github.wait_release` confirmed the green GitHub Actions workflow, the GitHub Release with wheel/sdist/`SHA256SUMS`, and the published PyPI version.
- Required tools: `git`, `gh` (GitHub authentication comes from the active `gh auth` session), `ruff`, `pytest`, `python -m build`, `twine`; the version preflight queries the public PyPI JSON API.

### Repair loop before publication

If the pipeline fails before the release commit (lint, tests, build, file preparation), release files are rolled back automatically. To repair code:

1. Verify the rollback: `git status --short` must be clean on synced `main`.
2. Create a separate `fix/<short-name>` branch from fresh `main`, apply the minimal fix, push, and open a PR.
3. Wait for CI on the PR to be green, then merge it and delete the branch (`gh pr merge <PR> --merge --delete-branch`).
4. Return to synced `main` (`git checkout main && git pull`).
5. Request a new exact production confirmation for the release command — the previous confirmation does not survive a code change.

Limit identical automatic repair attempts to three. After the third failed identical attempt, stop and report `blocked` with the failure summary instead of trying again.

Never move an existing tag and never reuse a version already published on PyPI; if the version is taken, propose the next one and start over with a new confirmation.

## Final summary

```yaml
status: success | failed | blocked | cancelled
run_id: <run-id>
pipeline: <pipeline>
version: <version/build or unknown>
artifacts:
  - <artifact path or upload result>
log: .cdt/runs/<run-id>/output.log
working_tree:
  - <git status --short entry or clean>
next_actions:
  - <only when action is required>
```
