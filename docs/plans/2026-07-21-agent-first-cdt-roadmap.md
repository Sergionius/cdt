# Agent-first CDT roadmap

## Implementation status

Implemented for CDT 0.4.0 on 2026-07-21. The delivered scope includes shared run IDs and records, direct human execution, detached agent execution, exact production confirmation, Flutter/mobile initialization, bundled JSON Schema, PyPI distribution metadata and trusted publishing, documentation, CI consolidation, and targeted coverage improvements.

Plugin entry-point discovery and CI workflow generation remain demand-driven follow-ups, as specified by the conditional language in this plan. MCP, network services, dashboards, databases, and remote workers remain explicitly out of scope.

## Context

This plan follows a comparison of CDT with `oblien/openship` at commit `f05cbf2` and CDT `v0.3.6` at commit `8f67e42`.

CDT should remain a focused, local, agent-friendly release orchestrator. It should borrow onboarding, run identity, observability, and distribution ideas from Openship without becoming a deployment platform.

Agent-first does not mean agent-only. Direct human operation must remain a first-class path: commands such as `cdt run test` should stay simple, readable, and fully supported. Agents may use additional JSON and background-run interfaces, but these interfaces must not make the ordinary interactive CLI worse or mandatory for humans.

The current integration target is any local coding agent that can invoke commands and read Agent Skills. MCP, an HTTP API, a dashboard, a database, and a persistent control plane are not current requirements.

## Goals

- Make long-running CDT releases reliable and easy for agents to supervise.
- Preserve the simple direct human workflow, including `cdt run <pipeline>`.
- Keep release behavior explicit in project-local `cdt.yaml`.
- Provide compact, stable machine-readable status instead of requiring log parsing.
- Provide clear terminal output and prompts for direct human execution.
- Enforce production safety in both agent guidance and CDT itself.
- Improve onboarding, installation, documentation, and discoverability.

## Principles

1. CDT is one release engine with two first-class clients: humans and agents.
2. Humans use concise interactive commands; agents use stable structured output and optional background execution.
3. `cdt run <pipeline>` remains the canonical direct execution command and must not require an agent wrapper.
4. CDT executes and owns factual run state; agents should not infer it by parsing complete build logs.
5. Every machine-oriented feature should have a clear human-readable equivalent where appropriate.
6. Auto-detection may generate YAML, but must not replace explicit configuration with hidden behavior.
7. Production safety should not depend exclusively on agent instructions.
8. Reliability of the existing release workflow comes before broader functionality.
9. Avoid a daemon, database, HTTP API, dashboard, or event streaming until a demonstrated use case requires one.

## Phase 0: Design the run model

### Run identity

Replace pipeline-named mutable artifacts such as:

```text
.cdt/agent-release-test.log
.cdt/agent-release-test.status.json
.cdt/agent-release-test.pid
```

with immutable run directories:

```text
.cdt/runs/<run-id>/
  manifest.json
  status.json
  output.log
  exit-code
  pid
```

Example:

```text
.cdt/runs/20260721-154500-test-a81f/
```

A pointer such as `.cdt/runs/latest-test` may identify the latest run for backward-compatible pipeline-based status commands.

This prevents successive or concurrent runs from mixing logs, status, PIDs, and exit codes.

### Status model

Do not add JSONL event streaming initially. Agents only need an atomically updated status snapshot, while humans can continue to observe normal terminal output:

```json
{
  "schema_version": 1,
  "run_id": "20260721-154500-test-a81f",
  "status": "running",
  "pipeline": "test",
  "current_step": {
    "id": "1/0",
    "name": "ios.flutter_build_ipa"
  },
  "completed_steps": ["0"],
  "artifacts": [],
  "started_at": "...",
  "updated_at": "..."
}
```

### Compatibility

Keep the old form temporarily:

```bash
cdt agent-release status test
```

It should resolve to the latest run of that pipeline. The preferred interface should become:

```bash
cdt agent-release status --run <run-id>
```

### Deliverable

Before implementation, document:

- run ID format;
- run directory layout;
- status and manifest schemas;
- lifecycle states and transitions;
- concurrency rules;
- cleanup and retention policy;
- compatibility behavior for existing commands and files.

## Phase 1: Shared reliable execution with two UX paths

### Shared execution model

Run identity and status persistence should belong to the core executor, not to an agent client or the `agent-release` wrapper.

For a human:

```bash
cdt run test
```

CDT should continue streaming normal readable output and should transparently record the run. The user should not need to provide, copy, or understand a run ID unless they later want history, diagnostics, or resume behavior.

