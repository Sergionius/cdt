# Getting started in 5 minutes

## Install

CDT is distributed as `cdt-release` and installs the `cdt` command:

```bash
pipx install cdt-release
cdt --version
cdt doctor
```

A tagged GitHub release can also be installed directly:

```bash
pipx install "git+https://github.com/Sergionius/cdt.git@v0.5.4"
```

### Upgrade from 0.3.x

The old GitHub package used the distribution name `cdt`. Replace its pipx environment once:

```bash
pipx uninstall cdt
pipx install cdt-release
```

No `cdt.yaml` migration is needed. Schema version 1 remains supported, omitted pipeline risk defaults to `standard`, and old pipeline-named detached status files remain readable.

## Create a pipeline

In a Flutter project, run:

```bash
cdt init
```

CDT detects the available iOS, Android, and web project directories and creates a reviewable `cdt.yaml` with a standard test pipeline. It does not add uploads, credentials, notifications, version changes, or production steps automatically.

If the project already has `cdt.yaml`, inspect its pipelines instead:

```bash
cdt pipeline list
cdt pipeline inspect test
```

## Validate before execution

```bash
cdt pipeline validate test
cdt pipeline preflight test
cdt run test --dry-run
```

The dry run shows the step tree, artifact flow, warnings, and risk without executing commands.

## Run directly

```bash
cdt run test
```

Direct execution remains the normal human workflow. Every real run is recorded automatically under `.cdt/runs/`; no run ID is required to start it.

Inspect the newest completed or running record without copying its ID, or select one pipeline:

```bash
cdt history
cdt history --pipeline test --status failed
cdt status
cdt status --pipeline test
cdt logs --pipeline test --tail 80
```

Detached logs and persisted status errors redact known environment secrets and common credential forms. Run records can still contain project paths and artifact names, so continue treating `.cdt/runs/` as sensitive project data.

## Production pipelines

Declare production explicitly:

```yaml
version: 1

pipelines:
  prod:
    risk: production
    steps:
      - flutter.pub_get
```

Interactive execution asks for the exact pipeline name. Non-interactive execution must provide it:

```bash
cdt run prod --confirm prod
```

## Editor completion

Write the bundled JSON Schema to the project or editor configuration:

```bash
cdt schema --output cdt.schema.json
```

For YAML language servers, add this header to `cdt.yaml`:

```yaml
# yaml-language-server: $schema=./cdt.schema.json
```

## Update

```bash
cdt self-update --check
cdt self-update --manager pipx
```

## Releasing CDT itself

The CDT repository dogfoods its own tooling: `cdt.yaml` in the repository root declares a production `release` pipeline that takes an explicit `--input version=X.Y.Z`, verifies a synced `main` and an unused version, runs lint and tests, prepares release files, builds distributions, pushes the release commit and tag atomically, and waits for GitHub Actions, the GitHub Release, and PyPI.

From a fresh checkout, use the repository virtualenv bootstrap before CDT is installed globally:

```bash
.venv/bin/cdt pipeline validate --strict
.venv/bin/cdt pipeline inspect release
.venv/bin/cdt run release --input version=X.Y.Z --dry-run
```

The real release needs the exact production command:

```bash
.venv/bin/cdt run release --input version=X.Y.Z --confirm release
```

Required tools: `git`, `gh` (with an active `gh auth` session), `ruff`, `pytest`, `python -m build`, and `twine`. See the [Releasing section in the README](../README.md#releasing) for the full flow.
