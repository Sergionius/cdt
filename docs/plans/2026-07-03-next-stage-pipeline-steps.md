# Next Stage: Internal Pipeline Steps

## Goal

Introduce a small internal pipeline step model without changing public CLI behavior.

This stage does not add `cdt.yaml`, external plugins, or user-defined pipeline execution. The goal is to split the existing large flow functions into reusable, testable step blocks so that a later stage can expose those blocks to project configuration.

## Current State

The project already has:

- Thin Typer commands in `cdt/cli.py`.
- Flow modules in `cdt/flows/`.
- Platform and service helpers in `cdt/platforms/` and `cdt/services/`.
- `CommandRunner` for testable command execution.
- `BuildArtifact` for APK, AAB, IPA, and web outputs.
- CI and unit tests.

The remaining issue is orchestration. `test_flow.py`, `prod_flow.py`, `ios_flow.py`, and `deploy_flow.py` still own the order of operations, progress labels, artifact lookup, failure handling, and side effects directly.

## Non-Goals

- Do not add `cdt.yaml` in this stage.
- Do not add external plugin loading.
- Do not change command names, options, help, or CLI behavior.
- Do not change `.env` contracts.
- Do not rewrite the parallel build loops in `test` and `prod` first.
- Do not introduce async or parallel pipeline execution.
- Do not add `cdt pipeline ...` or `cdt run ...` commands yet.

## Target Shape

Add a new internal package:

```text
cdt/pipeline/
  __init__.py
  context.py
  step.py
  executor.py
```

Add step modules as they become useful:

```text
cdt/steps/
  __init__.py
  flutter.py
  android.py
  ios.py
  web.py
  appstore.py
  firebase.py
  git.py
  notify.py
  tracker.py
```

The first version should stay simple. A step is a Python object with a name and a `run(ctx)` method.

```python
class Step(Protocol):
    name: str

    def run(self, ctx: PipelineContext) -> None:
        ...
```

Do not make the executor responsible for platform details. Steps should wrap the existing helper functions and register their outputs.

## Pipeline Context

Create `PipelineContext` as shared mutable state for a pipeline run.

Recommended fields:

```python
@dataclass
class PipelineContext:
    cwd: Path
    env: dict[str, str]
    runner: CommandRunner
    ids: list[str] = field(default_factory=list)
    old_version: str | None = None
    new_version: str | None = None
    artifacts: dict[str, BuildArtifact] = field(default_factory=dict)
    values: dict[str, str] = field(default_factory=dict)
```

Keep it intentionally small. Add fields only when a real step needs them.

The context may expose helpers for:

- reading required env values with readable errors
- registering and fetching named artifacts
- resolving project-relative paths
- running commands with consistent failure behavior

## Executor

Create a simple sequential executor:

```python
class PipelineExecutor:
    def run(self, steps: Sequence[Step], ctx: PipelineContext) -> None:
        for step in steps:
            step.run(ctx)
```

Do not implement parallel execution yet. Existing `test_flow.py` and `prod_flow.py` already handle the complex parallel logic. This stage should prove the step model on simpler flows first.

Exception wrapping is optional for this stage. Preserve `typer.BadParameter` and `typer.Exit` behavior unless tests show that adding a pipeline-specific error keeps public behavior unchanged.

## First Steps To Extract

Start with low-risk steps that are already sequential.

### iOS Xcode Steps

In `cdt/steps/ios.py`:

- `IncrementIosBuildNumberStep`
- `IosXcodeBuildIpaStep`

Behavior:

- Supports new `IOS_*` env keys and legacy `NATIVE_*` through existing helpers.
- Writes `ctx.old_version` and `ctx.new_version`.
- Stores the IPA artifact in `ctx.artifacts["ipa"]`.

### App Store Steps

In `cdt/steps/appstore.py`:

- `UploadTestFlightStep`

Behavior:

- Reads `ctx.artifacts["ipa"]`.
- Uses `ctx.new_version`.
- Accepts a changelog provider or fixed changelog string.

### Notify And Tracker Steps

In `cdt/steps/notify.py`:

- `NotifySuccessStep`
- `PlaySuccessSoundStep`
- `NotifyProdUserAgentPachcaStep`

In `cdt/steps/tracker.py`:

- `TrackerCommentStep`

### Web, Git, And Firebase Steps

In `cdt/steps/web.py`:

- `BuildFlutterWebStep`
- `CopyWebBuildStep`
- `ApplyWebCacheBustingStep`

