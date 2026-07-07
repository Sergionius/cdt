---
name: cdt-release
description: Use when asked to send or publish an app release through CDT, including natural Russian requests like "отправь тестовый релиз", "сделай тестовый релиз", "залей тестовую сборку", "релиз с id задачи", "отправь в TestFlight/AppTester/Pachca", or commands like cdt run test/ios-test. Runs CDT with low-noise output, verifies cdt.yaml before execution, avoids production unless explicitly requested.
---

# CDT Release Skill

Use this skill for any request to run a release through `cdt`, for example `cdt run test`, `cdt run ios-test`, TestFlight/AppTester/Firebase test releases, or production releases.

## Safety Contract

- Treat `cdt run` as real execution. CDT does not add a second deploy/upload/git-push confirmation prompt.
- Keep chat output minimal and observable.
  - Before long work: say only what is being executed and where the log is.
  - During long commands: do not narrate every poll/progress line.
  - After completion: give one concise structured success/failure summary.
- Do not paste full build logs. Capture logs to `.cdt/agent-release-<pipeline>.log` and show only a short tail on failure.
- Never run production pipelines (`prod`, `ios-prod`, `android-prod`, `release`, `deploy`, `production`, or anything production-like) unless the user explicitly asks for production and confirms the exact command.
- Forbidden without explicit human confirmation: `cdt run prod`, `cdt run ios-prod`, `cdt run android-prod`, `cdt run release`, `cdt run deploy`, or any pipeline that uploads to production, publishes publicly, pushes git refs, or mutates production infrastructure.
- Never assume a pipeline name implies platform coverage. Inspect `cdt.yaml` before running and verify the steps.
- Prefer one unified pipeline for a multi-platform test release so Android and iOS share one version/build number.
- Avoid failed first attempts that mutate versions: CDT/Flutter often needs write access to Flutter cache, Xcode, network, keychains, and project build directories. Request/use sufficient permissions before the first release run when the harness has sandbox restrictions.

## Preflight Checklist

Before any `cdt run`:

1. Confirm `cdt.yaml` exists.
2. Run/list/inspect quietly:
   - `cdt pipeline list`
   - `cdt pipeline inspect <pipeline>`
3. Verify intent and risk:
   - requested pipeline name matches the user's intent;
   - pipeline is not production-like unless production was explicitly requested and confirmed;
   - potentially destructive steps are understood before execution.
4. For a test release, verify the chosen pipeline is not production and check whether it includes the expected steps:
   - version bump: `flutter.increment_build_number` when a new build is desired;
   - dependencies: `flutter.pub_get`;
   - Android: `android.build_aab` or `android.build_apk`;
   - iOS: `ios.flutter_build_ipa` and usually `appstore.upload_testflight`;
   - notification: `notify.success` when the user expects Pachca/notification.
5. If the requested pipeline does not match the user's intent, stop and state the exact mismatch in one sentence. If the user asked to fix the config, make the minimal `cdt.yaml` change, then run the pipeline.

## Production Confirmation

For production-like pipelines, pause and ask for explicit confirmation before running. The confirmation must include the exact command, for example:

```text
Подтверждаю production release: cdt run <pipeline>
```

Do not accept ambiguous confirmations such as "ok", "да", "go", or "continue" for production. If the user previously said they wanted production but did not confirm the exact command, ask once and wait.

If CDT later provides a dry-run or confirmation flag for the requested operation, prefer using it before the real run. Do not invent unsupported flags.

## Token-Efficient Long-Running Protocol

After the preflight checklist, run long releases through the agent helper instead of streaming `cdt run` directly:

```bash
cdt agent-release start <pipeline> --id <ID>
cdt agent-release status <pipeline> --wait --timeout 40m
```

In chat, say only the start line and the final summary:

```text
Выполняю: cdt agent-release start <pipeline> (лог: .cdt/agent-release-<pipeline>.log)
```

Hard rules for normal long-running releases:

- Do not use `tail`, `grep`, or full log reads while the process is healthy.
- Do not narrate polling/progress messages such as "всё ещё ждём".
- Use `cdt agent-release status <pipeline>` for compact checks and `--wait` whenever possible so polling happens outside the chat loop.
- Build the final response from `.cdt/agent-release-<pipeline>.status.json`, meta/status output, and `git status --short`; read the log only on failure or explicit debug.
- If `status` reports `timeout`, `stale`, or a missing/stale PID, stop and ask before killing unless the user already authorized stopping.

Fallback only when the installed CDT does not support `agent-release`:

```bash
mkdir -p .cdt
cdt run <pipeline> --status-file .cdt/agent-release-<pipeline>.status.json > .cdt/agent-release-<pipeline>.log 2>&1
```

## Observability

Track progress internally as:

```yaml
status: running | success | failed | blocked
pipeline: <pipeline>
steps_completed:
  - <step or phase>
artifacts:
  - <path or upload result>
log: .cdt/agent-release-<pipeline>.log
next_actions:
  - <action>
```

If an automation-readable artifact is useful, write `.cdt/agent-release-<pipeline>.json` with the same fields. Do not create it when the user only needs a normal chat summary.

## Failure Handling

On failure:

1. Do not immediately retry if `pubspec.yaml` or version files changed; that may bump the build number again.
2. Check `git status --short` and the log tail.
3. Report:
   - `status: failed`;
   - failed command/pipeline;
   - whether version files changed;
   - last 40-80 log lines or the most relevant error;
   - recommended next action.
4. Retry only after the user approves, except when the only issue is harness permission/sandbox and the user already authorized elevated execution. Prefer starting with correct permissions to avoid this.

## Success Summary

On success, report in this concise format:

```yaml
status: success
pipeline: <pipeline>
version: <version/build or unknown>
artifacts:
  - <AAB/APK/IPA/TestFlight/AppTester/Firebase/Pachca result>
log: .cdt/agent-release-<pipeline>.log
working_tree:
  - <git status --short entries or clean>
next_actions:
  - <only if action is required>
```

Example:

```text
status: success
pipeline: test
version: 1.1.45+628
artifacts:
  - Android: build/app/outputs/bundle/release/app-release.aab
  - iOS: IPA загружен в TestFlight, ASC build VALID
  - Уведомление: notify.success отправлен
log: .cdt/agent-release-test.log
working_tree:
  - M pubspec.yaml
  - M cdt.yaml
next_actions: []
```
