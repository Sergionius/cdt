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
- `artifact.copy_to_downloads`
- `hook.python_script`
- `notify.prod_user_agent`
- `notify.success`

Build steps use `profile` for CDT presets (`prod` adds `ENV=prod`). Flutter `flavor` is separate and optional. Build steps default to `no_pub: true` and do not increment versions; add explicit `flutter.increment_build_number` and `flutter.pub_get` steps when needed.

`artifact.copy_to_downloads` copies a named file artifact to `~/Downloads` by default.

`notify.prod_user_agent` is separate from `notify.success`. When `NOTIFY_PROVIDER=pachca`, it sends production user-agent details using `PACHCA_USER_AGENT_WEBHOOK_URL` and `UA_APP_NAME`; optional formatting variables are `UA_TITLE`, `UA_IOS_DEVICE`, and `UA_ANDROID_DEVICE`. With another provider the step is a no-op.

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