In `cdt/steps/git.py`:

- `PrepareGitMainStep`
- `GitAddCommitPushStep`

In `cdt/steps/firebase.py`:

- `EnsureFirebaseCliStep`
- `FirebaseDeployStep`

## First Flow To Migrate

Start with `ios-test` and `ios-prod`.

They are sequential:

```text
increment iOS build number
build IPA with Xcode
upload TestFlight
notify success
play success sound
tracker comments for ios-test
prod user-agent notification for ios-prod
```

Update `cdt/flows/ios_flow.py` so it builds steps and runs them through `PipelineExecutor`.

Expected shape:

```python
def run_ios_test_flow(cwd: Path, env: dict[str, str], ids: list[str]) -> None:
    ctx = PipelineContext(cwd=cwd, env=env, runner=CommandRunner(), ids=ids)
    steps = [
        IncrementIosBuildNumberStep("IOS_TEST_SCHEME", "NATIVE_TEST_SCHEME"),
        IosXcodeBuildIpaStep(),
        UploadTestFlightStep(changelog=dev_changelog(ids)),
        NotifySuccessStep(include_ids=True),
        PlaySuccessSoundStep(),
        TrackerCommentStep(),
    ]
    PipelineExecutor().run(steps, ctx)
```

Keep external behavior and messages as close as practical to the current flow.

## Second Flow To Migrate

Migrate `deploy` and `firebase_deploy` after iOS Xcode flows.

Recommended `deploy` steps:

- `PrepareGitMainStep`
- `BuildFlutterWebStep(env_name="prod")`
- `CopyWebBuildStep`
- `ApplyWebCacheBustingStep`
- `GitAddCommitPushStep`
- `PlaySuccessSoundStep`

Recommended `firebase_deploy` steps:

- `EnsureFirebaseCliStep`
- `BuildFlutterWebStep()`
- `FirebaseDeployStep`
- `PlaySuccessSoundStep`

## Future Registry And Built-Ins

A later stage should add a step registry and built-in pipeline descriptions:

- `pipeline/registry.py`
- `flows/builtin.py`
- stable names such as `ios.xcode_build_ipa`, `appstore.upload_testflight`, `web.build`, `git.commit_push`, and `firebase.deploy`

That stage can then add:

```sh
cdt pipeline list
cdt pipeline inspect <name>
cdt run <name>
```

Only after built-ins can run through the registry should `cdt.yaml` loading be introduced.

## Test Plan

Add tests for:

- `PipelineContext` stores versions and artifacts.
- Context helpers validate required env values, artifact lookup, and project-relative paths.
- `PipelineExecutor` runs steps in order.
- Executor stops on exception.
- Each extracted step calls the expected helper.
- `ios-test` still supports repeated `--id`.
- `ios-prod` still sends prod user-agent notification.
- `deploy` still runs git add, commit, push in order.
- CLI smoke tests still pass.

Tests must not call real Flutter, Xcode, Firebase, git push, App Store Connect, or network.

Use `FakeRunner` and monkeypatch external side effects.

## Implementation Order

1. Add `cdt/pipeline/context.py`, `step.py`, and `executor.py`.
2. Add tests for context and executor.
3. Add iOS Xcode, App Store, notification, and tracker steps.
4. Rewrite `ios_flow.py` to use steps.
5. Add or update iOS flow tests.
6. Add web, git, and firebase steps.
7. Rewrite `deploy_flow.py` to use steps where it is clearly sequential.
8. Keep `test_flow.py` and `prod_flow.py` mostly unchanged for now.
9. Run full checks.

## Checks After Each Commit

```sh
.venv/bin/python -m compileall cdt tests
.venv/bin/python -m pytest -q
.venv/bin/python -m cdt.cli --help
git diff --check HEAD
git status --short
```

## Acceptance Criteria

- `ios-test`, `ios-prod`, `deploy`, and `firebase_deploy` run through internal steps.
- `test` and `prod` keep their current behavior.
- New step model is covered by tests.
- Existing public commands still work.
- Existing CLI help still lists the same commands and options.
- No public CLI command or env contract changes.
- `cdt.yaml` is not introduced yet.
- The code is ready for a future step registry.

## Known Risks

- Wrapping exceptions too aggressively could change Typer output or test behavior.
- Moving notification and sound behavior into steps can obscure the current command-specific completion messages.
- Starting with `test` or `prod` would force a premature parallel executor.
- A registry without real pipeline steps would add structure without reducing flow complexity.
