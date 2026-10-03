# Reusable Python step plugins

CDT pipelines can use steps from ordinary Python packages. The mechanism is
deliberately minimal: `plugins:` in `cdt.yaml` lists Python module names, and
CDT imports exactly those modules. Importing a module registers its steps in
the same registry as the built-ins. There is no plugin registry, no entry-point
discovery, no curated index and no installation machinery — a documented
package layout is the whole contract.

A runnable example lives in [`examples/reusable-plugin/`](../examples/reusable-plugin/):
a `src`-layout package `cdt_example_steps` with a read-only, retry-safe
`example.check_file` step and a `cdt.yaml` that uses it.

## Writing a plugin package

Steps are declared with the `@step` decorator from `cdt.sdk`, with explicit
metadata (the same `StepMetadata` fields the built-ins use):

```python
# src/cdt_example_steps/__init__.py
from cdt.sdk import RetryableStepError, step


@step(
    "example.check_file",
    description="Read-only probe that records an existing project file path in pipeline values.",
    category="example",
    risk="safe",
    retry_safe=True,
)
def check_file(ctx, path: str = "pubspec.yaml"):
    resolved = ctx.project_path(path)
    if not resolved.is_file():
        raise RetryableStepError(f"Project file does not exist (yet): {path}")
    ctx.values["example_checked_file"] = str(resolved)
```

`pyproject.toml` is an ordinary Python package definition; the `src` layout is
a convention, not a requirement:

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "cdt-example-steps"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = ["cdt-release"]

[tool.hatch.build.targets.wheel]
packages = ["src/cdt_example_steps"]
```

Metadata guidance:

- Always set `description`, `category` and `risk` explicitly — plan, inspect
  and preflight outputs are built from them.
- Declare `requires`/`produces` when the step consumes or creates artifacts, so
  static artifact-flow analysis works.
- `retry_safe: true` is an explicit capability for steps whose whole rerun is
  safe — read-only checks are the typical case. Only such steps may combine
  with `retry: {max_attempts: >1}` in `cdt.yaml`, and only failures raised as
  `RetryableStepError` are retried.
- `timeout_option` names an existing native constructor parameter of the step;
  without it the `timeout_seconds` envelope setting is rejected.

## Installing into CDT's Python environment

Plugin modules must be importable by the same Python interpreter that runs the
`cdt` command. There is no separate plugin environment.

- **pipx install** (the recommended CDT installation): inject the package into
  the `cdt-release` environment:

  ```bash
  pipx inject cdt-release ./cdt-example-steps
  ```

- **Local development** (repository checkout with its virtualenv, or a `pip`
  install): install into the same environment as CDT itself:

  ```bash
  python -m pip install -e ./cdt-example-steps
  ```

- **Tests and CI without installation**: prepend the `src` directory to
  `PYTHONPATH`; nothing is installed and no package index is contacted:

  ```bash
  PYTHONPATH="$PWD/cdt-example-steps/src" cdt run example
  ```

## Using the plugin from several projects

Once the package is importable in CDT's environment, any project on that
machine enables it by listing the module in `cdt.yaml`:

```yaml
version: 1
plugins:
  - cdt_example_steps

pipelines:
  example:
    steps:
      - step: example.check_file
        with:
          path: pyproject.toml
        retry:
          max_attempts: 3
          delay_seconds: 1
```

Multiple projects can list the same module, and one project can list several
modules. A module name must be a plain importable module (a package `__init__`
or a single module); import errors are reported when the pipeline loads, before
any step runs. Plugins load after the built-ins are registered.

## Trust model and limitations

- **Importing a plugin executes its code.** Plugin steps run inside the CDT
  process with all the privileges of your user. Treat installing a step package
  exactly like installing any other Python dependency: only from sources you
  trust and review.
- **Steps run in-process.** There is no sandbox and no per-step process
  isolation; a plugin step can do anything the CDT process can.
- **No automatic discovery.** CDT never scans directories, entry points or
  indexes, and never installs anything; only modules listed in `plugins:` are
  imported.
- **Name conflicts fail loudly.** The step registry is global per process, and
  a duplicate registration (a plugin re-registering a built-in name, or two
  plugins claiming one name) raises an error instead of silently overriding.
  Prefix your step names with a stable namespace of your own, as the example
  does with `example.`.
- **Configuration is your responsibility.** Plugins are code, not data:
  version them with your project, and keep secrets out of step options — use
  environment variables as the built-ins do.
