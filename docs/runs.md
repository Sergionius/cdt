# Run records

Every real `cdt run <pipeline>` execution receives a unique ID and writes an isolated record:

```text
.cdt/runs/<run-id>/
  manifest.json
  status.json
  output.log
  exit-code
  pid
```

Planning, validation, preflight, and `cdt run --dry-run` do not create run records.

## Human and agent execution

A human runs the normal command:

```bash
cdt run test
```

CDT streams readable output and records state transparently. With no selector, status and logs use the newest run globally:

```bash
cdt history
cdt history --pipeline test --status failed
cdt status
cdt status <run-id>
cdt status --pipeline test
cdt logs
cdt logs <run-id>
cdt logs --pipeline test --tail 120
```

An explicit run ID takes priority over `--pipeline`. History filters compose, use effective run status, and are applied before `--limit`.

An automation client can start the same executor in detached mode:

```bash
cdt agent-release start test --json
cdt agent-release status --run <run-id> --wait --json
```

`agent-release` is a process-management adapter, not a separate pipeline implementation.

## Manifest

`manifest.json` records schema version, run ID, pipeline, task IDs, CDT version, project path, Git revision, start time, command, and whether execution was detached. It never stores environment variable values.

## Status lifecycle

`status.json` is updated atomically and uses these states:

- `queued`: the record exists and execution has not started;
- `running`: at least one step is executing or the pipeline is between steps;
- `success`: every selected step completed;
- `failed`: execution ended with an error;
- `cancelled`: a detached process was stopped;
- `blocked`: execution requires an external decision or state change.

The status includes current/completed step IDs, parallel child state, artifact metadata, version changes, errors, and timestamps. Consumers must check `schema_version` before relying on fields.

A status command may report `stale` when a detached PID disappeared without a terminal status or exit code. `timeout` is a wait result, not a pipeline terminal state.

## Failed-build output

A failed `cdt run` ends with a readable English summary instead of CLI usage help. For an iOS IPA build that fails inside a parallel group the summary looks like this:

```text
Pipeline failed at step 0/0 (ios.flutter_build_ipa).
iOS IPA build failed. Check the Flutter/Xcode output above for details.
Command: flutter build ipa --obfuscate --split-debug-info=obfsymbols --no-pub
Exit code: 74
Other parallel steps were allowed to finish.
Artifacts produced: android_aab
```

The first line attributes the failure to the leaf step that actually failed: its position inside the parallel group plus its stable step ID (`0/0 (ios.flutter_build_ipa)`); sequential failures use the plain step name. The cause line describes what failed. `Command:` and `Exit code:` show the actual generated command and its exit code when CDT knows them, and are omitted when they are unavailable. The parallel sentence appears only for failures inside a parallel group: sibling steps were still allowed to finish, and `Artifacts produced:` lists what they completed (`none` when nothing was produced).

A Firebase App Distribution upload failure is reported the same way. The command is shown with the token value replaced by `***`, and the Firebase CLI's own exit code is preserved:

```text
Pipeline failed at step 1/0 (firebase.upload_app_distribution).
Firebase App Distribution upload failed. Check the Firebase CLI output above for details; inspect the saved run with cdt logs <run-id>.
Command: firebase appdistribution:distribute /path/to/project/build/app/outputs/bundle/release/app.aab --app 1:1234567890:android:0a1b2c3d4e5f6a7b --groups qa-team --release-notes https://tracker.yandex.ru/TASK-123 --token ***
Exit code: 7
Other parallel steps were allowed to finish.
Artifacts produced: android_apk
```

This summary deliberately distinguishes the two failure points of an Android/Firebase pipeline: `android.build_aab` and `android.build_apk` report `Android AAB build failed.` / `Android APK build failed.` with the Flutter/Gradle command, while `firebase.upload_app_distribution` reports the upload step with the full `firebase appdistribution:distribute` command. When the failing step is the upload, the AAB build itself succeeded, so do not start by rebuilding the artifact.

A Firebase CLI message such as `Failed to make request` on its own does not establish the root cause: it is equally consistent with a network problem, invalid or expired token credentials, missing tester permissions, or an unusable artifact. CDT reports the command and exit code as-is and does not guess the cause. Read the Firebase CLI output streamed above the summary, or inspect the saved record afterwards with `cdt logs <run-id> --tail 80`, and check the obvious external preconditions (network reachability, `FIREBASE_TOKEN` validity, App Distribution access for the app ID) before changing code.

