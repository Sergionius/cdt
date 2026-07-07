# v0.3.0 Planning Foundation Feature Branch

## Goal

Create feature branch `feature/v0.3-planning-foundation` and implement the first v0.3.0 planning/safety foundation for CDT.

This stage should introduce step metadata, a non-executing pipeline plan command, and `cdt run --dry-run` as a safe alias to planning. It should not implement run reports, doctor, retry/timeout, or JSON Schema yet.

## Scope

Included:

1. Step metadata foundation.
2. Richer `cdt pipeline steps --json` output.
3. `cdt pipeline plan <pipeline> [--json]`.
4. `cdt run <pipeline> --dry-run` that never executes pipeline steps.
5. Tests and documentation updates for the above.

Deferred to later branches:

- `.cdt/runs/*.json` run reports.
- `cdt doctor`.
- `schema/cdt.schema.json`.
- retry/timeout execution wrappers.
- improved parallel error detail.
- recipes documentation.

## Branch

Create the branch before editing:

```bash
git checkout -b feature/v0.3-planning-foundation
```

If the branch already exists:

```bash
git checkout feature/v0.3-planning-foundation
```

## Design Principles

- Dry-run must be safe: `cdt run --dry-run` must not call `Step.run()` or `PipelineExecutor.run()`.
- Planning should be static and based on YAML + registry metadata + validation.
- Metadata should be useful but minimal for v0.3.0.
- Keep backward-compatible CLI behavior for existing commands.
- Keep JSON payloads versioned with `schema_version`.
- Prefer small, testable additions over broad rewrites.

## Step 1: Add Step Metadata

### Target files

- `cdt/pipeline/registry.py`
- `cdt/pipeline/builtins.py`
- `cdt/pipeline/validation.py`
- `tests/test_pipeline_registry.py` or new focused tests

### Implementation shape

Add a dataclass similar to:

```python
@dataclass(frozen=True)
class StepMetadata:
    name: str
    description: str = ""
    category: str = "custom"
    risk: str = "custom"
    requires_artifacts: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()
    external_tools: tuple[str, ...] = ()
    plugin: bool = False
```

Suggested initial risk values:

- `safe`
- `build`
- `artifact`
- `hook`
- `upload`
- `deploy`
- `push`
- `custom`

Registry should support:

- registering a step with optional metadata;
- listing names as before;
- listing metadata for JSON commands;
- default metadata for custom/plugin steps when no explicit metadata exists.

Do not require project plugin authors to change existing code.

### Built-in metadata examples

- `flutter.increment_build_number`: category `flutter`, risk `artifact` or `build`.
- `flutter.pub_get`: category `flutter`, risk `safe`, external tool `flutter`.
- `ios.flutter_build_ipa`: category `ios`, risk `build`, produces `artifact`, external tool `flutter`.
- `android.build_aab`: category `android`, risk `build`, produces `artifact`, external tool `flutter`.
- `appstore.upload_testflight`: category `appstore`, risk `upload`.
- `firebase.upload_app_distribution`: category `firebase`, risk `upload`.
- `firebase.deploy`: category `firebase`, risk `deploy`.
- `git.commit_push`: category `git`, risk `push`.
- `hook.python_script`: category `hook`, risk `hook`.

## Step 2: Enrich `cdt pipeline steps --json`

### Target files

- `cdt/pipeline/validation.py`
- `cdt/cli.py`
- tests around `pipeline steps --json`

Current JSON includes `registered_steps` as a list of names. Keep that for compatibility if possible, and add a richer field:

```json
{
  "schema_version": 1,
  "registered_steps": ["flutter.pub_get"],
  "steps": [
    {
      "name": "flutter.pub_get",
      "description": "Run flutter pub get",
      "category": "flutter",
      "risk": "safe",
      "requires_artifacts": [],
      "produces": [],
      "external_tools": ["flutter"],
      "plugin": false
    }
  ],
  "errors": []
}
```

Human output may remain a list of names for now.

## Step 3: Add Pipeline Plan Payload

### Target files

- `cdt/pipeline/validation.py` or new `cdt/pipeline/planning.py`
- `cdt/cli.py`
- tests, preferably `tests/test_pipeline_plan.py`

