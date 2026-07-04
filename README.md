# CDT

CDT is a command line toolkit for Flutter release workflows driven by project-local YAML pipelines.

## Commands

```bash
cdt --version
cdt run <pipeline>
cdt pipeline list
cdt pipeline inspect <pipeline> --json
cdt pipeline validate [pipeline]
cdt pipeline steps
cdt migrate legacy --dry-run
cdt migrate legacy
```

Legacy direct commands (`cdt prod`, `cdt test`, `cdt deploy`, `cdt ios-prod`, `cdt ios-test`, `cdt firebase_deploy`) were removed in 0.2. Use pipelines instead.

## Minimal `cdt.yaml`

```yaml
version: 1

pipelines:
  prod:
    steps:
      - flutter.increment_build_number
      - flutter.pub_get
      - parallel:
          steps:
            - ios.flutter_build_ipa:
                profile: prod
                flavor: prod
                artifact: ios_ipa
            - android.build_aab:
                profile: prod
                flavor: prod
                artifact: android_aab
      - artifact.copy_to_downloads:
          artifact: android_aab
      - notify.success
```

See `examples/cdt.yaml` and `docs/pipelines.md` for a fuller prod pipeline, plugins, artifacts, and hooks.

## Migrate legacy projects

```bash
cdt migrate legacy --dry-run
cdt migrate legacy
```

| Было | Стало |
|---|---|
| `cdt prod` | `cdt run prod` |
| `cdt test` | `cdt run test` |
| `cdt deploy` | `cdt run deploy` |
| `cdt ios-prod` | `cdt run ios-prod` |
| `cdt ios-test` | `cdt run ios-test` |
| `cdt firebase_deploy` | `cdt run firebase-deploy` |

The migrator creates or merges `cdt.yaml`, creates `cdt/hooks/.gitkeep`, keeps existing pipelines unless `--force` is used, and backs up existing config to `cdt.yaml.bak` before writing.

Recommended hook location:

```text
cdt/hooks/
```

Legacy `cdt prod` added `--dart-define=STORE=ru` for Android APK. Pipeline built-ins no longer do this implicitly. `cdt migrate legacy` preserves this explicitly in generated YAML; remove or change it if not needed.

## Built-in steps

Use `cdt pipeline steps` for the complete list. Common built-ins:

- `flutter.increment_build_number`
- `flutter.pub_get`
- `ios.flutter_build_ipa`
- `android.build_aab`
- `android.build_apk`
- `appstore.upload_testflight`
- `artifact.copy_to_downloads`
- `hook.python_script`
- `notify.success`

Build steps use `profile` for CDT presets (`profile: prod` adds `--dart-define=ENV=prod`). Flutter `flavor` is separate. Build steps do not run `flutter pub get` or increment versions implicitly.

## Python hooks

```yaml
- hook.python_script:
    script: cdt/hooks/fetch_offline_data.py
    env:
      OFFLINE_API_URL: ${OFFLINE_API_URL}
    outputs:
      - assets/offline_data.json
```

Hooks run from the project root as `python3 <script>` and must stay inside the project root.
