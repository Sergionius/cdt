# AI agent workflow

CDT exposes a CLI and JSON contract that agents can use without reading Python
internals.

## CDT Release Skill

This repository includes an Agent Skill for release automation:

```text
skills/cdt-release/SKILL.md
```

Use it when an AI agent is asked to send a CDT test release, upload to
TestFlight/AppTester/Firebase, or run commands such as `cdt run test` and
`cdt run ios-test`. The skill instructs the agent to:

- inspect `cdt.yaml` before running a pipeline;
- use `cdt pipeline list`, `cdt pipeline inspect <pipeline>`, and `cdt pipeline plan <pipeline> --json` as preflight;
- avoid production-like pipelines unless the user explicitly asks for production
  and confirms the exact command;
- capture long output in `.cdt/agent-release-<pipeline>.log`;
- report concise structured success/failure summaries instead of pasting full
  build logs.

Agents that support repository-level rules should also read:

```text
.agents/rules/cdt-release.md
```

`AGENTS.md` at the repository root is the cross-platform entry point for agents
such as Claude Code, Codex, and Pi-compatible harnesses.

### Human-in-the-loop production safety

CDT treats `cdt run` as real execution. There is no extra built-in `--yes`,
`--confirm`, or `--dry-run` prompt for deploy, upload, git push, or plugin code
execution. Agents must therefore pause before production-like pipelines and ask
for exact human confirmation, for example:

```text
Подтверждаю production release: cdt run <pipeline>
```

Ambiguous replies such as `ok`, `да`, `go`, or `continue` are not enough for a
production run.

### Structured run summary

After each run, agents should summarize using these fields:

```yaml
status: success | failed | blocked
pipeline: <pipeline>
version: <version/build or unknown>
artifacts:
  - <artifact path or upload result>
log: .cdt/agent-release-<pipeline>.log
working_tree:
  - <git status --short entries or clean>
next_actions:
  - <action, only if needed>
```

A JSON report at `.cdt/agent-release-<pipeline>.json` is optional and should be
created only when useful for automation.

### Install for Pi

Pi can load skills from packages, local paths, or settings. To install the CDT
repository as a Pi package:

```sh
pi install git:github.com/Sergionius/cdt
```

For a local checkout:

```sh
pi install /path/to/cdt
```

A project can also reference only the skill directory in `.pi/settings.json`:

```json
{
  "skills": ["../cdt/skills"]
}
```

Adjust the relative path so it points from the project settings file to the CDT
checkout.

### Install for Codex or other Agent Skills clients

Copy or symlink `skills/cdt-release/` into the client skill directory, for
example:

```sh
mkdir -p ~/.codex/skills
ln -s /path/to/cdt/skills/cdt-release ~/.codex/skills/cdt-release
```

The skill follows the common `SKILL.md` directory layout and can be used by
Agent Skills compatible clients.

### Other Agent Skills clients

Other Agent Skills compatible clients can use `skills/cdt-release/SKILL.md` by
copying or linking `skills/cdt-release/` according to their own skill
installation mechanism.

### Claude Code

Claude Code can use the root `AGENTS.md` file as the entry point. For release
work, it points the agent to `skills/cdt-release/SKILL.md` and
`.agents/rules/cdt-release.md`.

## Add a Project Step

Create a project-local plugin module and register a function with `@step`:

```python
from cdt.sdk import step


@step("offline.fetch_config")
def fetch_config(ctx, output: str):
    path = ctx.project_path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    ctx.values["offline_config_path"] = str(path)
```

Reference the module from `cdt.yaml`:

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

## Validate Before Running

Use JSON validation and planning to check schema, plugin imports, registered step names, and risk classification without executing deploy or upload work:

```sh
cdt pipeline validate offline-test --json
cdt pipeline inspect offline-test --json
cdt pipeline plan offline-test --json
cdt pipeline steps --json
```

Do not expect validation to resolve `${values.*}` or `${artifact.*}` that are
created by previous runtime steps. Those references are lazy and are resolved
when the consuming step runs.

## Run Trusted Pipelines

CDT treats project pipeline code as trusted. When an agent calls `cdt run`, CDT
performs the requested real work directly. There is no extra `--yes` prompt for
deploy, upload, git push, or plugin code execution.

Common commands:

```sh
cdt run offline-test
cdt run deploy
cdt run mobile-upload --id TASK-1 --id TASK-2
```

`cdt run deploy` can be configured to mirror the legacy `cdt deploy` flow. Keep
deploy paths in `.env` and reference them from `cdt.yaml`:

- `WEB_REPOSITORY`
- `WEB_BUILD_PLACE`
- `WEB_INNER`

Never write secrets or local absolute paths into shared example files.
