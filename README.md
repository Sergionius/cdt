# CDT

CDT is an AI-friendly release automation CLI built around project-local YAML pipelines, safe preflight checks, and reusable steps for mobile, web, and custom deployments.

## Installation

Install a specific GitHub release:

```bash
pip install "git+https://github.com/Sergionius/cdt.git@v0.2.0"
```

Or install the latest `main`:

```bash
pip install "git+https://github.com/Sergionius/cdt.git"
```

For local development:

```bash
git clone https://github.com/Sergionius/cdt.git
cd cdt
python -m pip install -e '.[dev]'
```

Note: the `cdt` name on PyPI belongs to another project, so install this CDT from GitHub.

## Commands

```bash
cdt --version
cdt run <pipeline>
cdt pipeline list
cdt pipeline inspect <pipeline> --json
cdt pipeline validate [pipeline]
cdt pipeline steps
```

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

## AI agent skill

CDT ships an Agent Skill at `skills/cdt-release/SKILL.md` for safer low-noise release runs by AI coding agents. It makes agents inspect `cdt.yaml`, avoid production pipelines unless explicitly requested, write long logs to `.cdt/agent-release-<pipeline>.log`, and return concise success/failure summaries.

For Pi, install this repository as a package or point settings at the local skill directory:

```bash
pi install git:github.com/Sergionius/cdt
# or, from a local checkout:
pi install /path/to/cdt
```

See `docs/ai-agents.md` for Codex/Pi setup details.

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