For an agent:

```bash
cdt agent-release start test --json
```

The same core executor runs in detached mode and returns structured metadata. `agent-release` should remain a thin process-management adapter rather than a separate release implementation.

Planning commands and `cdt run --dry-run` should not create real execution records.

### Agent start command

Proposed interface:

```bash
cdt agent-release start test --json
```

Example response:

```json
{
  "schema_version": 1,
  "status": "started",
  "run_id": "20260721-154500-test-a81f",
  "pipeline": "test",
  "log": ".cdt/runs/20260721-154500-test-a81f/output.log",
  "status_file": ".cdt/runs/20260721-154500-test-a81f/status.json"
}
```

An agent should only need to retain `run_id`. A human may continue to use `cdt run test` directly without creating or managing a background run explicitly.

### Status and wait commands

```bash
cdt agent-release status --run <run-id> --json
cdt agent-release status --run <run-id> --wait --timeout 40m --json
```

Requirements:

- JSON mode writes only JSON to stdout;
- payloads include `schema_version`, `run_id`, `pipeline`, and timestamps;
- timeout is distinct from failure;
- finished runs remain inspectable after the terminal or agent session exits;
- healthy status checks do not require reading the log;
- terminal states clearly distinguish `success`, `failed`, `cancelled`, `stale`, and `blocked`.

### Worker lifecycle

Handle explicitly:

- worker startup failure;
- process exit before exit-code persistence;
- stale or reused PIDs;
- an agent client or terminal terminating while a release continues;
- concurrent runs of different pipelines;
- accidental concurrent runs of the same pipeline;
- corrupt or partially written status files.

Write JSON and exit metadata atomically using temporary files and rename.

### Manifest

Each run should record non-secret execution context:

```json
{
  "schema_version": 1,
  "run_id": "...",
  "pipeline": "test",
  "ids": ["TASK-123"],
  "cdt_version": "0.4.0",
  "project_root": "...",
  "git_commit": "...",
  "git_branch": "...",
  "started_at": "...",
  "command": ["cdt", "run", "test", "--id", "TASK-123"]
}
```

Do not persist secrets or environment variable values.

### Acceptance criteria

- Consecutive runs of one pipeline do not mix state.
- Different pipelines may run concurrently.
- Starting a second instance of one pipeline cannot corrupt the first.
- An agent can produce a final summary without reading the full log.
- On failure, an agent only needs a short relevant log tail.
- `cdt run <pipeline>` uses the same execution and status model while retaining clear live terminal output for humans.
- Direct human runs are recorded transparently for later diagnostics.
- Human execution does not require `agent-release`, JSON output, or manual run ID management.
- Agent and human execution cannot drift into separate pipeline semantics.
- Tests cover stale PID, timeout, worker crash, corrupt status, and concurrency.

## Phase 2: Optimize Agent Skill integrations

Update `skills/cdt-release/SKILL.md` to use this workflow:

1. Confirm `cdt.yaml` exists.
2. Run `cdt pipeline list`.
3. Run inspect and plan in machine-readable form.
4. Check intent and production risk.
5. Start the release with JSON output.
6. Retain the returned run ID.
7. Wait using compact status output.
8. Read the log only on failure or explicit debugging.
9. Check `git status --short`.
10. Return one concise structured summary.

Target summary:

```yaml
status: success
run_id: 20260721-154500-test-a81f
pipeline: test
version: 1.2.3+456
artifacts:
  - platform: ios
    result: TestFlight upload completed
  - platform: android
    path: build/app/outputs/bundle/release/app-release.aab
log: .cdt/runs/20260721-154500-test-a81f/output.log
working_tree:
  - M pubspec.yaml
next_actions: []
```

The skill should not instruct an agent to inspect PID or exit files directly, poll logs while a process is healthy, or retry after a failed version-mutating run without checking the working tree.

## Phase 3: Production safety in CDT

Agent instructions remain useful, but CDT should enforce explicitly declared production risk.

Proposed pipeline configuration:

```yaml
pipelines:
  prod:
    risk: production
    steps:
      - ...
```

Do not rely only on pipeline name heuristics. A TestFlight upload is not necessarily production, and custom pipeline names cannot be classified reliably from names alone.

Interactive human execution should require entering the pipeline name. Non-interactive agent clients should pass an exact confirmation:

```bash
cdt agent-release start prod --confirm prod
```

Without confirmation, machine-readable output should report:

```json
{
  "status": "confirmation_required",
  "pipeline": "prod",
  "required_confirmation": "prod"
}
```

For compatibility:

