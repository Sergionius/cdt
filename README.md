# <img src="site/assets/logo.png" alt="" width="28">&nbsp;CDT &nbsp;[![CI](https://github.com/Sergionius/cdt/actions/workflows/ci.yml/badge.svg)](https://github.com/Sergionius/cdt/actions/workflows/ci.yml)

CDT is an AI-friendly release automation CLI built around project-local YAML pipelines, safe preflight checks, and reusable steps for mobile, web, and custom deployments.

CDT includes built-in steps for Flutter, native iOS/Xcode, Android, web, Firebase/AppTester, TestFlight, Python hooks, and custom steps via its SDK.

## Installation

CDT is a CLI tool, so `pipx` is the recommended installation method.

Install a specific GitHub release:

```bash
pipx install "git+https://github.com/Sergionius/cdt.git@v0.3.0"
```

Or install the latest `main`:

```bash
pipx install "git+https://github.com/Sergionius/cdt.git"
```

Upgrade or reinstall:

```bash
pipx uninstall cdt
pipx install "git+https://github.com/Sergionius/cdt.git@v0.3.0"
```

For local development:

```bash
git clone https://github.com/Sergionius/cdt.git
cd cdt
python -m pip install -e '.[dev]'
# or reinstall the local checkout as a pipx CLI:
scripts/reinstall.sh
```

`pip install git+https://github.com/Sergionius/cdt.git@v0.3.0` also works, but `pipx` keeps the CLI isolated from project Python environments.

Note: the `cdt` name on PyPI belongs to another project, so install this CDT from GitHub.

## Commands

```bash
cdt --version
cdt run <pipeline>
cdt run <pipeline> --dry-run
cdt pipeline list
cdt pipeline inspect <pipeline> --json
cdt pipeline plan <pipeline> --json
cdt pipeline validate [pipeline]
cdt pipeline steps
cdt self-update
cdt self-update --dry-run
```

Version 0.3.0 adds static planning metadata: `cdt pipeline plan <pipeline>` and `cdt run <pipeline> --dry-run` show the step tree, risk, warnings, and artifact flow without executing steps.

`cdt self-update` updates the installed CLI to the latest GitHub release (requires `pipx`). Use `cdt self-update --dry-run` to see the available release tag and the update command without running it.

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

## Release notes

See [`CHANGELOG.md`](CHANGELOG.md) for release notes.

## AI agent skill

CDT ships an Agent Skill at `skills/cdt-release/SKILL.md` for safer low-noise release runs by AI coding agents. It makes agents inspect `cdt.yaml`, avoid production pipelines unless explicitly requested and confirmed, write long logs to `.cdt/agent-release-<pipeline>.log`, and return concise structured success/failure summaries.

Repository-level agent guidance lives in `AGENTS.md`; hard release safety rules live in `.agents/rules/cdt-release.md`.

For Pi, install this repository as a package or point settings at the local skill directory:

```bash
pi install git:github.com/Sergionius/cdt
# or, from a local checkout:
pi install /path/to/cdt
```

Other Agent Skills compatible clients can use `skills/cdt-release/SKILL.md` by copying or linking the skill directory according to their own installation mechanism. Claude Code and similar agents should start from `AGENTS.md`.

See `docs/ai-agents.md` for Pi, Codex, Claude Code, and generic Agent Skills setup details.

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
