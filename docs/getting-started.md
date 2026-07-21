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
pipx install "git+https://github.com/Sergionius/cdt.git@v0.4.0"
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

Inspect a completed or running record later:

```bash
cdt history
cdt status <run-id>
cdt logs <run-id> --tail 80
```

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