Note that `Artifacts produced:` covers only what the failing step or group itself produced (for example, a parallel sibling's `android_apk`). It does not mean earlier steps built nothing: a sequential `android.build_aab` that finished before the failed upload still registered its `android_aab`, and the artifact remains on disk. The full list of all artifacts collected during the run is stored in `status.json` and shown by `cdt status <run-id>`, so check it before rebuilding or re-running.

The displayed exit code is the return code of the Flutter command itself, not an embedded Xcode diagnostic. Xcode may report its own error codes (such as 74) inside the build log; CDT neither substitutes nor guesses them and reports Flutter's return value as-is, so use the build output to find the actual Xcode failure.

The summary identifies the failing step; it does not diagnose the underlying tool failure. Read the streamed Flutter/Xcode build output above the summary in the terminal, or inspect the saved record afterwards with `cdt logs <run-id>` — the same redacted summary is persisted in `output.log` and in the `status.json` error field.

## Concurrency

Run directories are immutable identities, so different pipelines and repeated runs of one pipeline cannot overwrite each other. A `latest-<pipeline>` pointer resolves compatibility commands such as:

```bash
cdt agent-release status test
```

Exact run IDs are preferred for automation.

## Logs and secret redaction

Both direct and detached executions save diagnostics in `output.log`. Detached execution captures the full combined output of the run. Direct `cdt run` tees the CDT-owned stdout/stderr into `output.log` while it is produced, so ASC retries, step progress, and the terminal error summary of a failed run remain inspectable afterwards; output that CDT does not own, such as raw third-party subprocess streaming on the interactive terminal, is not intercepted. Before writing either log, CDT replaces known values from credential-like environment keys, additional keys named by `CDT_REDACT_KEYS`, Bearer credentials, authorization headers, password/token assignments, and JWT-looking values with `***`. The redactor is applied only to the saved copy — the direct terminal keeps its normal interactive output. Status errors and worker startup diagnostics use the same redactor. `cdt logs` redacts again when displaying a record as defense in depth for older logs.

`CDT_REDACT_KEYS` is a comma-separated list of environment key names, not secret values:

```bash
export CDT_REDACT_KEYS=CUSTOM_SESSION,INTERNAL_AUTH
```

Redaction is defense in depth and cannot classify every provider diagnostic. Continue treating run logs as sensitive project data.

This limitation also bounds failure diagnostics for uploads such as Firebase App Distribution. During a direct `cdt run`, raw Firebase CLI subprocess output is streamed to the interactive terminal but is not necessarily captured into `output.log`; only the redacted terminal summary and CDT-owned lines are saved. CDT therefore preserves the upload command and exit code but does not automatically extract an HTTP status or network-cause explanation from the CLI's output — inspect the streamed Firebase output in the terminal when the failure happens. Do not work around this by enabling third-party debug modes such as `firebase --debug` in the pipeline: their verbose output can include additional credential material or request details that CDT's redactor cannot guarantee to classify.

Run records may still contain project paths, artifact names, task IDs, and other operational metadata. Continue treating `.cdt/runs/` as sensitive project data.

## Retention

CDT does not delete run records automatically. This avoids removing release evidence unexpectedly. `.cdt/` is ignored by the CDT repository template, and project owners may remove old completed directories according to their own retention policy.

Never delete a running directory. Check `cdt status <run-id>` before cleanup.

## Recovery and resume

Use the previous run's `status.json` as resume input:

```bash
cdt run test \
  --resume-status-file .cdt/runs/<run-id>/status.json \
  --skip-completed
```

Before resuming, inspect `git status --short`, verify that recorded artifacts still exist, and check whether version files changed during the failed run.

For TestFlight pipelines, resume skips completed build and upload steps and starts at `appstore.complete_testflight` with the version context restored from the status file, so the IPA is not re-uploaded and the build number is unchanged:

```bash
cdt run prod \
  --resume-status-file .cdt/runs/<run-id>/status.json \
  --skip-completed
```

See [Resuming a failed TestFlight upload](pipelines.md#resuming-a-failed-testflight-upload) in the pipeline documentation for the full description.
