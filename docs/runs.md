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
