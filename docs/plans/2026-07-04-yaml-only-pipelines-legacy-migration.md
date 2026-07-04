# CDT 0.2.0 Release Plan: YAML-only pipelines + legacy migration

## Summary

Перевести CDT на единую pipeline-модель через `cdt.yaml`, но добавить мигратор, чтобы не писать конфиг вручную.

Остаются команды:

- `cdt run <pipeline>`
- `cdt pipeline list/inspect/validate/steps`
- `cdt migrate legacy`
- `cdt --version`

Удаляются legacy команды:

- `cdt prod`
- `cdt test`
- `cdt deploy`
- `cdt ios-prod`
- `cdt ios-test`
- `cdt firebase_deploy`

`cdt.yaml` остаётся в root проекта. Project-local automation scripts/hooks рекомендуем хранить в `cdt/hooks/`.

## Key Changes

### Pipeline schema

Поддерживаем только schema v1:

```yaml
version: 1
plugins: [] # optional

pipelines:
  prod:
    steps: []
```

Без top-level `pre_build`, `offline_data`, default pipelines и `cdt pipeline init`.

### Legacy migration

Добавить:

```bash
cdt migrate legacy
cdt migrate legacy --dry-run
cdt migrate legacy --force
```

Мигратор создаёт/обновляет:

```text
cdt.yaml
cdt/hooks/.gitkeep
```

Генерирует pipelines:

| Legacy | Pipeline |
|---|---|
| `cdt prod` | `prod` |
| `cdt test` | `test` |
| `cdt deploy` | `deploy` |
| `cdt ios-prod` | `ios-prod` |
| `cdt ios-test` | `ios-test` |
| `cdt firebase_deploy` | `firebase-deploy` |

Правила merge:

- если `cdt.yaml` нет — создать полный файл;
- если есть — проверить `version: 1` и `pipelines`;
- добавить отсутствующие pipelines;
- существующие pipelines не менять;
- перезаписывать существующие pipelines только с `--force`;
- перед записью в существующий `cdt.yaml` всегда делать `cdt.yaml.bak`;
- `--dry-run` ничего не пишет и показывает итоговый YAML/план изменений;
- comments в существующем YAML не гарантируем сохранять при merge.

В generated `prod` добавить commented offline hook example с путём `cdt/hooks/fetch_offline_data.py`.

### Python hook step

Добавить built-in `hook.python_script`:

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

Правила:

- запускает `python3 <script> [args...]` из project root;
- `script` обязателен, должен существовать и быть внутри project root;
- `args` — list of strings;
- env priority: shell env > `.env`;
- `${VAR}` без значения — ошибка;
- `fail_on_error: true` по умолчанию;
- `strict_outputs: false` по умолчанию;
- если `strict_outputs: true`, проверять только tracked changes через `git diff --name-only`; разрешены изменения только в `outputs`; untracked игнорируются.

### Build/profile model

Переименовать build option `env` в `profile`:

```yaml
- ios.flutter_build_ipa:
    profile: prod
    flavor: prod
    artifact: ios_ipa
```

`profile` — CDT preset:

- `profile: prod` добавляет generic prod defaults, например `--dart-define=ENV=prod`;
- `profile` не означает Flutter `--flavor`;
- Flutter flavor задаётся отдельно через `flavor`;
- если проект не использует flavors, `flavor` можно не указывать.

Важно: убрать project-specific hardcoded default `STORE=ru` из CDT built-ins. Для сохранения legacy behavior мигратор явно добавляет его в generated `prod` pipeline для APK:

```yaml
- android.build_apk:
    profile: prod
    flavor: prod
    artifact: android_apk
    dart_defines:
      STORE: ru
```

### Versioning and pub get

Добавить явный step:

```yaml
- flutter.increment_build_number
```

Он:

- меняет только build number в `pubspec.yaml`;
- `1.2.3+7 → 1.2.3+8`;
- `1.2.3 → 1.2.3+1`;
- если version отсутствует или некорректный — понятная ошибка;
- пишет строки:
  - `ctx.old_version`
  - `ctx.new_version`
  - `ctx.values["flutter.version.old"]`
  - `ctx.values["flutter.version"]`
  - `ctx.values["flutter.build_number"]`.

Build steps больше не инкрементят version скрыто.

`flutter.pub_get` остаётся отдельным явным step. Build steps не запускают `flutter pub get` автоматически; по умолчанию используют `no_pub: true`.

### Feature-complete built-ins

Добавить/обновить:

- `flutter.increment_build_number`
- `flutter.pub_get`
- `ios.flutter_build_ipa`
- `android.build_aab`
- `android.build_apk`
- `appstore.upload_testflight`
- `artifact.copy_to_downloads`
- `notify.success`
- `hook.python_script`

