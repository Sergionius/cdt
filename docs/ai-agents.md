# Agent automation

CDT is agent-first, not agent-only. The direct human interface remains:

```bash
cdt run <pipeline>
```

Automation clients use the same pipeline engine with structured planning, isolated run records, and optional detached execution.

## Release skill

The repository includes:

```text
skills/cdt-release/SKILL.md
```

Use it for test releases, TestFlight/App Distribution uploads, and other requests that execute `cdt run`. Repository-level clients should also read:

```text
AGENTS.md
.agents/rules/cdt-release.md
```

The skill requires agents to:

- inspect `cdt.yaml` and the selected pipeline;
- validate, plan, and preflight before execution;
- reject production execution without exact human confirmation;
- start long work through the detached release helper;
- wait on compact status JSON rather than polling full logs;
- read a short log tail only on failure;
- return one concise structured summary.

## Stable preflight contract

```bash
cdt pipeline list
cdt pipeline inspect <pipeline> --json
cdt pipeline plan <pipeline> --json
cdt pipeline preflight <pipeline> --json
```

JSON payloads include `schema_version`. Planning and dry-run commands never execute pipeline steps and do not create run records.

## Detached execution

Start a run:

```bash
cdt agent-release start test --json
```

The response includes a unique `run_id` and paths under:

```text
.cdt/runs/<run-id>/
```

Wait without reading the build log:

```bash
cdt agent-release status --run <run-id> --wait --timeout 40m --json
```

Stop a detached run:

```bash
cdt agent-release stop --run <run-id> --json
```

Pipeline-based status remains available for compatibility and resolves the latest run:

```bash
cdt agent-release status test --json
```

### Exit codes

- `agent-release start`: `0` when the worker starts, `1` for configuration/startup errors, `2` when production confirmation is required.
- `agent-release status --wait`: `0` when a compact terminal or timeout payload is returned; inspect `status` and `wait_status` rather than inferring pipeline success from the command exit code.
- planning, validation, and preflight commands: `0` for a valid/ready result and `1` when their JSON payload contains errors.

## Production confirmation

Production must be declared explicitly:

```yaml
pipelines:
  prod:
    risk: production
    steps:
      - ...
```

A detached production run requires exact confirmation:

```bash
cdt agent-release start prod --confirm prod --json
```

Without it, CDT exits without creating a run and returns `confirmation_required`.

## Structured summary

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
  - <action only when needed>
```

## Add a project step

Create a trusted project-local plugin:

```python
from cdt.sdk import step


@step("offline.fetch_config")
def fetch_config(ctx, output: str):
    path = ctx.project_path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    ctx.values["offline_config_path"] = str(path)
```

Reference it from `cdt.yaml`:

```yaml
version: 1
plugins:
  - cdt_steps.offline
pipelines:
  offline-test:
    steps:
      - offline.fetch_config:
          output: build/offline/config.json
```

Project plugin code is trusted and executes with the same permissions as CDT. Never run an unreviewed project pipeline or plugin.
