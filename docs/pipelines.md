# CDT pipelines

CDT uses `cdt.yaml` as its explicit project-local automation model. Run pipelines with:

```bash
cdt run prod
cdt run prod --dry-run
cdt pipeline list
cdt pipeline inspect prod --json
cdt pipeline plan prod --json
cdt pipeline validate
cdt pipeline steps
```

## Schema v1

```yaml
version: 1
plugins: [] # optional

pipelines:
  prod:
    risk: production
    steps:
      - flutter.increment_build_number
      - flutter.pub_get
      - ios.flutter_build_ipa:
          profile: prod
          flavor: prod
          artifact: ios_ipa
```

Top-level fields are `version`, optional `plugins`, and `pipelines`. Each pipeline contains `steps` and may declare `risk: standard` (the default) or `risk: production`. Legacy top-level lifecycle keys and default pipelines are not supported.

A production pipeline requires exact confirmation. Humans are prompted interactively; automation can use `cdt run prod --confirm prod`. Do not classify production only by pipeline name.

## Pipeline inputs

A pipeline can declare non-secret inputs that must be passed explicitly on the command line:

```yaml
pipelines:
  release:
    risk: production
    inputs:
      version:
        required: true
        pattern: '^\d+\.\d+\.\d+(?:[.-][A-Za-z0-9]+)?$'
    steps:
      - release.require_version_available:
          version: ${inputs.version}
```

Rules:

- `required: true` fails the run when the input is missing; the optional `pattern` is a regex that the value must fully match.
- Pass inputs as repeatable `--input KEY=VALUE` options on `cdt run`, `cdt pipeline plan`, and `cdt agent-release start`. Unknown, duplicate, malformed (`no '='`), missing required, or pattern-violating inputs are rejected before any step runs.
- Step options interpolate inputs with `${inputs.<name>}`; referencing an undeclared input fails with a clear error.
- Inputs are non-secret by contract: never pass credentials as inputs. They are stored redacted in the run manifest and status and shown by `cdt pipeline inspect` / `cdt pipeline plan` as declarations only (never runtime values).
- Resume requires the same inputs as the original run; a release cannot be continued with a different version.

Pipelines without `inputs` keep the previous behavior; no migration is needed.

## Conditional steps

Leaf steps also accept an extended form (including leaves inside existing groups):

```yaml
version: 1
pipelines:
  preview:
    inputs:
      deploy: {}
    steps:
      - step: firebase.deploy
        with: {}
        when:
          input: deploy
          equals: "yes"
```

`step` is required; `with` defaults to `{}` and contains only constructor options.
`when` is optional. Unknown envelope fields are rejected. String and single-key
step forms are unchanged. Conditions cannot be attached to groups.

A condition names a declared input and exactly one operator:

- `equals: "value"` / `not_equals: "value"`: exact, case-sensitive string comparisons.
  Quote YAML booleans/numbers when using them as comparison strings.
- `present: true`: the input exists and is not the empty string; whitespace is nonempty.
  `present: false` is the inverse.
- For an absent optional input, `equals` is false and `not_equals` is true.

Conditions are literal: no `${...}` interpolation, env/runtime values, or expressions.
All decisions are frozen after input validation and before the first step runs.
Skipped leaves retain their numeric IDs, do not interpolate options or construct a
runtime step, do not produce artifacts, and are not counted as completed.
An entirely skipped parallel group does not create a worker pool.

Conditions never bypass configuration validation, production risk requirements,
or exact production confirmation. Static preflight remains conservative and checks
all declared leaves, including conditional ones.

## Step retries

Extended leaf records can also declare an opt-in retry policy:

```yaml
- step: appstore.upload_testflight
  with: {}
  retry:
    max_attempts: 3
    delay_seconds: 1
```

`retry` belongs to the envelope, never to `with`: constructor options of the step
are unchanged. `max_attempts` is an integer from 1 to 5 (default 1);
`delay_seconds` is a finite number from 0 to 60 seconds (default 0). Unknown
retry fields, booleans in place of integers, and non-finite numbers are rejected
before execution.

Retries require an explicit capability from the step author:

- `max_attempts` above 1 is accepted only for steps whose metadata declares
  `retry_safe: true` (SDK: `@step(..., retry_safe=True)`).
- The capability is never inferred from `risk: safe` and is never added to
  built-in steps automatically. Built-ins keep their existing service-level
  retries: App Store Connect requests, Google Play reads, and other internal
  retry budgets are unchanged, and no upload, push, publication, webhook, or
  hook step gained a new automatic retry.

A retryable failure is a special `RetryableStepError` raised by the step itself.
It means a transient failure after which rerunning the whole step is safe.
SDK contract: before raising `RetryableStepError`, a step must leave the
pipeline context and its external effects in a state from which rerunning the
whole step is safe. Neither the executor nor the retry policy performs any
generic rollback of partial effects. Ordinary exceptions, validation errors,
cancellation, and ambiguous mutation results are never retried.

Execution semantics:

- Attempts are bounded by `max_attempts` with a fixed `delay_seconds` pause
  between attempts; each attempt constructs a fresh runtime step instance.
- The policy applies identically to sequential and parallel leaves.
- A leaf is completed only after a successful attempt. Intermediate failures
  are not terminal failures: the status file records `step_attempts` with the
  attempt count and the redacted last error per leaf.
- Retries never start for skipped leaves, and a completed leaf is never
  re-executed. An explicit resume of an unfinished step starts a new bounded
  attempt cycle; the saved budget is not carried over.

`cdt pipeline plan` and `cdt pipeline inspect` show each leaf's `retry` policy,
and plans include `retry_safe` in the step metadata.

### Step timeouts

Extended leaf records can also declare a hard external deadline for the step's
own process:

```yaml
- step: hook.python_script
  with: {script: hooks/slow.py}
  timeout_seconds: 30
```

`timeout_seconds` must be a positive finite number of seconds. Like `retry`, it
belongs to the envelope, never to `with`.

Timeouts require an explicit capability from the step author:

- `timeout_seconds` is accepted only for steps whose metadata declares
  `timeout_option` (SDK: `@step(..., timeout_option="timeout")`). The capability
  names an existing native constructor parameter of the step; the envelope value
  is passed to that parameter, and the step itself implements the actual
  behaviour.
- Setting `timeout_seconds` and the same parameter in `with` at the same time is
  rejected as ambiguous before execution.
- The envelope timeout is a hard deadline enforced by CDT with process-group
  termination. On platforms without POSIX process groups CDT rejects the
  envelope timeout instead of promising a process-tree guarantee it cannot
  keep.

The capability says nothing about *how* the step times out internally. A native
operation timeout (for example `ASC_WAIT_TIMEOUT_SEC` for App Store Connect
waits) is cooperative: the step manages its own requests and reports a clean
failure. The envelope `timeout_seconds` is fundamentally different: it is a
hard external deadline after which CDT terminates the step's process tree.
Plain Python steps run in CDT's own interpreter, so a Python step without a
declared native timeout parameter has no general deadline and cannot be force-
stopped; do not use `timeout_seconds` as a substitute for a native timeout.

The first built-in consumer is `hook.python_script`, which declares
`timeout_option: timeout`:

- The legacy single-key form `- hook.python_script: {timeout: 30}` is unchanged:
  `timeout` stays a constructor option, `timeout: null` disables the timeout,
  and the default is 30 seconds.
- The envelope form above is equivalent to the legacy form plus the managed
  termination guarantees below.
- On POSIX the hook runs in its own process group. On timeout CDT sends TERM to
  the whole group, waits a bounded grace period, then sends KILL, and always
  reaps the direct process. The same cleanup runs when the wait is interrupted
  (for example by Ctrl+C). Children that leave the process group on their own
  are outside this guarantee.
- On platforms without POSIX process groups, the envelope timeout is rejected;
  the legacy `timeout` option keeps the historical platform behaviour there
  (only the direct child process is guaranteed to be stopped).

A timeout failure is an ordinary step failure, never a retryable
`RetryableStepError`: retries require the separate `retry_safe` capability and
an explicit `RetryableStepError` from the step. `fail_on_error` and
`strict_outputs` checks behave as before, and a failed cleanup is reported
instead of being hidden behind the timeout error.

`cdt pipeline plan` and `cdt pipeline inspect` show each leaf's
`timeout_seconds`, and plans include `timeout_option` in the step metadata.

## Build step timing

Leaves whose step metadata declares `risk: "build"` — the built-in Android
AAB/APK, Flutter IPA, Xcode IPA, web build, and Python distribution build steps
— are measured around the whole leaf call. One entry is written to the
`build_timings` status field, indexed by the leaf step ID, with `name`,
`started_at`, `finished_at`, `duration_seconds`, and `outcome` (`success`,
`failed`, or `cancelled`). The measurement starts when the leaf actually starts
and includes option resolution, retries, retry delays, and artifact
registration; it is elapsed wall time from a monotonic clock, not CPU time or
pure compiler time. A running entry serializes only `name` and UTC
`started_at` with the remaining fields `null`; if the process is killed, the
entry stays unfinished instead of inventing a duration.

The field is telemetry only. Build steps always invoke their build tool on
every execution, even when the output artifact already exists: CDT has no
up-to-date check, no fingerprinting, and no cache that could skip a build (see
[Build performance: comparing repeated builds](build-performance.md) for the
deliberate decision and how to measure). Conditionally skipped leaves and
leaves completed in an earlier run get no new measurements, and each run record
contains only the timings of its own run.

