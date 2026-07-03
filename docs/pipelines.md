# CDT pipelines

`cdt.yaml` is a trusted project-local pipeline file. CDT imports configured
plugins and executes steps directly in the project process. Treat plugin
modules as trusted project-local Python code: review them like build scripts
before running a pipeline from an unfamiliar repository.

## Schema

```yaml
version: 1
plugins:
  - cdt_steps.offline
pipelines:
  deploy:
    steps:
      - git.prepare_clean_main
      - web.build:
          env: prod
      - parallel:
          steps:
            - ios.flutter_build_ipa:
                env: test
                artifact: ios_ipa
            - android.build_aab:
                env: test
                artifact: android_aab
```

Top-level fields:

- `version`: must be `1`.
- `plugins`: optional list of Python module names to import before validation or run.
- `pipelines`: mapping of pipeline names to `steps`.

Each item in `steps` is one of:

- Step name string: `- flutter.pub_get`
- Single-key step mapping: `- web.build: { env: prod }`
- Parallel group: `- parallel: { steps: [...] }`

Parallel groups run all child steps concurrently and join all of them. In v1, a
failed child does not cancel already-started siblings. After join, CDT reports
the failed child step names. Nested parallel groups are not supported.

## Plugin Steps

Project-local plugins are normal Python modules importable from the project
root. Add their module names under top-level `plugins:` in `cdt.yaml`:

```yaml
version: 1
plugins:
  - cdt_steps.offline

pipelines:
  offline-test:
    steps:
      - offline.fetch_config:
          output: build/offline/config.json
```

The plugin module registers a step with `@step` from `cdt.sdk`:

```python
from cdt.sdk import PipelineContext, step


@step("offline.fetch_config")
def fetch_config(ctx: PipelineContext, output: str):
    path = ctx.project_path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    ctx.values["offline_config_path"] = str(path)
```

`PipelineContext` is the runtime object passed to every step:

- `ctx.env`: project `.env` values.
- `ctx.runner`: command runner for subprocess calls.
- `ctx.artifacts`: named build artifacts registered by earlier steps.
- `ctx.values`: simple string values shared between steps and interpolation.

Plugin functions can also mutate the filesystem, run commands, and read secrets
available to the process. Keep plugin code in the project, commit only
non-secret examples, and do not load plugin modules from untrusted sources.

## Interpolation

Step options are resolved lazily immediately before the step runs:

- `${ENV_KEY}` reads from the project `.env`.
- `${values.name}` reads `ctx.values["name"]`.
- `${artifact.name.path}`, `${artifact.name.kind}`, and `${artifact.name.label}`
  read artifacts registered by previous steps.
- `${ids}` joins repeated CLI `--id` values with `, `.
- `${flutter.version}` reads the current Flutter version from `pubspec.yaml`.

`cdt pipeline validate` does not execute steps and does not resolve runtime-only
values produced by earlier steps. Those values are checked when the pipeline
runs.

## Built-in Steps

Use `cdt pipeline steps` or `cdt pipeline steps --json` for the exact list
available in the current project. Built-ins include:

- `flutter.pub_get`
- `ios.flutter_build_ipa`
- `ios.xcode_build_ipa`
- `android.build_aab`
- `appstore.upload_testflight`
- `firebase.ensure_cli`
- `firebase.deploy`
- `firebase.upload_app_distribution`
- `git.prepare_clean_main`
- `git.commit_push`
- `web.build`
- `web.copy`
- `web.cache_bust`
- `notify.success`
- `tracker.comment`

### Flutter Build Options

`ios.flutter_build_ipa` and `android.build_aab` accept shared Flutter build
options:

```yaml
- ios.flutter_build_ipa:
    env: test
    artifact: ios_ipa
    flavor: qa
    target: lib/main_qa.dart
    dart_defines:
      ENV: qa
      API: mock
    obfuscate: true
    split_debug_info: obfsymbols
    no_pub: true
    extra_args:
      - --export-method=ad-hoc

- android.build_aab:
    env: test
    artifact: android_aab
    flavor: qa
    target: lib/main_qa.dart
    dart_defines:
      ENV: qa
      API: mock
    obfuscate: true
    split_debug_info: obfsymbols
    no_shrink: true
    no_pub: true
    extra_args:
      - --build-name=1.2.3
```

`dart_defines` may be a mapping as shown above, or a list such as
`["ENV=qa", "API=mock"]`. `extra_args` is appended at the end of the Flutter
command for project-specific flags.

Defaults preserve the legacy CDT commands:

- iOS test: `flutter build ipa --obfuscate --split-debug-info=obfsymbols --no-pub`
- Android test: `flutter build appbundle --obfuscate --split-debug-info=obfsymbols --no-shrink --no-pub`
- prod build steps add `--dart-define=ENV=prod` unless overridden by
  `dart_defines`.

## Deploy Pipeline

`cdt run deploy` can mirror the legacy `cdt deploy` flow with an explicit
project `cdt.yaml`. The project `.env` remains the source for:

- `WEB_REPOSITORY`
- `WEB_BUILD_PLACE`
- `WEB_INNER`

Keep these as env references instead of copying literal path values into
`cdt.yaml`:

```yaml
version: 1

pipelines:
  deploy:
    steps:
      - git.prepare_clean_main
      - web.build:
          env: prod
      - web.copy:
          repository: ${WEB_REPOSITORY}
          destination: ${WEB_BUILD_PLACE}
          inner: ${WEB_INNER}
      - web.cache_bust:
          destination: ${WEB_BUILD_PLACE}
          inner: ${WEB_INNER}
      - git.commit_push:
          repository: ${WEB_REPOSITORY}
          message: ${flutter.version}
```

## CLI

```sh
cdt run deploy
cdt pipeline list
cdt pipeline inspect deploy
cdt pipeline inspect deploy --json
cdt pipeline validate
cdt pipeline validate deploy --json
cdt pipeline steps --json
```

JSON output is intended to be stable for automation and agents:

- `pipelines`: configured pipeline names.
- `plugins`: configured plugin module names.
- `steps`: resolved step tree for inspect.
- `registered_steps`: built-ins plus loaded plugin steps.
- `errors`: structured objects with `code`, `message`, and optional `path`.
