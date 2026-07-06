---
name: cdt-release
description: Use when asked to send or publish an app release through CDT, including natural Russian requests like "отправь тестовый релиз", "сделай тестовый релиз", "залей тестовую сборку", "релиз с id задачи", "отправь в TestFlight/AppTester/Pachca", or commands like cdt run test/ios-test. Runs CDT with low-noise output, verifies cdt.yaml before execution, avoids production unless explicitly requested.
---

# CDT Release Skill

Use this skill for any request to run a release through `cdt`, for example `cdt run test`, `cdt run ios-test`, TestFlight/AppTester/Firebase test releases, or production releases.

## Core Rules

- Keep chat output minimal.
  - Before long work: say only what is being executed and where the log is.
  - During long commands: do not narrate every poll/progress line.
  - After completion: give one concise success/failure summary.
- Do not paste full build logs. Capture logs to `.cdt/agent-release-<pipeline>.log` and show only a short tail on failure.
- Never run production pipelines (`prod`, `ios-prod`, `release`, `deploy`, or anything production-like) unless the user explicitly asks for production.
- Never assume a pipeline name implies platform coverage. Inspect `cdt.yaml` before running and verify the steps.
- Prefer one unified pipeline for a multi-platform test release so Android and iOS share one version/build number.
- Avoid failed first attempts that mutate versions: CDT/Flutter often needs write access to Flutter cache, Xcode, network, keychains, and project build directories. Request/use sufficient permissions before the first release run when the harness has sandbox restrictions.

## Preflight

1. Confirm `cdt.yaml` exists.
2. Run/list/inspect quietly:
   - `cdt pipeline list`
   - `cdt pipeline inspect <pipeline>`
3. For a test release, verify the chosen pipeline is not production and check whether it includes the expected steps:
   - version bump: `flutter.increment_build_number` when a new build is desired;
   - dependencies: `flutter.pub_get`;
   - Android: `android.build_aab` or `android.build_apk`;
   - iOS: `ios.flutter_build_ipa` and usually `appstore.upload_testflight`;
   - notification: `notify.success` when the user expects Pachca/notification.
4. If the requested pipeline does not match the user's intent, stop and state the exact mismatch in one sentence. If the user asked to fix the config, make the minimal `cdt.yaml` change, then run the pipeline.

## Running Low-Noise

Use a log file instead of streaming output:

```bash
mkdir -p .cdt
cdt run <pipeline> > .cdt/agent-release-<pipeline>.log 2>&1
```

In chat, say only:

```text
Выполняю: cdt run <pipeline> (лог: .cdt/agent-release-<pipeline>.log)
```

For long-running App Store Connect/TestFlight polling, do not post every poll. Wait for completion. If you must reassure the user after a long time, post at most one short update every 10 minutes:

```text
Все еще выполняется: жду обработку TestFlight/ASC.
```

## Failure Handling

On failure:

1. Do not immediately retry if `pubspec.yaml` or version files changed; that may bump the build number again.
2. Check `git status --short` and the log tail.
3. Report:
   - failed command/pipeline;
   - whether version files changed;
   - last 40-80 log lines or the most relevant error;
   - recommended next action.
4. Retry only after the user approves, except when the only issue is harness permission/sandbox and the user already authorized elevated execution. Prefer starting with correct permissions to avoid this.

## Success Summary

On success, report only:

- pipeline name;
- resulting version/build number from `pubspec.yaml` or CDT output;
- produced artifacts/important upload results (AAB/IPA/TestFlight/AppTester/Pachca notification);
- working tree changes from `git status --short`.

Example:

```text
Готово: `cdt run test` успешно завершен.
Версия: 1.1.45+628.
Android: build/app/outputs/bundle/release/app-release.aab.
iOS: IPA загружен в TestFlight, ASC build VALID.
Уведомление: notify.success отправлен.
Рабочее дерево: M pubspec.yaml, M cdt.yaml.
```