`cdt status --json`, human-readable `cdt status`, and `agent-release status`
surface `build_timings`; older status payloads without the field stay readable.
See [Run records: status lifecycle](runs.md#status-lifecycle) for the exact
serialization rules.

## CDT self-release pipeline

The CDT repository uses its own `cdt.yaml` production pipeline named `release` for its own releases. The version is always explicit:

```bash
cdt pipeline list
cdt pipeline inspect release
cdt pipeline preflight release
cdt run release --input version=X.Y.Z --dry-run
cdt run release --input version=X.Y.Z --confirm release
```

The dry run plans the pipeline without executing steps or creating run records. The real run executes, in order: `git.require_synced_main`, `release.require_version_available`, `python.ruff_check`, `python.pytest`, `python.prepare_release`, `python.build_distribution`, `git.release_commit`, `git.release_tag_push` (atomic branch+tag push), and `github.wait_release`. The pipeline succeeds only after the GitHub Actions workflow is green, the GitHub Release ships wheel/sdist/`SHA256SUMS`, and the exact version is available on PyPI with wheel/sdist files.

Required tools for the self-release: `git`, `gh` (authenticated via an active `gh auth` session), `ruff`, `pytest`, `python -m build`, and `twine`; the preflight also queries the public PyPI JSON API. From a fresh checkout without a global install, the same commands work through the repository virtualenv bootstrap, for example `.venv/bin/cdt run release --input version=X.Y.Z --dry-run`.

## Planning and dry runs

Use `cdt pipeline plan <pipeline>` to show the static step tree, parallel groups, options, and risk classification without executing any step. Use `--json` for agent- and CI-friendly output.

```bash
cdt pipeline plan prod
cdt pipeline plan prod --json
cdt pipeline plan preview --input deploy=yes --json
```

Plans show each leaf's `when` and `decision` (`run`, `skip`, or `unknown`). With no
`--input`, conditional decisions are `unknown`. Supplying inputs makes the set
explicit: omitted optional inputs are absent, not unknown. Dry-run always uses an
explicit set, even when empty: `cdt run preview --dry-run` skips the example above.
Input declarations are shown, not the supplied runtime values.

`cdt run <pipeline> --dry-run` uses the same planner and does not call step execution code. It is intended as a safe preflight before real release, upload, deploy, or git-push work. Dry runs do not create run records.

Every real execution creates `.cdt/runs/<run-id>/` with `manifest.json`, `status.json`, `output.log`, `exit-code`, and process metadata. Direct `cdt run <pipeline>` output remains interactive and readable; use `cdt history`, `cdt status <run-id>`, and `cdt logs <run-id>` only when later inspection is useful.

JSON plans include compact step metadata plus `artifact_flow` for each step. `artifact_flow.produces_types` lists static result types a step creates, such as `ios_ipa`, `android_aab`, `web_build`, `version`, `upload_result`, `notification`, `tracker_comment`, or `file`. `artifact_flow.requires` is a list of grouped requirement entries:

```json
{
  "types": ["ios_ipa"],
  "mode": "all",
  "names": ["ios_ipa"]
}
```

`mode` is `all` when every listed type is required, or `any` when at least one is acceptable. `names` are best-effort artifact names inferred from static string options in `cdt.yaml` (for example, `artifact: ios_ipa`). Dynamic interpolations such as `${values.ios_artifact}` are ignored for static analysis. `artifact_flow.requires_names` and `produces_names` are flattened convenience lists.

`artifact_flow` describes declared capabilities, not proof that a step will run.
Only `run` producers contribute guaranteed available names; `skip` and `unknown`
producers do not. Active consumers retain missing-artifact warnings, while skipped
consumers need no artifacts.

Artifact-flow warnings are preflight hints and do not block execution by themselves. A missing required artifact warning means a step refers to an artifact name that no previous sequential step declares. Parallel branches start together, so a branch cannot consume an artifact produced by a sibling branch; produce the artifact before the parallel group or consume it after the group completes.

## Steps and parallel groups

Each item in `steps` is one of:

- Step name string: `- flutter.pub_get`
- Single-key step mapping: `- android.build_aab: { profile: prod }`
- Extended leaf mapping: `- step: flutter.pub_get` with optional `with`, `when`, and `retry`
- Parallel group: `- parallel: { steps: [...] }`
- Sequential group: `- sequence: { steps: [...] }`

A `sequence` runs its child steps in order and can be used as a branch of `parallel`. This lets independent platform flows run concurrently while preserving dependencies inside each branch:

```yaml
- parallel:
    steps:
      - sequence:
          steps:
            - ios.flutter_build_ipa: {profile: prod, artifact: ios_ipa}
            - appstore.upload_testflight: {artifact: ios_ipa, changelog: prod build}
      - sequence:
          steps:
            - android.build_aab: {profile: prod, artifact: android_aab}
            - artifact.copy_to_downloads: {artifact: android_aab}
            - android.build_apk: {profile: prod, artifact: android_apk}
            - artifact.copy_to_downloads: {artifact: android_apk}
- notify.prod_user_agent
- notify.success
```

Here APK starts as soon as AAB and its copy step finish; it does not wait for iOS. Nested groups inside `sequence` and nested `parallel` groups are not supported in schema v1.

Parallel branches start together, already-started branches are not cancelled on failure, and errors are aggregated after all branches finish.

Parallel context limitations:

- `ctx.artifacts` registration is thread-safe.
- `ctx.values` is a mutable mapping (ordinary dictionaries are still accepted by the context constructor). Unlike the former shared dictionary, each branch receives an isolated snapshot made before workers start. Steps in one `sequence` see earlier writes in that branch; siblings never see them.
- After every branch succeeds, CDT atomically merges changed keys, including deletions. Identical final writes or deletions are allowed; different writes or a deletion versus a write conflict. Conflicts report key names and step IDs, not values. Failure or conflict leaves the root values unchanged.
- Only `values` is isolated. Other context fields, filesystem changes and external service effects are not transactional and are not rolled back by a failed values merge. Registered artifacts remain available even after failure.
- Keep values non-secret and JSON-compatible (the supported contract is string keys and string values). Successful leaf boundaries are checkpointed for [safe resume](runs.md#parallel-values-checkpoints); a redacted checkpoint cannot be restored.
- Parallel artifact dependencies follow the same rule: branches can only consume artifacts that existed before the group started, while artifacts produced by branches become available after the group completes.

## Built-ins

Important built-ins include:

- `flutter.increment_build_number`
- `flutter.pub_get`
- `ios.flutter_build_ipa`
- `android.build_aab`
- `android.build_apk`
- `appstore.upload_testflight`
- `appstore.upload_testflight_ipa`
- `appstore.complete_testflight`
- `appstore.update_metadata`
- `firebase.ensure_cli`
- `firebase.deploy`
- `google_play.upload_aab`
- `artifact.copy_to_downloads`
- `hook.python_script`
- `notify.prod_user_agent`
- `notify.success`
- `notify.webhook`

Build steps use `profile` for CDT presets (`prod` adds `ENV=prod`). Flutter `flavor` is separate and optional. Build steps default to `no_pub: true` and do not increment versions; add explicit `flutter.increment_build_number` and `flutter.pub_get` steps when needed.

iOS builds are signed by your Xcode/Flutter project configuration, not by CDT; see the [iOS code signing recipe](ios-signing.md) for a local and CI setup on the existing interfaces.

Custom steps come from ordinary Python plugin modules listed in `plugins:`; see [Reusable Python step plugins](plugins.md) and `examples/reusable-plugin/`.

`artifact.copy_to_downloads` copies a named file artifact to `~/Downloads` by default.

`notify.prod_user_agent` is separate from `notify.success`. When `NOTIFY_PROVIDER=pachca`, it sends production user-agent details using `PACHCA_USER_AGENT_WEBHOOK_URL` and `UA_APP_NAME`; optional formatting variables are `UA_TITLE`, `UA_IOS_DEVICE`, and `UA_ANDROID_DEVICE`. With another provider the step is a no-op.

`notify.webhook` is the generic, provider-agnostic notification step described in "Generic webhook" below. Telegram/Pachca behaviour of `notify.success` is unchanged.

## TestFlight upload and completion

There are two ways to configure a TestFlight upload:

- `appstore.upload_testflight` remains fully supported and keeps its previous full-cycle behavior: it uploads the IPA with `iTMSTransporter` and then performs the post-upload App Store Connect processing (find the build, wait for processing, set the changelog) inside one step. Existing pipelines do not require migration.
- The recommended configuration splits the same work into two resumable steps, so a failure after a successful upload can be fixed without re-uploading the IPA:

```yaml
- sequence:
    steps:
      - ios.flutter_build_ipa:
          profile: prod
          artifact: ios_ipa
      - appstore.upload_testflight_ipa:
          artifact: ios_ipa
      - appstore.complete_testflight:
          changelog: prod build
```

`appstore.upload_testflight_ipa` requires an `ios_ipa` artifact, `xcrun`, and the ASC credentials `ASC_KEY_ID`, `ASC_ISSUER_ID`, and `ASC_PRIVATE_KEY_PATH`. It only runs the transporter upload and does not touch build status or changelog.

`appstore.complete_testflight` requires the ASC credentials plus `IOS_BUNDLE_ID`, but no artifact and no `xcrun`. It performs only the post-upload processing: it parses the build number from the saved pipeline version context `new_version` (for example `1.2.3+726`), queries App Store Connect for that build of the configured app, waits for a terminal processing state (`VALID`), and idempotently creates or updates the `en-US` TestFlight changelog (PATCH for an existing localization, POST for a missing one). It never uploads an IPA and never changes the build number, so rerunning completion for an already uploaded build is safe. It is also the resume entry point after a finished upload; see [Resuming a failed TestFlight upload](#resuming-a-failed-testflight-upload).

### Resuming a failed TestFlight upload

If `appstore.upload_testflight_ipa` completed but `appstore.complete_testflight` failed (for example, App Store Connect was unreachable or processing timed out), resume from the failed run's status file:

```bash
cdt run prod \
  --resume-status-file .cdt/runs/<run-id>/status.json \
  --skip-completed
```

Resume skips all completed steps, including the IPA build and the transporter upload, and starts at `appstore.complete_testflight`. The pipeline version context (`new_version`) is restored from the status file, so the IPA is not re-uploaded and the build number is not changed. The same applies to the compatible full-cycle `appstore.upload_testflight`: a rerun with `--skip-completed` skips the whole step when it is already recorded as completed, while a step failed during post-upload processing reruns from its start.

### App Store Connect request resilience

All App Store Connect requests in the TestFlight steps use a token-aware client with bounded retries. This applies to both the compatible `appstore.upload_testflight` and the split `appstore.upload_testflight_ipa` / `appstore.complete_testflight` pair.

Transient failures are retried up to 4 attempts per request with bounded exponential backoff (1s base, capped at 15s, plus up to 0.5s jitter). Transient means network-level failures such as timeouts, SSL errors (including SSL EOF), connection reset/abort, and other temporary OS-level socket errors, plus HTTP 429 and temporary 5xx responses.

- HTTP 429 and temporary 5xx honor a `Retry-After` header, either as a delay in seconds or as an HTTP-date. The effective delay is still capped at 15 seconds and never sleeps past the remaining wait deadline.
- Permanent HTTP 4xx responses are not retried. They fail immediately with the HTTP status and a safe response body. The only exception is HTTP 401: the JWT is force-refreshed once and the request is retried a single time; a second 401 fails immediately.
- The JWT is cached and refreshed automatically before its 20 minute expiry, so long waits do not fail because of token age.
- `ASC_WAIT_TIMEOUT_SEC` (integer, default `30`) sets the overall completion wait for the build to appear and finish processing in TestFlight. It bounds the whole wait, and request retry delays are clamped to the same deadline so retries cannot extend it.
- Progress is written to the run log as `==> ASC transient failure, attempt N/4 (category); retrying in Xs` lines. When the retry budget is exhausted, the error names the attempt count and the last failure category. No credentials or authorization headers are included in these messages.

## App Store review submission

`appstore.submit_review` continues a finished TestFlight upload: it creates or reuses the App Store version, binds the exact verified build, fills the localized "What's new" text, sets the release mode and phased release, and sends the version for App Store review through the current ASC `reviewSubmissions` flow. It never uploads an IPA, needs no Flutter, Xcode or `xcrun` tooling, and never changes the build number.

There are two equivalent configurations. Add the step after `appstore.complete_testflight` in an existing production pipeline so every release is submitted right after the upload completes:

```yaml
- sequence:
    steps:
      - ios.flutter_build_ipa:
          profile: prod
          artifact: ios_ipa
      - appstore.upload_testflight_ipa:
          artifact: ios_ipa
      - appstore.complete_testflight:
          changelog: prod build
      - appstore.submit_review:
          whats_new:
            ru: "${inputs.whats_new}"
          release_mode: manual
          phased_release: true
```

Or declare a standalone `submit-review` pipeline that submits an already uploaded build later, without rebuilding or re-uploading anything (see [Which build is submitted](#which-build-is-submitted)):

```yaml
  submit-review:
    risk: production
    inputs:
      whats_new:
        required: true
    steps:
      - appstore.submit_review:
          whats_new:
            ru: "${inputs.whats_new}"
          release_mode: manual
          phased_release: true
```

```bash
cdt run submit-review --input whats_new="Исправления ошибок и улучшения" --confirm submit-review
```

### Required options

All three options are mandatory and validated before the step runs; a submission can never start with an unspecified text, release mode or phased-release choice:

| Option | Required | Description |
|---|---|---|
| `whats_new` | yes | Non-empty mapping from locale to non-empty text, for example `{ru: "Исправления ошибок"}`. Supports the existing `${inputs.*}` interpolation. Only the listed locales are written; other localizations stay untouched. |
| `release_mode` | yes | `manual` or `automatic`. No default is applied. |
| `phased_release` | yes | A real boolean (`true`/`false`). No default is applied, and quoted strings such as `"false"` are rejected instead of silently counting as a choice. |

The app is taken from `IOS_BUNDLE_ID`; authentication uses the existing `ASC_KEY_ID`, `ASC_ISSUER_ID`, and `ASC_PRIVATE_KEY_PATH`. No manual app, version, or build input is needed.

### Which build is submitted

The exact build is determined automatically:

- inside a pipeline, the current version context wins: `new_version` (for example `1.2.3+456`) set by the build steps and restored from the status file on resume;
- a standalone run with no version context reads the record of the last successfully completed TestFlight completion for this bundle ID in this checkout, stored at `.cdt/appstore/uploads/<bundle-key>.json` (the key is a SHA-256 of the bundle ID, so two apps never overwrite each other).

When neither source is available, the step fails with an explicit error telling you to run `appstore.upload_testflight` or `appstore.complete_testflight` for the app first. CDT never picks an arbitrary latest Apple build and never substitutes a previous build for a missing one. The selected build is re-verified in App Store Connect on every submission — the local record is only the source of the choice, never the proof.

Records from runs made before this functionality do not exist. To create a record for an already uploaded build, rerun the completion of that known build with the version context restored from the old run's status file:

```bash
cdt run prod \
  --resume-status-file .cdt/runs/<run-id>/status.json \
  --resume-from appstore.complete_testflight
```

Completion is idempotent: it re-finds the build, confirms processing, and writes the record without re-uploading the IPA or changing the build number.

### Production confirmation

Every pipeline containing `appstore.submit_review` requires `risk: production` — including the step nested inside `sequence` or `parallel` — and every real run needs the exact CLI confirmation (`cdt run <pipeline> --confirm <pipeline>`), both direct and detached. Existing pipelines do not start submitting versions for review automatically: the shipped `prod` pipeline keeps only building and uploading to TestFlight until you explicitly add the step.

Planning commands (`cdt pipeline inspect`, `cdt pipeline plan`, `cdt run --dry-run`) never contact Apple and never create checkpoints or upload records.

### What manual/automatic and phased release mean

Both `release_mode` values describe what happens **after Apple approves** the version:

- `manual` — the version is held for an explicit release in App Store Connect after approval;
- `automatic` — the version is released to users automatically once Apple approves it.

`phased_release: true` enables Apple's standard seven-day phased release for automatic updates: the update rolls out to an increasing percentage of users over seven days. It is not an absolute prohibition on getting the update earlier — a user can still update manually in the App Store. `phased_release: false` submits the version without the seven-day rollout.

Apple does not support phased release for some versions, for example the first version of an app. In such cases CDT reports Apple's error and stops instead of silently changing the chosen release behavior.

### What to prepare before submitting

- The app card (description, screenshots, prices, availability) and all other mandatory review data must already exist in App Store Connect. CDT does not edit them; missing mandatory data surfaces as a blocking error from Apple during review.
- Every locale in `whats_new` must already exist as a version localization. Unknown locales fail with an explanation to prepare them in App Store Connect first — CDT does not create partial app cards.
- The version must be editable (`PREPARE_FOR_SUBMISSION`). Rejected, already-submitted, awaiting-release, and unknown version states stop with an explanation; CDT never resubmits or overwrites such versions automatically.

### When the step stops instead of submitting

CDT refuses to guess and stops without external changes when:

- no build can be identified (no current version context and no saved record for the app);
- the App Store version already has a different build selected — an existing build selection is never overwritten automatically;
- an open review submission belongs to another version, or a submission contains items of other versions — CDT does not add the version to a foreign submission and does not submit it;
- an unfinished submission operation with different parameters exists for the same app, or another submission for the app is running in this checkout (per-app lock);
- a response from Apple was lost and the outcome cannot be established with read-only requests — the operation is saved as blocked and requires verification in App Store Connect.

### Checkpoints, resume and unknown outcomes

Every submission writes a versioned checkpoint under `.cdt/appstore/operations/<operation-id>.json`; the operation ID derives from the app, platform, version, build number and the canonical submission parameters. The checkpoint stores only non-secret values and is written atomically before every external change — nothing is sent to Apple if the checkpoint could not be saved. A per-app lock file under `.cdt/appstore/locks/` serializes submissions within this checkout.

- Resume (`cdt run <pipeline> --resume-status-file .cdt/runs/<run-id>/status.json --skip-completed`) skips confirmed steps and continues an unfinished operation from its recorded phase; every re-run re-verifies the checkpoint against App Store Connect, so the local phase is never treated as proof.
- An identical already-confirmed operation is re-verified remotely and returns its stored result without submitting again; changed parameters while an operation is unfinished stop with an explicit conflict.
- After a lost response, the step verifies the version, settings, and submission composition with GET requests. An accepted submission completes the operation without repeating the submit request; a remaining ambiguity saves a blocked state and fails with an explicit message naming the checkpoint.
- The safe reaction to a blocked result is verifying the version and the submission in App Store Connect. Deleting a checkpoint is **not** a safe way to retry: it only erases CDT's memory of changes that may already have been applied remotely.

A successful step means Apple accepted the submission and it left the unsubmitted stage — **not** that Apple approved the version or that users can download it. The final message states this distinction explicitly; approval and the actual release happen later and outside this step.

See [Run records → App Store review checkpoints](runs.md#app-store-review-checkpoints) for how checkpoints interact with run status and resume.

## App Store metadata updates

`appstore.update_metadata` updates localized App Store metadata texts — `description`, `keywords`, `promotional_text` and `whats_new` — of existing localizations of an existing iOS App Store version. It is independent of `appstore.submit_review`: it never creates a version, a localization or a review submission, never selects a build and never sends anything for review. It also does not upload anything and needs no IPA, Flutter, Xcode or `xcrun` tooling.

```yaml
  metadata:
    risk: production
    inputs:
      version:
        required: true
      whats_new:
        required: true
    steps:
      - step: appstore.update_metadata
        with:
          version: "${inputs.version}"
          localizations:
            ru:
              description: "Описание приложения"
              keywords: "ключевое,слово,приложение"
              promotional_text: "Промо-текст"
              whats_new: "${inputs.whats_new}"
            en-US:
              whats_new: "Bug fixes and improvements"
```

```bash
cdt run metadata --input version=1.2.3 --input whats_new="Исправления ошибок" --confirm metadata
```

### Required options

| Option | Required | Description |
|---|---|---|
| `version` | yes | Non-empty version string of the existing iOS App Store version, for example `"1.2.3"`. Supports the existing `${inputs.*}` interpolation. |
| `localizations` | yes | Non-empty mapping from locale to a non-empty mapping of fields. Supported fields: `description`, `keywords`, `promotional_text`, `whats_new` (written to the ASC attributes `description`, `keywords`, `promotionalText`, `whatsNew`). |

Field values must be strings: numbers, booleans and `null` are rejected instead of being silently converted to text. An empty string is kept as an explicit clear request and sent to Apple only when App Store Connect accepts empty values for that field. The app is taken from `IOS_BUNDLE_ID`; authentication uses the existing `ASC_KEY_ID`, `ASC_ISSUER_ID`, and `ASC_PRIVATE_KEY_PATH`.

### What the step requires and never does

- The version is looked up exactly (paginated, with client-side verification) and must already exist: a missing version stops the step — CDT never falls back to get-or-create and never uses the last uploaded TestFlight build to guess the version.
- The version must be in `PREPARE_FOR_SUBMISSION`. Rejected, submitted, awaiting-release and unknown states stop with an explanation; CDT never modifies such versions automatically.
- Before the first mutation the step verifies that every requested locale already exists as a version localization. One unknown locale fails the whole step without any partial update — CDT does not create partial app cards.

### Minimal, verified updates

The step reads the current localizations first and PATCHes only the values that actually differ from the request; already matching fields are skipped, so a repeated run with an unchanged request performs no mutations at all. Locales are processed in stable sorted order, and every PATCH is issued with ambiguous-retry disabled (`retry_ambiguous=False`).

After every PATCH the step verifies the result by reading the localization back:

- a lost or ambiguous PATCH response is accepted only when the read-back confirms every requested value;
- an unverifiable or mismatching result fails the step with an explicit "unverified result" message and no further PATCH is sent in that run — verify the localization in App Store Connect instead of retrying blindly.

A partially successful run is not rolled back: locales confirmed before a failure keep their new texts, and the failure message never claims a completed transaction. Nothing is submitted for review and no build is selected, in this or in any later run.

### Production confirmation and limits

Every pipeline containing `appstore.update_metadata` requires `risk: production` — recursively through `sequence` and `parallel`, including conditionally skipped steps — and every real run needs the exact CLI confirmation (`cdt run <pipeline> --confirm <pipeline>`), both direct and detached. Planning commands (`cdt pipeline inspect`, `cdt pipeline plan`, `cdt run --dry-run`) never contact App Store Connect.

The step declares no automatic retries and no envelope `timeout_seconds` capability: App Store Connect requests keep their own bounded retry settings. A successful run registers only safe summary values in the status file (bundle id, version string and the processed locale names) — never credentials and never the metadata texts.

## Google Play upload (AAB)

`google_play.upload_aab` uploads one named AAB artifact and creates one release on one explicitly chosen Google Play track through the Google Play Android Publisher API. It is unrelated to Firebase App Distribution and does not read Firebase credentials.

One-time setup happens in Google Cloud Console and Play Console, not in Firebase:

- Link the app to a Google Cloud project and enable the **Google Play Android Developer API** for that project.
- Grant the publishing identity Play Console permissions for the target app under **Users and permissions** (for example, release-to-testing or release-to-production rights). These are Play Console app permissions, not Firebase IAM roles.
- The application and the target track must already exist; CDT does not create applications or tracks and does not perform the initial app setup (store listing, Play App Signing) in Play Console.

Authentication uses Application Default Credentials. Set `GOOGLE_APPLICATION_CREDENTIALS` to a service-account JSON key (relative paths resolve from the project root) or rely on the ambient CI identity; `FIREBASE_TOKEN` is not used by this step. `cdt pipeline preflight <pipeline>` checks the configured ADC file read-only; an unset `GOOGLE_APPLICATION_CREDENTIALS` is not an error because ADC may come from the CI environment, and existing credentials never prove Play Console permissions.

Options:

| Option | Required | Description |
|---|---|---|
| `artifact` | yes | Name of an `android_aab` artifact registered by an earlier step. |
| `package_name` | yes | Explicit Android package name, for example `com.example.app`. |
| `track` | yes | Explicit existing track: `internal`, `alpha`, `beta`, `production`, or a custom closed-testing track. No default is applied. |
| `release_status` | yes | `draft`, `inProgress` or `completed`. No default is applied. |
| `release_notes` | no | Mapping from language codes to non-empty localized texts, for example `{en-US: "Bug fixes"}`. |
| `release_name` | no | Human-readable release name. |
| `user_fraction` | no | Staged rollout share. Required with `0 < user_fraction < 1` for `inProgress`; forbidden for `draft` and `completed`. Numeric strings from `${inputs.*}` are validated after interpolation; booleans, NaN, infinity and non-numeric values are rejected. |

### Google Play production pipelines

Every Google Play step requires `risk: production` — including steps nested inside `sequence` or `parallel` — and every real run needs the exact CLI confirmation (`cdt run <pipeline> --confirm <pipeline>`). The four common shapes (build steps shown for context):

Internal testing track:

```yaml
  play-internal:
    risk: production
    steps:
      - android.build_aab: {profile: prod, artifact: android_aab}
      - google_play.upload_aab:
          artifact: android_aab
          package_name: com.example.app
          track: internal
          release_status: completed
          release_notes: {en-US: "Internal testing build"}
```

Draft release (saved only, never sent for review):

```yaml
  play-draft:
    risk: production
    steps:
      - android.build_aab: {profile: prod, artifact: android_aab}
      - google_play.upload_aab:
          artifact: android_aab
          package_name: com.example.app
          track: beta
          release_status: draft
          release_notes: {en-US: "Nightly candidate"}
```

Full production rollout:

```yaml
  play-production:
    risk: production
    steps:
      - android.build_aab: {profile: prod, artifact: android_aab}
      - google_play.upload_aab:
          artifact: android_aab
          package_name: com.example.app
          track: production
          release_status: completed
          release_notes:
            en-US: "Stable release"
```

Staged production rollout with an explicit fraction input:

```yaml
  play-production-staged:
    risk: production
    inputs:
      fraction:
        required: true
        pattern: '^0\.\d+$'
    steps:
      - android.build_aab: {profile: prod, artifact: android_aab}
      - google_play.upload_aab:
          artifact: android_aab
          package_name: com.example.app
          track: production
          release_status: inProgress
          user_fraction: ${inputs.fraction}
          release_notes:
            en-US: "Gradual rollout"
```

Run them with the exact pipeline name as confirmation:

```bash
cdt run play-internal --confirm play-internal
cdt run play-draft --confirm play-draft
cdt run play-production --confirm play-production
cdt run play-production-staged --input fraction=0.05 --confirm play-production-staged
```

Planning, dry runs, `cdt pipeline plan`, `cdt pipeline inspect`, and schema generation never touch credentials, the network, or checkpoints.

### What a Google Play release status means

- `draft`: the release is saved in Play Console only. It was not sent for review, nothing is published, and no user can install the build. CDT explicitly reports that a draft was created and nothing else happened.
- `inProgress` / `completed`: CDT commits the edit with `changesInReviewBehavior=ERROR_IF_IN_REVIEW`, which sends the changes through Google's standard review and publication flow. Google accepting the changes is **not** review approval, and approval is **not** actual availability to users; CDT verifies neither and says so in its final message. Committing never cancels a review that is already in progress — Google rejects such a commit and CDT stops.
- Staged rollout (`inProgress` with `user_fraction`) creates the new release next to the previous completed release, which stays available as the base release while Google gradually assigns users to the new version.

### Managed publishing and the manual Publish

Managed publishing is switched on and off manually in Play Console. Where managed publishing applies to the changes, the final **Publish** after Google's approval is also pressed manually in Play Console. CDT automates neither action and does not infer the mode from indirect signs: the final message of a non-draft release explains both possibilities (Google continues the release rollout automatically, or a manual Publish is still required) instead of claiming which mode is enabled. A successful commit therefore never means the app is released to users.

### When the Google Play step stops instead of publishing

CDT refuses to guess and stops without replacing existing state when:

- the target track already contains a draft release, an `inProgress` or `halted` release, a staged rollout with a fraction other than 1.0, or more than one release — an unfinished release is never replaced, rollouts are never continued or completed, and builds are never promoted between tracks;
- the same package already has an unfinished publication operation with different parameters (checkpoint conflict);
- another publication for the same package is currently running in this checkout;
- Google rejects the commit because the app is under review — CDT surfaces the error as-is and never retries with flags that would skip sending the changes for review;
- Google reports any other error whose resolution lives in Play Console (missing permissions, app state, program policies) — CDT reports a safe reason and stops; it does not switch send modes or work around Console requirements automatically.

### Google Play checkpoints, resume and unknown outcomes

Every publication writes a versioned checkpoint under `.cdt/google-play/operations/<operation-id>.json`; the operation ID derives from the canonical publication parameters plus the AAB SHA-256, so identical parameters map to the same checkpoint. Checkpoints store only non-secret values (package, track, release parameters, hashes, edit metadata, version code, phase, confirmed result) and are written atomically before every external mutation — nothing is sent to Google if the checkpoint could not be saved.

- Resume (`cdt run <pipeline> --resume-status-file .cdt/runs/<run-id>/status.json --skip-completed`) skips confirmed steps and continues an unfinished operation from its recorded phase without repeating confirmed mutations. An already-confirmed identical operation returns its stored result without a single API call.
- A plain rerun without resume cannot bypass an unfinished operation either: identical parameters resume the same checkpoint, and changed parameters stop with an explicit conflict error.
- If an upload or commit response is lost, the step verifies the remote state with read-only requests and, when the outcome still cannot be established, fails with an explicit "publication result is unknown" error naming the checkpoint and instructing you to verify Bundle Explorer and the track pages in Play Console before doing anything else.
- Deleting a checkpoint is **not** a safe way to repeat a publication: it only erases CDT's memory of changes that may already have been applied remotely. The safe path is verifying Play Console and resolving any half-applied state there.

The lock file `.cdt/google-play/locks/<package>.lock` serializes publications of one package within this checkout only. It does not coordinate other machines, other checkouts, CI runners, or manual Play Console work: if the remote state changed elsewhere, CDT stops on the mismatch instead of overwriting it.

See [Run records → Google Play publication checkpoints](runs.md#google-play-publication-checkpoints) for how checkpoints interact with run status and resume.

## Firebase deploy

`firebase.ensure_cli` and `firebase.deploy` cover Firebase project deployments with the Firebase CLI. Both take no options.

`firebase.ensure_cli` is a cheap guard step: it runs `firebase --version` and fails early with an installation hint when the CLI is missing or broken. Put it first in a deploy pipeline so a missing tool fails before any build or upload work.

`firebase.deploy` runs `firebase deploy` in the project root with the `firebase` binary from `PATH`. Targets, hosting, rules and functions are defined by the project's own Firebase configuration (`firebase.json`, `.firebaserc`) — the step does not duplicate them in `cdt.yaml`. The step is registered with `risk: deploy`; when the CLI exits non-zero, CDT plays the fail sound (if configured, see [Terminal sounds](#terminal-sounds)) and fails the pipeline.

```yaml
- firebase.ensure_cli
- firebase.deploy
```

There is no `web.deploy` step. Web artifacts are built with `web.build`, optionally rewritten with `web.cache_bust` and placed with `web.copy`; hosting upload happens through `firebase.deploy` or your own tooling.

## Terminal sounds

Several built-in build, upload and deploy steps play a short fail sound when they fail (Flutter/iOS/Android builds, `firebase.deploy`, `firebase.upload_app_distribution`, `google_play.upload_aab`, App Store steps, `git.commit_push`, web steps). The legacy `cdt` flows also play a success sound when the whole flow completes.

Sounds are opt-in and configured through environment variables (the project `.env` works as everywhere else):

| Variable | Meaning |
|---|---|
| `SUCCESS_SOUND` / `FAIL_SOUND` | `macos` enables the sound; unset or empty keeps the terminal silent. Any other value prints a warning. |
| `SUCCESS_SOUND_FILE` / `FAIL_SOUND_FILE` | Optional custom sound file, absolute or relative to the project root. Defaults: the system `Glass.aiff` / `Basso.aiff`. |
| `SOUND_VOLUME` | Playback volume `0.0`–`1.0`; non-numeric values fall back to `0.3`, out-of-range values are clamped. |

Playback uses the macOS `afplay` tool and reports every problem only as a warning: a missing `afplay`, a missing custom file or a failed playback never changes the pipeline result. Do not rely on sounds in headless CI — nothing plays unless a mac with `afplay` explicitly opts in through these variables.

## Python hook

```yaml
- hook.python_script:
    name: fetch_offline_config
    script: cdt/hooks/fetch_offline_data.py
    args: []
    env:
      OFFLINE_API_URL: ${OFFLINE_API_URL}
      OFFLINE_OUTPUT: assets/offline_data.json
    outputs:
      - assets/offline_data.json
    timeout: 30
    fail_on_error: true
    strict_outputs: false
```

The script must exist inside the project root and runs as `python3 <script> [args...]` from the project root. Environment values come from `.env` plus the shell, with shell values taking priority. With `strict_outputs: true`, CDT checks tracked changes via `git diff --name-only` and permits only files listed in `outputs`.

The legacy `timeout` option defaults to 30 seconds; `timeout: null` disables it. On POSIX the hook runs in its own process group, and the timeout terminates the whole group (TERM, bounded grace, KILL, guaranteed reap of the direct process). On platforms without POSIX process groups only the direct child process is guaranteed to stop. The extended-record envelope `timeout_seconds` (see "Step timeouts") requires POSIX process groups and is rejected elsewhere.

## Generic webhook

`notify.webhook` delivers one explicitly configured JSON payload to an HTTPS endpoint:

```yaml
- step: notify.webhook
  with:
    url_env: RELEASE_WEBHOOK_URL          # required: env variable holding the HTTPS destination
    authorization_env: RELEASE_WEBHOOK_AUTH # optional: env variable holding the full Authorization header value
    payload:                              # required, non-empty JSON object, sent as-is
      text: "Release ${inputs.version} is out"
    timeout_seconds: 30                   # optional, default 30; also settable as the envelope timeout_seconds
    fail_on_error: true                   # optional, default true
```

Hard safety rules:

- The destination URL and the full `Authorization` header value are read only from the environment variables named by `url_env` and `authorization_env`; both must be plain variable names. The destination must be an HTTPS URL with a host, without userinfo or fragment; otherwise the step fails before sending.
- Exactly one POST via the standard library with verified TLS. Redirects are never followed and automatic retries never happen (the step is not `retry_safe`); a 3xx answer is a failure with its HTTP status.
- Only `2xx` responses count as successful. Network errors, timeouts and other statuses become a safe step error, or a warning when `fail_on_error: false`.
- The payload is exactly the configured object: its string fields support ordinary `${inputs.*}`/`${values.*}` interpolation, and nothing from env, inputs, artifacts or context is added automatically.
- If the payload contains the destination URL, the authorization value, or a known context secret (credential-like `.env` values), the step rejects it before sending instead of silently masking it - remove the secret from the payload explicitly.
- The destination URL, the authorization value and the response body are never read into messages or saved logs; failures report only a safe category (`network_error`, `timeout`, `ssl_error`, `http_error`) plus the HTTP status when the server answered.

Static preflight checks that the named env variables are present (literal names only; interpolated names are checked when the step runs). `cdt pipeline plan` and `cdt pipeline inspect` show option names, never credential values, and perform no network actions.

`notify.webhook` complements but does not replace `notify.success`: Telegram/Pachca behaviour and the provider selection via `NOTIFY_PROVIDER` are unchanged.
