# CDT Release Agent Rules

Use these hard rules whenever an agent runs or prepares to run `cdt run`.

## Required pre-run checks

1. Load `skills/cdt-release/SKILL.md`.
2. Confirm `cdt.yaml` exists in the target project.
3. Run `cdt pipeline list` and `cdt pipeline inspect <pipeline>` before `cdt run`.
4. Verify the inspected steps match the user's intent and are not production-like unless production was explicitly requested and confirmed.
5. Announce the command and log path before execution.

## Production safety

Never run production-like pipelines without exact human confirmation. This includes pipelines named or behaving like `prod`, `ios-prod`, `android-prod`, `release`, `deploy`, `production`, public publishing, production uploads, git pushes, or production infrastructure changes.

Acceptable confirmation format:

```text
Подтверждаю production release: cdt run <pipeline>
```

Ambiguous replies such as `ok`, `да`, `go`, or `continue` are not enough for production.

## Logging and observability

- Always write long command output to `.cdt/agent-release-<pipeline>.log`.
- Do not paste full logs into chat.
- On failure, show only the relevant error or the last 40-80 log lines.
- Summaries must include: `status`, `pipeline`, `artifacts`, `log`, `working_tree`, and `next_actions`.
- A JSON report at `.cdt/agent-release-<pipeline>.json` is optional and should be created only when useful for automation.

## Forbidden actions

- Do not run `cdt run <production-like-pipeline>` without exact confirmation.
- Do not invent unsupported safety flags such as `--dry-run` or `--confirm`.
- Do not immediately retry after `pubspec.yaml` or version files changed.
- Do not assume a pipeline covers Android/iOS from its name alone; inspect it.