Add a planner that accepts `PipelineConfig` and pipeline name and returns a static plan.

Suggested JSON shape:

```json
{
  "schema_version": 1,
  "pipeline": "mobile-upload",
  "pipelines": ["mobile-upload", "prod"],
  "plugins": [],
  "overall_risk": "upload",
  "steps": [
    {
      "type": "step",
      "name": "flutter.pub_get",
      "category": "flutter",
      "risk": "safe",
      "options": {},
      "metadata": {}
    },
    {
      "type": "parallel",
      "risk": "build",
      "steps": []
    }
  ],
  "warnings": [],
  "errors": []
}
```

Risk aggregation can be simple for this branch. Suggested order:

```text
safe < artifact < build < hook < upload < deploy < push < custom
```

If any step is custom/plugin with no metadata, use `custom` risk and add a warning.

Planning must:

- load config;
- register built-ins;
- load plugins;
- validate selected pipeline;
- include validation errors in JSON;
- return non-zero when errors exist;
- never instantiate or run configured steps.

## Step 4: Add `cdt pipeline plan`

### Target file

- `cdt/cli.py`

Add command:

```bash
cdt pipeline plan <pipeline>
cdt pipeline plan <pipeline> --json
```

Human output should be concise, for example:

```text
Pipeline: mobile-upload
Overall risk: upload
Steps:
  - flutter.increment_build_number [artifact]
  - flutter.pub_get [safe]
  - parallel [build]
    - ios.flutter_build_ipa [build]
    - android.build_aab [build]
  - parallel [upload]
    - appstore.upload_testflight [upload]
    - firebase.upload_app_distribution [upload]
```

If validation errors exist, show them and exit with code 1.

## Step 5: Add `cdt run --dry-run`

### Target files

- `cdt/cli.py`
- tests around `cdt run --dry-run`

Add option:

```python
dry_run: bool = typer.Option(False, "--dry-run", help="Show pipeline plan without executing steps")
```

Behavior:

- If `--dry-run` is false, existing behavior is unchanged.
- If `--dry-run` is true, call the same planning path as `cdt pipeline plan`.
- Must not call `run_configured_pipeline`, `PipelineExecutor.run`, `ConfiguredStep.run`, or any concrete `Step.run`.
- Should support existing `--id` arguments so interpolated planning context can later use them, even if v0.3.0 does not resolve all dynamic values.

## Step 6: Tests

Add or update focused tests for:

- built-in metadata registration;
- `pipeline steps --json` includes rich step entries;
- `pipeline plan --json` includes steps, parallel groups, options, risks, and errors;
- unknown step in plan returns non-zero and JSON errors;
- plugin/custom step without metadata is represented as custom risk;
- `cdt run --dry-run` does not execute a test step with visible side effects;
- existing `cdt run <pipeline>` behavior still executes normally.

Use focused commands while developing:

```bash
pytest tests/test_pipeline_registry.py tests/test_pipeline_plan.py tests/test_cli.py
ruff check .
```

Final validation:

```bash
pytest
ruff check .
python -m build
```

## Step 7: Documentation

Update:

- `README.md` command list with `cdt pipeline plan` and `cdt run --dry-run`.
- `docs/pipelines.md` with a short planning/dry-run section.
- `docs/ai-agents.md` to recommend `cdt pipeline plan <pipeline> --json` as the agent preflight primitive.
- `CHANGELOG.md` under `Unreleased`.

Keep docs concise. Avoid documenting deferred features as implemented.

## Acceptance Criteria

- `cdt pipeline steps --json` exposes metadata while preserving existing useful fields.
- `cdt pipeline plan <pipeline>` works without executing steps.
- `cdt pipeline plan <pipeline> --json` is machine-readable and includes risk information.
- `cdt run <pipeline> --dry-run` is equivalent to planning and performs no side effects.
- Unknown steps and invalid configs produce clear non-zero validation results.
- Existing tests pass.
- New tests cover the safety guarantee that dry-run does not execute steps.

## Notes

Do not implement run reports in this branch. They should come next, after the planner establishes a stable representation of step trees and risks.
