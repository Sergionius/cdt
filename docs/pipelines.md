# CDT pipelines

CDT 0.2 uses `cdt.yaml` as the only automation model. Run pipelines with:

```bash
cdt run prod
cdt pipeline list
cdt pipeline inspect prod --json
cdt pipeline validate
cdt pipeline steps
```

## Schema v1

```yaml
version: 1
plugins: [] # optional

pipelines:
  prod:
    steps:
      - flutter.increment_build_number
      - flutter.pub_get
      - ios.flutter_build_ipa:
          profile: prod
          flavor: prod
          artifact: ios_ipa
```

Top-level fields are `version`, optional `plugins`, and `pipelines`. Legacy top-level lifecycle keys and default pipelines are not supported.

## Steps and parallel groups

Each item in `steps` is one of:

- Step name string: `- flutter.pub_get`
- Single-key step mapping: `- android.build_aab: { profile: prod }`
- Parallel group: `- parallel: { steps: [...] }`

Parallel branches start together, already-started branches are not cancelled on failure, and errors are aggregated after all branches finish.

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
- `notify.success`

Build steps use `profile` for CDT presets (`prod` adds `ENV=prod`). Flutter `flavor` is separate and optional. Build steps default to `no_pub: true` and do not increment versions; add explicit `flutter.increment_build_number` and `flutter.pub_get` steps when needed.

`artifact.copy_to_downloads` copies a named file artifact to `~/Downloads` by default.

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
