# AI agent workflow

CDT exposes a CLI and JSON contract that agents can use without reading Python
internals.

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

Use JSON validation to check schema, plugin imports, and registered step names
without executing deploy or upload work:

```sh
cdt pipeline validate offline-test --json
cdt pipeline inspect offline-test --json
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