1. Introduce `risk: production` as opt-in.
2. Warn when step metadata suggests high-risk behavior.
3. Decide separately whether a future schema should require explicit risk declarations.

## Phase 4: Add `cdt init`

Start with Flutter/mobile projects rather than attempting universal stack detection.

```bash
cdt init
```

Detect:

- `pubspec.yaml`;
- iOS project/workspace;
- Android project;
- common Flutter flavors;
- existing `cdt.yaml`;
- Firebase configuration presence.

Proposed interaction:

```text
Detected: Flutter, iOS, Android

Select pipelines:
[x] test
[x] ios-test
[x] android-test
[ ] production
```

The first version must not:

- create production pipelines without an explicit choice;
- write secrets;
- upload anything;
- change application versions;
- guess App Store or Firebase credentials;
- overwrite an existing `cdt.yaml` without confirmation.

The output must remain a normal readable `cdt.yaml`. The target first-run path is:

```bash
cdt init
cdt pipeline validate
cdt run test --dry-run
```

## Phase 5: Schema and documentation

### JSON Schema

Publish a schema for:

- pipelines;
- parallel and sequence groups;
- built-in steps and options;
- risk declarations;
- plugin modules.

Plugin-specific options should remain extensible because the core schema cannot know all third-party step definitions.

### Documentation

- Remove stale `CDT 0.2` wording.
- Document run IDs, status lifecycle, recovery, cancellation, and concurrency.
- Document production confirmation behavior.
- Add separate direct-human and agent-assisted release walkthroughs.
- Add Flutter/mobile recipes and troubleshooting for Xcode, Flutter cache, keychain, TestFlight, and Firebase.
- Add `SECURITY.md` and `CONTRIBUTING.md`.
- Add a terminal recording or screenshot demonstrating plan, start, wait, and summary.

## Phase 6: Installation and distribution

Publish the Python distribution under a unique available name while retaining the `cdt` console command.

Candidate distribution names:

- `cdt-release`;
- `cdt-cli`;
- `cdt-deploy`.

Preferred user experience:

```bash
pipx install cdt-release
cdt --version
```

Use PyPI trusted publishing through GitHub OIDC. Continue attaching wheels and source archives to GitHub Releases. Keep build, `twine check`, and installed-wheel smoke tests.

Package naming requires a separate availability and collision check before a decision.

## Phase 7: Quality and technical debt

Prioritize coverage in currently weaker areas:

1. `cdt/platforms/ios_xcode.py`;
2. `cdt/services/appstore.py`;
3. Firebase steps;
4. `cdt/ui.py`.

Add failure-oriented tests for:

- external command timeout;
- partial upload success;
- malformed provider responses;
- missing artifacts;
- interrupted parallel groups;
- retry after a version bump;
- worker and status persistence failures.

Reduce duplication between `.github/workflows/ci.yml` and `.github/workflows/pr.yml`, or give them clearly separate fast-PR and full-matrix responsibilities.

Consider Python 3.13 after dependency verification. Add Windows only if CDT intends to support Windows as a release host.

## Original sequencing

The approved work was consolidated into CDT 0.4.0 rather than spread across several minor releases. The original sequencing is retained below as design history.

### CDT 0.4: Agent run protocol and human-compatible execution

- run IDs and isolated run directories;
- manifest and status schemas;
- reliable start, status, wait, and stop;
- stable JSON responses;
- concurrency and crash tests;
- updated release Agent Skill;
- regression coverage for direct `cdt run <pipeline>` usage.

### CDT 0.5: Safety and onboarding

- explicit production risk;
- exact confirmation support;
- Flutter/mobile `cdt init`;
- JSON Schema;
- refreshed documentation.

### CDT 0.6: Distribution and ecosystem

- unique PyPI distribution;
- OIDC publishing;
- improved plugin discovery if needed;
- CI workflow generation only if real demand is confirmed.

## Explicitly out of scope

Do not add at this stage:

- MCP;
- an HTTP API;
- a dashboard or desktop app;
- a database or central control plane;
- remote build workers;
- automatic deployment without generated and reviewable YAML;
- JSONL event streaming without a concrete consumer;
- broad Openship-style web/backend platform support.

## Recommended first implementation

Start with run identity and the stable status protocol, not `cdt init`.

The existing `agent-release` helper already covers background execution and compact waiting, but pipeline-named mutable files limit history, concurrency, and correctness. Fixing that foundation first simplifies agent integrations and provides a reliable base for safety, onboarding, and distribution improvements. The ordinary human path remains `cdt run <pipeline>` and must be protected by compatibility tests throughout the work.
