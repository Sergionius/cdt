<!-- ralphex-base: bf6bc609483f8ec483f7cfe043304cf6fc9e53de -->

# Clear English errors for failed iOS pipeline builds

## Goal

Replace the confusing archive-failure summary with a readable English error identifying the failed iOS step, actual Flutter command exit code, and artifacts successfully produced by parallel work—without presenting a build failure as invalid CLI input.

## Context

- `cdt/steps/ios.py` discards the return code from `ctx.runner.run()` and raises `typer.Exit(code=1)`. Its empty string representation leaves parallel failure reports without a useful cause.
- `cdt/pipeline/executor.py` already identifies failed parallel children and preserves successful sibling artifacts. However, it wraps runtime failures in `typer.BadParameter`, causing Typer to display usage instructions and `Invalid value`.
- Command and exit-code summaries currently depend on configured options and message parsing. The dynamically generated Flutter command is unavailable through those options.
- `cdt/pipeline/runner.py` persists failure exit metadata and a terminal summary. Status and saved logs already support secret redaction.
- Existing tests cover parallel completion, nested failure attribution, redaction, resume, and direct-run log persistence.

## Scope

Improve the Flutter IPA failure cause, pipeline failure formatting, CLI presentation, and corresponding regression coverage. Preserve execution ordering, successful artifacts, status schema, and existing logging behavior.

## Out of Scope

- Diagnosing or fixing Xcode error 74.
- Parsing Flutter/Xcode logs to guess an underlying cause.
- Changing subprocess capture, cancellation, retries, or release behavior.
- Migrating every platform build step to a new command-error mechanism.

## Implementation Steps

### Task 1: Preserve the Flutter build failure details
**Files:**
- Modify: `cdt/runner.py`
- Modify: `cdt/steps/ios.py`
- Modify: `tests/test_ios_flutter.py`

- [x] Add an internal `CommandExecutionError` exception in `cdt/runner.py` carrying a copied command argument list and integer exit code, with a nonempty English cause supplied by the caller. Leave `CommandRunner.run()` and its integer return contract unchanged.
- [x] In `IosFlutterBuildIpaStep.run()`, retain the runner return code and raise `CommandExecutionError` on failure with the cause `iOS IPA build failed. Check the Flutter/Xcode output above for details.` Preserve the failure sound and do not register an IPA after failure.
- [x] Add fake-runner tests for successful artifact registration and failed builds, verifying the exact generated command, original return code, readable cause, failure sound, and absence of an IPA artifact. Mock sound playback and use temporary artifact fixtures.

### Task 2: Format runtime failures without losing child metadata
**Files:**
- Modify: `cdt/pipeline/executor.py`
- Modify: `tests/test_pipeline_error_ux.py`
- Modify: `tests/test_pipeline_executor.py`

- [ ] Introduce an internal `PipelineExecutionError` in `cdt/pipeline/executor.py`, carrying the existing failed-step metadata and a readable message. Use it instead of `typer.BadParameter` for aggregated parallel failures and executor summaries of step-raised `typer.BadParameter` or `CommandExecutionError`.
- [ ] Retain pre-execution configuration/resume validation as `typer.BadParameter`. Preserve propagation of unrelated exceptions from sequential steps and do not catch `BaseException` or change interruption behavior.
- [ ] Extend child failure metadata to retain structured command arguments and exit code when supplied by `CommandExecutionError`. Prefer this metadata over configured command/script options and legacy exit-code text parsing.
- [ ] Format a single failed child once, using this layout:
  ```text
  Pipeline failed at step 4/0 (ios.flutter_build_ipa).
  iOS IPA build failed. Check the Flutter/Xcode output above for details.
  Command: flutter build ipa ...
  Exit code: 1
  Other parallel steps were allowed to finish.
  Artifacts produced: android_aab
  ```
  Render the actual command with `shlex.join()`. Omit unavailable command and exit-code fields instead of printing `unknown` or `not applicable`. Include the parallel sentence only for parallel failures; retain `Artifacts produced: none` when appropriate.
- [ ] For multiple failures, print one child block per failure in configured child order, followed by the parallel completion sentence and one artifact summary. Select the first failed leaf in that same order as `failed_step`, keeping nested sequence attribution and eliminating completion-order-dependent selection.
- [ ] Give otherwise empty child exceptions a useful fallback: `Step exited with code N.` for `typer.Exit`, or the exception class name for other empty exceptions. Do not describe a `typer.Exit` code as a subprocess exit code.
- [ ] Redact the complete assembled summary before raising or persisting it. Preserve all child causes, successful sibling completion, artifact registration, and status schema version 1.
- [ ] Update existing message/type assertions and add coverage for empty exceptions, structured command failures, multiple distinct exit codes, nested failures, deterministic ordering, omitted unavailable fields, and redacted command arguments.

### Task 3: Present runtime errors cleanly through the CLI and run records
**Files:**
- Modify: `cdt/cli.py`
- Modify: `cdt/pipeline/runner.py`
- Modify: `tests/test_pipeline_status_file.py`
- Modify: `tests/test_agent_first.py`
- Modify: `tests/test_pipeline_error_ux.py`

