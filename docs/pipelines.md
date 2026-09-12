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
- Pass inputs as repeatable `--input KEY=VALUE` options on `cdt run` and `cdt agent-release start`. Unknown, duplicate, malformed (`no '='`), missing required, or pattern-violating inputs are rejected before any step runs.
- Step options interpolate inputs with `${inputs.<name>}`; referencing an undeclared input fails with a clear error.
- Inputs are non-secret by contract: never pass credentials as inputs. They are stored redacted in the run manifest and status and shown by `cdt pipeline inspect` / `cdt pipeline plan` as declarations only (never runtime values).
- Resume requires the same inputs as the original run; a release cannot be continued with a different version.

Pipelines without `inputs` keep the previous behavior; no migration is needed.

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
```

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

Artifact-flow warnings are preflight hints and do not block execution by themselves. A missing required artifact warning means a step refers to an artifact name that no previous sequential step declares. Parallel branches start together, so a branch cannot consume an artifact produced by a sibling branch; produce the artifact before the parallel group or consume it after the group completes.

## Steps and parallel groups

Each item in `steps` is one of:

- Step name string: `- flutter.pub_get`
- Single-key step mapping: `- android.build_aab: { profile: prod }`
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
- Writing to `ctx.values` from parallel branches is not guaranteed to be thread-safe.
- Parallel branches should not depend on each other through `ctx.values`; produce values before the parallel group or join via explicit artifacts/steps after it.
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
- `artifact.copy_to_downloads`
- `hook.python_script`
- `notify.prod_user_agent`
- `notify.success`

Build steps use `profile` for CDT presets (`prod` adds `ENV=prod`). Flutter `flavor` is separate and optional. Build steps default to `no_pub: true` and do not increment versions; add explicit `flutter.increment_build_number` and `flutter.pub_get` steps when needed.

`artifact.copy_to_downloads` copies a named file artifact to `~/Downloads` by default.

`notify.prod_user_agent` is separate from `notify.success`. When `NOTIFY_PROVIDER=pachca`, it sends production user-agent details using `PACHCA_USER_AGENT_WEBHOOK_URL` and `UA_APP_NAME`; optional formatting variables are `UA_TITLE`, `UA_IOS_DEVICE`, and `UA_ANDROID_DEVICE`. With another provider the step is a no-op.

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
