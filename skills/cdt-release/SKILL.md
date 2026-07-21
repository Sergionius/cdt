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
- Do not paste full build logs. Detached runs write to `.cdt/runs/<run-id>/output.log`; read only a short tail on failure.
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
3. Read only the relevant log tail.
4. Report failed step, error, changed version files, artifacts produced, and one recommended next action.
5. Retry only after approval, except for a pure harness permission failure when elevated execution was already authorized.

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
