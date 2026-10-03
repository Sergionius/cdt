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

See [Execution modes](#execution-modes) for how the three start modes differ and what each one records.

## Execution modes

Every real execution creates exactly one run record under `.cdt/runs/<run-id>/`. The three start modes differ in who owns the process and how output is captured; status, artifacts, and reading commands are the same.

| | Ordinary direct run | Foreground capture | Detached execution |
| --- | --- | --- | --- |
| Start command | `cdt run <pipeline>` | `cdt run <pipeline> --capture-output` | `cdt agent-release start <pipeline> --json` |
| Runs in | the caller's terminal, in the foreground | the caller's terminal, supervised by one parent `cdt` process | a detached background worker |
| Terminal output | CDT's own output, unredacted | redacted combined stream (the saved copy and the terminal copy are identical) | none; read the log afterwards |
| `output.log` | CDT-owned diagnostics only | redacted combined stdout/stderr of the whole run | redacted combined output of the run |
| Manifest | `detached: false` | `detached: false`, `capture_output: true` | `detached: true` |
| `pid` / `exit-code` | written by the run itself | owned by the supervisor parent | owned by the detached worker |
| Production pipelines | prompt for `--confirm <pipeline>` when omitted | prompt is impossible: pass `--confirm <pipeline>` explicitly | pass `--confirm <pipeline>` to `agent-release start` |

Run and read the record:

```bash
cdt run test --capture-output          # foreground capture of this run
cdt agent-release start test --json    # detached execution
cdt status                             # newest run, human-readable (YAML-like)
cdt status --json                      # machine-readable payload
cdt logs --tail 120                    # redacted log of the newest run
cdt history --pipeline test --status failed
```

### Foreground capture limits

`--capture-output` is a POSIX feature (Linux/macOS). On a platform without the required process-group primitives CDT rejects the flag before creating a run record or executing any step.

The child run gets `stdin=DEVNULL`: no input, no prompts, no PTY, no terminal resize handling. Production pipelines therefore require the exact `--confirm <pipeline>` up front — a captured run cannot ask for it interactively. stdout and stderr of the child are merged into one stream; CDT does not promise a global ordering of lines produced by different processes, because interleaving is decided by the operating system when each process writes.

The child run is forced into the verbose transport (`CDT_UI=verbose` in the child environment only), so external build commands inherit the run's stdout/stderr instead of writing pretty per-command temp logs. The parent's own UI mode is unchanged.

### What capture shows

Capture shows the redacted stream. Unlike an ordinary direct run — where the terminal keeps its normal interactive output and only the saved copy is redacted — the terminal copy and `output.log` receive the same redacted data. Known credentials are replaced with `***` before anything is written or displayed.

Steps cannot read input, so interactive prompts of external tools do not work. Third-party CLIs may buffer their own output internally: CDT disables the child Python's buffering, but it cannot disable a third-party tool's buffering, so such output can be delayed until the tool flushes or exits. Output that never reaches the run's stdout/stderr is not captured: private files a tool writes on its own, logs a tool suppresses, and processes that deliberately redirected their own descriptors are outside the guarantee.

### Capture redaction, permissions, and retention

The capture stream passes the same redaction as every saved run log before it is written to `output.log` and before it is shown: credential-like environment keys, keys named by `CDT_REDACT_KEYS`, Bearer credentials, authorization headers, password/token assignments, and JWT-looking values become `***`. The capture log is created with owner-only permissions (`0600`).

Log completeness is bounded by the existing oversized-line protection: a single line beyond the limit is discarded behind a visible replacement marker instead of being stored raw or buffered without bound. Redaction cannot classify every possible secret format, so unknown secrets remain a residual risk — continue treating `.cdt/runs/` as sensitive project data. For the same reason, do not enable secret-bearing debug modes of external CLIs (for example `firebase --debug`): their verbose output can include credential material that CDT's redactor cannot guarantee to classify.

Retention stays manual, exactly as for every other run record: CDT never deletes previous records, and the capture log is part of the record, not a separate rotation stream. See [Retention](#retention).

## Manifest

`manifest.json` records schema version, run ID, pipeline, task IDs, CDT version, project path, Git revision, start time, command (including `--input KEY=VALUE` pipeline inputs), and whether execution was detached. It never stores environment variable values. Pipeline inputs are also written to `status.json` after defense-in-depth redaction; they are non-secret operational values by contract, never credentials.

## Status lifecycle

`status.json` is updated atomically and uses these states:

- `queued`: the record exists and execution has not started;
- `running`: at least one step is executing or the pipeline is between steps;
- `success`: every selected step completed or was skipped by its input condition;
- `failed`: execution ended with an error;
- `cancelled`: a detached process was stopped;
- `blocked`: execution requires an external decision or state change.

The status includes current/completed step IDs, parallel child state, artifact metadata, version changes, errors, and timestamps. Consumers must check `schema_version` before relying on fields.

`step_decisions` records the precomputed `run`/`skip` decision by numeric ID (including
groups). `skipped_steps` lists conditionally skipped leaves separately from
`completed_steps`; they create no artifacts and do not renumber later steps.
Decisions cover the whole pipeline even when resume selects only part of it.
Older statuses may omit these optional fields.

`step_attempts` records retry activity per leaf: the index of the last failed
attempt and its redacted error message. Intermediate retryable failures are not
terminal failures; the leaf is completed only after a successful attempt, and
the field is absent for steps that never retried. Older statuses may omit it.

`build_timings` records a measurement for every build leaf that actually
started, indexed by the existing leaf step ID. Each entry has `name`,
`started_at`, `finished_at`, `duration_seconds`, and `outcome`. A just-started
entry contains only the step name and the UTC `started_at`; the other fields
stay `null` until the leaf finishes. The final `outcome` is `success`, `failed`,
or `cancelled`. `duration_seconds` is computed from a monotonic clock and covers
the whole leaf call — option resolution, retries, retry delays, and artifact
registration — not CPU time or pure compiler time. If the process is killed, an
unfinished entry remains without a final duration instead of inventing data.
Conditionally skipped leaves and leaves already completed in an earlier run
create no measurements, and a new run record contains only the measurements of
the current run; old durations are not restored or added up. Parallel build
leaves get independent entries. Older statuses may omit the field. See
[Build step timing](pipelines.md#build-step-timing) and
[Comparing repeated builds](build-performance.md) for the format's use.

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

Both direct and detached executions save diagnostics in `output.log`. Detached execution captures the full combined output of the run. Direct `cdt run` tees the CDT-owned stdout/stderr into `output.log` while it is produced, so ASC retries, step progress, and the terminal error summary of a failed run remain inspectable afterwards; output that CDT does not own, such as raw third-party subprocess streaming on the interactive terminal, is not intercepted. When the full combined output of a direct run is needed — including output of external commands that bypass CDT — use the opt-in [foreground capture](#execution-modes) mode instead. Before writing either log, CDT replaces known values from credential-like environment keys, additional keys named by `CDT_REDACT_KEYS`, Bearer credentials, authorization headers, password/token assignments, and JWT-looking values with `***`. For ordinary direct runs the redactor is applied only to the saved copy — the direct terminal keeps its normal interactive output; in foreground capture mode the terminal copy is redacted too. Status errors and worker startup diagnostics use the same redactor. `cdt logs` redacts again when displaying a record as defense in depth for older logs.

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

Resume requires the same `--input` values as the original run; CDT rejects continuing a run with different inputs, so a release cannot be resumed with a different version:

```bash
cdt run release \
  --input version=X.Y.Z \
  --resume-status-file .cdt/runs/<run-id>/status.json \
  --skip-completed
```

Input conditions are recomputed after matching the original inputs, not restored
from saved decisions. Even an explicit `--resume-from <step-id>` cannot force a
conditionally skipped leaf to run. Old statuses without condition fields remain
valid resume sources; missing saved inputs mean an empty input set.
See [Conditional steps](pipelines.md#conditional-steps) for syntax and semantics.

Retried steps follow the same rules: completed leaves are skipped without new
attempts, and an unfinished leaf starts a new bounded attempt cycle — the saved
`step_attempts` budget is informational and is not carried over.
See [Step retries](pipelines.md#step-retries) for the retry contract.

For TestFlight pipelines, resume skips completed build and upload steps and starts at `appstore.complete_testflight` with the version context restored from the status file, so the IPA is not re-uploaded and the build number is unchanged:

```bash
cdt run prod \
  --resume-status-file .cdt/runs/<run-id>/status.json \
  --skip-completed
```

See [Resuming a failed TestFlight upload](pipelines.md#resuming-a-failed-testflight-upload) in the pipeline documentation for the full description.

### Parallel values checkpoints

The optional `values_state` status field has its own `version: 1`. It stores root
values and, for unfinished parallel groups, the original base and each branch's
snapshot at its last successfully completed leaf. Completion IDs and checkpoints
are written under the same lock. Failed-leaf writes are not restored. Resume with
`--skip-completed` preserves completed side effects and branch-local values without
exposing them to siblings; the root changes only when the whole group can merge.

Selecting only part of an unfinished group with `--resume-from` does not merge
unfinished branches or declare the group complete. CDT saves the selected leaves
and reports which leaves remain; continue from that new status using
`--skip-completed` without `--resume-from`. Conflicting completed writes require
manual reconciliation, not automatic repetition of completed side effects.

Checkpoints use normal secret redaction, including values keys. If redaction
changes required state, `restorable: false` prevents resume before any new step;
CDT never restores `***` in place of a secret. Keep values non-secret. Legacy
statuses remain usable outside partially completed parallel groups; missing
branch state for such a group is rejected before execution. Use the same pipeline
configuration when resuming: IDs are positional, not a configuration fingerprint.
This is not rollback of artifacts, files or external effects, nor an exactly-once
guarantee if a process dies between a side effect and its successful checkpoint.

### Google Play publication checkpoints

`google_play.upload_aab` keeps durable operation state under `.cdt/google-play/operations/<operation-id>.json`, separate from `status.json`. Checkpoints store only non-secret publication data — package, track, release parameters, AAB hash, edit metadata, version code, phase, and the confirmed result once known — and are written atomically before every external mutation.

During resume and plain reruns:

- completed steps are skipped as usual, and an already-confirmed Google Play operation returns its stored result without repeating any upload or commit;
- an unfinished operation resumes from its recorded phase with the restored artifact, never repeating confirmed mutations blindly;
- a plain rerun without `--resume-status-file`/`--skip-completed` cannot bypass an unfinished operation either: the checkpoint ID derives from the publication parameters and the AAB SHA-256, so identical parameters resume the same operation, and changed parameters stop with an explicit conflict error;
- if the result cannot be established after a lost response, the run fails with an explicit "publication result is unknown" error naming the checkpoint and asking you to verify the release in Play Console (Bundle Explorer and the track pages) before doing anything else.

Deleting a checkpoint is **not** a safe way to repeat a publication: it erases CDT's knowledge of changes that may already have been applied remotely. Verify the app in Play Console and resolve any half-applied state there instead.

The per-package lock `.cdt/google-play/locks/<package>.lock` serializes publications within this checkout only. It does not coordinate other machines, CI runners, or manual Play Console edits; conflicting external changes make the run stop instead of being overwritten.

### App Store review checkpoints

`appstore.submit_review` keeps its state under `.cdt/appstore/`, separate from `status.json`:

- `.cdt/appstore/uploads/<bundle-key>.json` records which build the last successful full TestFlight cycle (`appstore.upload_testflight` or `appstore.complete_testflight`) made ready for one app: bundle id, marketing version, build number, and completion time. It stores no tokens or secrets. A standalone submit run reads this record only when the current pipeline has no version context, and the chosen build is always re-verified in App Store Connect before anything is submitted. Records for builds uploaded before this functionality do not exist; rerun `appstore.complete_testflight` for the known build with the version context restored from the old run's status file to create one without re-uploading the IPA.
- `.cdt/appstore/operations/<operation-id>.json` is a checkpoint for one review submission, written atomically before every external change. Nothing is sent to Apple if the checkpoint could not be saved.
- `.cdt/appstore/locks/` serializes submissions of one app within this checkout only; it does not coordinate other machines, other checkouts, or manual App Store Connect work.

During resume and reruns:

- an identical already-confirmed submission is re-verified against App Store Connect and returns its stored result without submitting again — a successful submission followed by a later pipeline failure never causes a second submission;
- a lost submit or creation response is reconciled with read-only GET verification: an accepted submission completes the operation without repeating the request;
- when the outcome still cannot be established, the run fails with an explicit error naming the checkpoint (`.cdt/appstore/operations/<operation-id>.json`) and the operation stays blocked: further runs stop until the version and the submission have been verified in App Store Connect;
- changed submission parameters while an operation is unfinished, a version in a non-editable state, a foreign review submission, or a remotely changed build stop with an explicit conflict instead of being overwritten or resubmitted.

Deleting a checkpoint is **not** a safe way to repeat a submission: it erases CDT's knowledge of changes that may already have been applied remotely. Verify the version and the submission in App Store Connect and resolve any half-applied state there instead.

### App Store metadata updates

`appstore.update_metadata` keeps no checkpoint files under `.cdt/appstore/`: it stores its durable state in App Store Connect itself. Every run re-reads the current version localizations before the first mutation, skips requested fields that already match, and verifies every PATCH by reading it back; an unverifiable result fails the run without repeating the PATCH, and you resolve it in App Store Connect.

The step registers only a safe summary in `release_results`: bundle id, version string, and the names of the updated and unchanged locales. It never records credentials or metadata texts, and it never submits anything for review or selects a build.
