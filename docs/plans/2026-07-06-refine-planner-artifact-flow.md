# Refine Planner Metadata and Artifact Flow

## Goal

Refine the v0.3.0 planning foundation by making `cdt pipeline plan --json` cleaner and more useful for preflight checks:

1. Reduce duplicated metadata in plan step nodes.
2. Clarify and improve built-in `StepMetadata` artifact/result types.
3. Add best-effort static artifact flow information to plan output.
4. Add non-blocking warnings for likely missing artifact dependencies, including parallel sibling dependencies.

This is a follow-up to `plans/2026-07-06-v030-planning-foundation.md`.

## Branch

Continue on:

```bash
feature/v0.3-planning-foundation
```

Before editing:

```bash
git fetch --all --prune
git pull --ff-only
```

## Important Terminology

Keep these concepts separate:

- **Artifact/result types**: static types declared in `StepMetadata`, for example `ios_ipa`, `android_aab`, `android_apk`, `web_build`, `version`, `upload_result`, `file`.
- **Artifact names**: configured pipeline-local artifact keys, usually from YAML option `artifact`, for example `ios_ipa`, `customer_app_ipa`, `android_aab`.

They often match but do not have to match.

Example:

```yaml
- ios.flutter_build_ipa:
    artifact: customer_app_ipa
```

This step:

- produces artifact/result type `ios_ipa`;
- produces configured artifact name `customer_app_ipa`.

For this task, keep existing `StepMetadata` field names:

```python
requires_artifacts: tuple[str, ...]
produces: tuple[str, ...]
```

Document that these describe static artifact/result **types**, not configured names. The plan output should expose both types and names via `artifact_flow`.

## Non-Goals

Do not:

- rename `StepMetadata.requires_artifacts` or `StepMetadata.produces`;
- add hard validation errors for artifact flow;
- execute any steps during planning;
- add run reports;
- add doctor;
- add JSON Schema;
- bump package version;
- change runtime execution behavior.

Artifact flow checks must be warnings only.

## Step 1: Compact `metadata` in plan JSON

### Current shape

Plan step nodes currently contain duplicated information:

```json
{
  "type": "step",
  "name": "flutter.pub_get",
  "category": "flutter",
  "risk": "safe",
  "metadata": {
    "name": "flutter.pub_get",
    "category": "flutter",
    "risk": "safe",
    "description": "Run flutter pub get.",
    "plugin": false
  }
}
```

### Target shape

Keep `name`, `category`, and `risk` at top level. Remove those duplicated fields from nested `metadata`:

```json
{
  "type": "step",
  "name": "flutter.pub_get",
  "category": "flutter",
  "risk": "safe",
  "metadata": {
    "description": "Run flutter pub get.",
    "requires_artifacts": [],
    "produces": [],
    "external_tools": ["flutter"],
    "plugin": false
  }
}
```

### Files

- `cdt/pipeline/planning.py`
- `tests/test_pipeline_plan.py`

### Notes

Do not change `cdt pipeline steps --json`; it should continue returning full metadata entries with `name`, `category`, and `risk`.

## Step 2: Improve built-in artifact/result type metadata

### Files

- `cdt/pipeline/builtins.py`
- `tests/test_pipeline_registry.py`

### Suggested type vocabulary

Use stable string values:

```text
ios_ipa
android_aab
android_apk
web_build
file
version
upload_result
notification
tracker_comment
```

### Suggested built-in metadata updates

- `ios.flutter_build_ipa`: `produces=("ios_ipa",)`
- `ios.xcode_build_ipa`: `produces=("ios_ipa",)`
- `ios.bump_xcode_build_number`: `produces=("version",)`
- `android.build_aab`: `produces=("android_aab",)`
- `android.build_apk`: `produces=("android_apk",)`
- `web.build`: `produces=("web_build",)`
- `flutter.increment_build_number`: `produces=("version",)`
- `appstore.upload_testflight`: `requires_artifacts=("ios_ipa",)`, `produces=("upload_result",)`
- `firebase.upload_app_distribution`: `requires_artifacts=("android_aab", "android_apk")`, `produces=("upload_result",)`
- `firebase.deploy`: `requires_artifacts=("web_build",)`, `produces=("upload_result",)` if this matches current step behavior; otherwise keep deploy without artifact requirements.
- `artifact.copy_to_downloads`: keep `requires_artifacts=("artifact",)`, `produces=("file",)` because it accepts any named artifact.
- `web.copy`: if it consumes the web build artifact, use `requires_artifacts=("web_build",)`, `produces=("file",)` or deployment-oriented result. If behavior is path-based and not artifact-based, keep generic or empty requirement.
- `notify.success`: `produces=("notification",)` if useful.
- `tracker.comment`: `produces=("tracker_comment",)`.

Use current step implementations to avoid inaccurate requirements. Prefer less specific metadata over wrong metadata.

## Step 3: Add `artifact_flow` to plan step nodes

### Files

- `cdt/pipeline/planning.py`
- `tests/test_pipeline_plan.py`

