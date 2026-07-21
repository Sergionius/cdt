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

CDT streams readable output and records state transparently. Run IDs are only needed for later inspection:

```bash
cdt history
cdt status <run-id>
cdt logs <run-id>
```

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

## Logs

Detached execution captures combined output in `output.log`. Direct execution keeps normal terminal streaming; its log file may be empty when underlying tools write directly to the terminal. Status and artifacts remain available in either mode.

Treat logs as potentially sensitive because third-party build tools may print paths or diagnostic values.

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