- [ ] Catch only `PipelineExecutionError` around `run_configured_pipeline()` in `run_pipeline()`. Emit its already-redacted message once to stderr using `typer.echo()` and exit with code 1 using `typer.Exit`; do not print usage help or `Invalid value`.
- [ ] Update `_terminal_failure_summary()` to record `PipelineExecutionError` as readable failure text without an internal exception-class prefix. Preserve existing fallback handling for other exception types and existing recorder cleanup and exit-file writes.
- [ ] Add a `CliRunner` regression using a temporary pipeline, the real `IosFlutterBuildIpaStep`, a mocked command runner, and a successful parallel artifact-producing step. Use synchronization rather than timing-dependent sleeps to prove the sibling finishes after the iOS failure.
- [ ] Assert that terminal output contains the iOS leaf ID/name, build cause, actual command, actual Flutter exit code, and `android_aab`; excludes usage help, `Invalid value`, empty causes, placeholder metadata, and the old duplicated parallel summary; and exits with code 1.
- [ ] Verify the same run has failed status, the correct leaf `failed_step`, completed sibling metadata, retained Android artifact, exit-file value 1, and a nonempty redacted terminal failure in `output.log`.
- [ ] Update existing direct-run log assertions for the new runtime exception presentation. Add assertions that invalid pipeline/configuration requests still use the existing validation UX and that successful runs retain their current output.
- [ ] Exercise redaction with a credential-like environment value included in both a command argument and failure text; assert it is absent from the final terminal summary, status JSON, and saved terminal summary.

### Task 4: Document the failure-output contract
**Files:**
- Modify: `docs/runs.md`
- Modify: `CHANGELOG.md`

- [ ] Add a concise failed-build example explaining failed leaf attribution, available command/exit details, successful parallel artifacts, and consulting existing Flutter/Xcode output and run logs.
- [ ] Clarify that the displayed subprocess exit code is Flutter’s return code, not necessarily an embedded Xcode diagnostic such as 74.
- [ ] Update the Unreleased notes to describe readable runtime errors without CLI usage wrappers and preservation of Flutter build failure details.

## Validation

Run focused tests:

```bash
pytest tests/test_ios_flutter.py tests/test_pipeline_error_ux.py tests/test_pipeline_executor.py tests/test_pipeline_status_file.py tests/test_agent_first.py tests/test_cli.py tests/test_pipeline_resume.py tests/test_agent_release.py tests/test_redaction.py tests/test_runner.py
```

Then run repository checks:

```bash
pytest
ruff check .
```

All new reproduction coverage must use temporary projects and mocked build execution. Do not run a real release pipeline, Flutter archive, Xcode build, upload, or notification.

## Acceptance Criteria

- The reported iOS failure produces a clear English runtime error, not an invalid-argument diagnostic.
- The summary identifies `ios.flutter_build_ipa` and its leaf step ID, preserves the actual generated command and Flutter exit code, and includes a meaningful cause.
- Embedded Xcode error 74 is not guessed or substituted for Flutter’s return code.
- Parallel siblings finish as before; successfully produced Android artifacts remain recorded and visible.
- Multiple and nested failures retain accurate, deterministic attribution.
- Unknown command/exit placeholders and duplicated failed-step summaries are absent.
- Terminal summaries and persisted diagnostics remain redacted; status schema and resume behavior remain unchanged.
- Configuration errors and successful executions retain their existing behavior.

## Execution Notes

- Decision: Build on existing parallel child metadata rather than replace the executor; Alternatives: redesign pipeline execution; Reason: the repository already implements child attribution and sibling completion; Side effects: existing error-message assertions change.
- Decision: Introduce structured command failure metadata only for the requested Flutter IPA path; Alternatives: parse subprocess logs or migrate every build step; Reason: captures authoritative data with minimal scope; Side effects: direct callers of this step receive a descriptive exception instead of an empty `typer.Exit`.
- Decision: Distinguish runtime pipeline errors from argument validation and render them with existing Typer echo/exit primitives; Alternatives: retain `BadParameter` or add a new rendering dependency; Reason: removes misleading usage output without changing validation behavior; Side effects: affected runtime failures consistently exit with code 1 rather than Typer’s argument-error code.
- Decision: Omit unavailable command and exit fields while retaining meaningful causes; Alternatives: print placeholder metadata; Reason: unavailable details should not obscure actionable information; Side effects: human-readable error text changes, but status fields do not.
- Decision: Order multiple failures by configured child order and use the first failed leaf as primary; Alternatives: completion order or deepest leaf across unrelated branches; Reason: produces stable, correctly associated diagnostics; Side effects: primary failure selection can change when several branches fail.
- Decision: Preserve raw build diagnostics and point users to them rather than diagnose Xcode error 74; Alternatives: extract or infer a root cause; Reason: Flutter’s return value alone cannot establish the Xcode failure cause; Side effects: none.
- Decision: Keep status schema, artifact semantics, subprocess capture, and unrelated sequential exception propagation unchanged; Alternatives: broaden runtime error normalization; Reason: avoids an unnecessary compatibility and logging refactor; Side effects: none.
- Decision: Validate entirely with existing pytest infrastructure and mocked builds; Alternatives: reproduce a real archive or release; Reason: the reporting defect is testable without credentials, external services, or release side effects; Side effects: none.
- Decision: Enforce the nonempty-cause requirement at `CommandExecutionError` construction (raises `ValueError` on an empty/blank cause) and expose `command`/`exit_code` as keyword-only arguments with a defensive copy of the command list; Alternatives: accept any cause string; Reason: keeps the “nonempty English cause” contract verifiable at the single construction site and prevents later mutation of the reported command; Side effects: none for the single caller added in this task.
- Decision: Run validation via `.venv/bin/python -m pytest` and `.venv/bin/ruff`; Alternatives: bare `pytest`/`ruff`; Reason: system `python`/`pytest` are not on PATH in this environment and the project virtualenv provides them; Side effects: none.
- Validation evidence for this task: `pytest tests/test_ios_flutter.py` → 7 passed; full `pytest` → 399 passed; `ruff check .` → all checks passed (one import-sort issue introduced during editing was auto-fixed); `ruff format --check` on the three changed files reports them formatted.