### Target shape

Each step node should include:

```json
"artifact_flow": {
  "requires_names": [],
  "produces_names": ["customer_app_ipa"],
  "requires_types": [],
  "produces_types": ["ios_ipa"]
}
```

### Rules

- `requires_types` comes from `StepMetadata.requires_artifacts`.
- `produces_types` comes from `StepMetadata.produces`.
- `produces_names` is inferred from YAML option `artifact` for artifact-producing build steps.
- `requires_names` is inferred from YAML option `artifact` for artifact-consuming steps.

Because the same option name `artifact` is used by producers and consumers, use metadata to decide direction:

- If metadata has `produces` containing artifact-like types (`ios_ipa`, `android_aab`, `android_apk`, `web_build`, or generic `artifact`), treat `options["artifact"]` as a produced name.
- If metadata has `requires_artifacts`, treat `options["artifact"]` as a required name.
- A step may theoretically both require and produce names; support both if metadata indicates both.

Only use string `artifact` option values. Ignore non-string/dynamic values for name inference.

## Step 4: Add non-blocking artifact flow warnings

### Files

- `cdt/pipeline/planning.py`
- `tests/test_pipeline_plan.py`

### Warning shape

Use existing `warnings` array:

```json
{
  "code": "missing_required_artifact",
  "message": "Step appstore.upload_testflight requires artifact name ios_ipa, but no previous step declares it.",
  "path": "pipelines.demo.steps[2]"
}
```

For parallel sibling dependency:

```json
{
  "code": "parallel_artifact_dependency",
  "message": "Step appstore.upload_testflight requires artifact name ios_ipa from the same parallel group; parallel branches start together.",
  "path": "pipelines.demo.steps[1].parallel.steps[1]"
}
```

### Sequential rules

Maintain best-effort state while planning:

- `available_names`: produced artifact names from previous sequential steps/groups.
- `available_types`: produced artifact/result types from previous sequential steps/groups.

For a step:

- warn if `requires_names` is non-empty and a required name is not in `available_names`;
- if no name can be inferred, optionally warn based on missing `requires_types`, but be conservative to avoid noisy warnings;
- after checking requirements, add produced names/types to available sets.

### Parallel rules

For a parallel group:

- each child sees only the state available before the parallel group;
- a child must not depend on a name produced by a sibling in the same group;
- after the group, merge all child produced names/types into the parent state;
- keep warnings non-blocking.

### Noise control

Prefer name-based warnings over type-only warnings. Type-only warnings can be noisy because plugin/hook steps may produce artifacts without precise metadata.

Recommended for this iteration:

- implement name-based `missing_required_artifact` warnings;
- implement same-parallel-group name dependency warnings;
- expose `requires_types`/`produces_types`, but do not warn on type-only gaps unless it is clearly safe.

## Step 5: Tests

Add/update tests for:

1. `plan --json` compact metadata does not include duplicated `name`, `category`, or `risk` in nested `metadata`.
2. Built-in metadata has precise types:
   - `ios.flutter_build_ipa.produces == ("ios_ipa",)`
   - `android.build_aab.produces == ("android_aab",)`
   - `android.build_apk.produces == ("android_apk",)`
   - `appstore.upload_testflight.requires_artifacts == ("ios_ipa",)`
3. Sequential artifact flow:
   - build step with `artifact: ios_ipa` followed by upload with `artifact: ios_ipa` has no missing-artifact warning.
4. Missing artifact warning:
   - upload with `artifact: ios_ipa` and no previous producer warns.
5. Parallel sibling warning:
   - build and upload for the same artifact in the same parallel group warns.
6. Dynamic/non-string artifact option does not crash planning.
7. Existing `steps --json` full metadata remains unchanged enough for existing consumers/tests.

## Step 6: Documentation

Update:

- `docs/pipelines.md`
- `docs/ai-agents.md`
- `CHANGELOG.md`

Document:

- `StepMetadata.requires_artifacts` and `StepMetadata.produces` describe static artifact/result types;
- `plan --json` exposes `artifact_flow` with both names and types;
- artifact flow warnings are preflight hints and do not block execution by themselves;
- parallel branches start together, so artifact dependencies should cross parallel-group boundaries only after the group completes.

## Step 7: Validation

Run:

```bash
.venv/bin/python -m pytest tests/test_pipeline_registry.py tests/test_pipeline_plan.py tests/test_cli.py
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
.venv/bin/python -m build
```

## Acceptance Criteria

- `cdt pipeline plan --json` has compact nested metadata without duplicated `name`, `category`, and `risk`.
- Built-in metadata uses more precise artifact/result types where accurate.
- Plan step nodes include `artifact_flow` with name/type requirements and productions.
- Sequential build-then-upload artifact flows do not warn.
- Missing upload artifacts produce non-blocking warnings.
- Same-parallel-group artifact dependencies produce non-blocking warnings.
- No steps are executed during planning.
- Full test suite, Ruff, and build pass.