`artifact.copy_to_downloads`:

- берёт artifact из `ctx.artifacts` по имени;
- destination default: `Path.home() / "Downloads"`;
- создаёт destination, если нет;
- копирует только файлы;
- если artifact path — директория, падает.

### Parallel and artifacts

`parallel`:

- запускает branches одновременно;
- ошибка в branch не отменяет уже запущенные branches;
- ждёт все branches;
- агрегирует ошибки.

Artifacts:

- in-memory `ctx.artifacts`;
- missing artifact → ошибка;
- duplicate artifact name → ошибка;
- artifacts из parallel branches пишутся в общий context.

Artifact JSON/debug model:

```json
{
  "name": "ios_ipa",
  "path": "build/ios/ipa/Runner.ipa",
  "kind": "ipa",
  "step": "ios.flutter_build_ipa"
}
```

### Pipeline commands

`cdt pipeline steps` показывает:

- all built-in steps;
- plugin steps, если `cdt.yaml` есть и plugins загрузились.

`cdt pipeline inspect --json` сохранить совместимым и добавить:

```json
{
  "schema_version": 1
}
```

Stable fields:

- `schema_version`
- `pipeline`
- `pipelines`
- `plugins`
- `steps`
- `registered_steps`
- `errors`

Unknown step validation должен предлагать closest matches, если они есть.

## Example prod pipeline

```yaml
version: 1

pipelines:
  prod:
    steps:
      # Uncomment and customize if you need offline data before prod build:
      # - hook.python_script:
      #     name: fetch_offline_config
      #     script: cdt/hooks/fetch_offline_data.py
      #     env:
      #       OFFLINE_API_URL: ${OFFLINE_API_URL}
      #       OFFLINE_OUTPUT: assets/offline_data.json
      #     outputs:
      #       - assets/offline_data.json

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

      - parallel:
          steps:
            - appstore.upload_testflight:
                artifact: ios_ipa
                changelog: prod build
            - artifact.copy_to_downloads:
                artifact: android_aab

      - android.build_apk:
          profile: prod
          flavor: prod
          artifact: android_apk
          dart_defines:
            STORE: ru

      - artifact.copy_to_downloads:
          artifact: android_apk

      - notify.success
```

## Test Plan

Проверить:

- package version `0.2.0`;
- legacy CLI commands удалены;
- `cdt run prod` загружает `pipelines.prod`;
- missing `cdt.yaml` / missing pipeline дают понятные ошибки;
- `cdt migrate legacy --dry-run`;
- migration creates `cdt.yaml` and `cdt/hooks/.gitkeep`;
- migration merge adds missing pipelines;
- migration does not overwrite existing pipelines unless `--force`;
- migration creates `cdt.yaml.bak` before modifying existing config;
- migration explicitly adds `STORE: ru` only in generated legacy-compatible APK step;
- built-in Android APK prod command does not hardcode `STORE=ru`;
- `hook.python_script`: path validation, cwd, args, env, timeout, failure;
- `strict_outputs` behavior;
- `flutter.increment_build_number`;
- build steps use `profile`, not `env`;
- `android.build_apk`;
- `artifact.copy_to_downloads`;
- duplicate/missing artifacts;
- `parallel` waits all branches and aggregates errors;
- `pipeline inspect --json` contains `schema_version`;
- unknown step validation suggests closest matches;
- `examples/cdt.yaml` validates.

## Documentation

Обновить:

- `README.md`
- `docs/pipelines.md`
- `examples/cdt.yaml`
- `CHANGELOG.md`

Добавить migration section:

```bash
cdt migrate legacy --dry-run
cdt migrate legacy
```

Migration table:

| Было | Стало |
|---|---|
| `cdt prod` | `cdt run prod` |
| `cdt test` | `cdt run test` |
| `cdt deploy` | `cdt run deploy` |
| `cdt ios-prod` | `cdt run ios-prod` |
| `cdt ios-test` | `cdt run ios-test` |
| `cdt firebase_deploy` | `cdt run firebase-deploy` |

Document recommended hook location:

```text
cdt/hooks/
```

Document legacy APK behavior:

> Legacy `cdt prod` added `--dart-define=STORE=ru` for Android APK. Pipeline built-ins no longer do this implicitly. `cdt migrate legacy` preserves this explicitly in generated YAML; remove or change it if not needed.

## Assumptions

- Release version: `0.2.0`.
- Breaking changes допустимы, потому что проект pre-1.0.
- `cdt pipeline init`, `notify.failure`, lifecycle hooks и remote plugins out of scope.
- Сложный pretty UI для parallel out of scope.
