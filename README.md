# <img src="site/assets/logo.png" alt="" width="28">&nbsp;CDT &nbsp;[![CI](https://github.com/Sergionius/cdt/actions/workflows/ci.yml/badge.svg)](https://github.com/Sergionius/cdt/actions/workflows/ci.yml)

CDT is an agent-first release automation CLI built around project-local YAML pipelines, safe preflight checks, and reusable steps for mobile, web, and custom deployments. Direct human operation remains a first-class workflow.

CDT includes built-in steps for Flutter, native iOS/Xcode, Android, web, Firebase/AppTester, TestFlight, Python hooks, and custom steps via its SDK.

## Installation

CDT is published as the `cdt-release` Python distribution and installs the `cdt` command. `pipx` is the recommended installation method:

```bash
pipx install cdt-release
```

A specific GitHub release can also be installed directly:

```bash
pipx install "git+https://github.com/Sergionius/cdt.git@v0.4.0"
```

Upgrade or reinstall:

```bash
cdt self-update --check
cdt self-update --manager pipx
cdt self-update --dry-run  # preview the release tag and command without running it
```

Or manually:

```bash
pipx uninstall cdt-release
pipx install cdt-release
```

For local development:

```bash
git clone https://github.com/Sergionius/cdt.git
cd cdt
python -m pip install -e '.[dev]'
# or reinstall the local checkout as a pipx CLI:
scripts/reinstall.sh
```

`pip install cdt-release` also works, but `pipx` keeps the CLI isolated from project Python environments.

The distribution is named `cdt-release` because the `cdt` project name on PyPI belongs to another project. The installed command remains `cdt`.

### Upgrading from CDT 0.3.x

CDT 0.3.x installed from GitHub used the distribution name `cdt`. Migrate the pipx environment once:

```bash
pipx uninstall cdt
pipx install cdt-release
```

Project configuration does not require migration: `cdt.yaml` version 1 remains supported, and pipeline `risk` defaults to `standard` when omitted. Existing pipeline-named agent-release status files remain readable for compatibility; new runs use `.cdt/runs/<run-id>/`.

## Commands

```bash
cdt --version
cdt init
cdt run <pipeline>
cdt run <pipeline> --dry-run
cdt history
cdt status <run-id>
cdt logs <run-id>
cdt pipeline list
cdt pipeline inspect <pipeline> --json
cdt pipeline plan <pipeline> --json
cdt pipeline validate [pipeline]
cdt pipeline steps
cdt schema --output cdt.schema.json
cdt doctor
cdt self-update --check
cdt self-update --manager pipx
cdt self-update --json --check
```

Static planning commands (`cdt pipeline plan <pipeline>` and `cdt run <pipeline> --dry-run`) show the step tree, risk, warnings, and artifact flow without executing steps.

Every real run is recorded under `.cdt/runs/<run-id>/` with an atomic status file, manifest, exit code, and log location. Human operators can continue to use `cdt run test` directly; run IDs are only needed for later inspection with `cdt history`, `cdt status`, or `cdt logs`. See [Run records](docs/runs.md) for lifecycle, concurrency, retention, and recovery.

Resume status migration note: current CDT status files store stable step ids (`0`, `1`, `1/0`, `1/0/1`) instead of step names. Older name-based status files are rejected because duplicate names such as anonymous `parallel` groups are ambiguous. Recreate the status file by rerunning without `--skip-completed`, or use `cdt pipeline inspect <pipeline>` / `cdt pipeline plan <pipeline>` to map completed work to step ids manually.

`cdt self-update` updates the installed CLI to the latest GitHub release. It supports `--manager pipx`, `--manager pip`, and `--manager uv`; editable/local installs should be updated manually. Use `cdt self-update --check` to check without changing files, `--json` for machine-readable output, and `--dry-run` to see the release tag and update command without running it. The command requires outbound HTTPS access to `api.github.com`.

For a quick first run, use `cdt init` in a Flutter project and see [Getting started in 5 minutes](docs/getting-started.md). `cdt init` creates a reviewable test pipeline; it never adds uploads, credentials, or production steps automatically.

## Minimal `cdt.yaml`

```yaml
version: 1

pipelines:
  prod:
    risk: production
    steps:
      - flutter.increment_build_number
      - flutter.pub_get
      - parallel:
          steps:
            - sequence:
                steps:
                  - ios.flutter_build_ipa:
                      profile: prod
                      flavor: prod
                      artifact: ios_ipa
                  - appstore.upload_testflight:
                      artifact: ios_ipa
                      changelog: prod build
            - sequence:
                steps:
                  - android.build_aab:
                      profile: prod
                      flavor: prod
                      artifact: android_aab
                  - android.build_apk:
                      profile: prod
                      flavor: prod
                      artifact: android_apk
      - notify.prod_user_agent
      - notify.success
```

See `examples/cdt.yaml` and `docs/pipelines.md` for a fuller prod pipeline, plugins, artifacts, and hooks.

## Release notes

See [`CHANGELOG.md`](CHANGELOG.md) for release notes.

## Releasing

After updating versions and the changelog, push a `v*` tag (for example `v0.4.0`). GitHub Actions runs lint, tests, build, and `twine check`, publishes `cdt-release` to PyPI through trusted publishing, creates a GitHub Release, and attaches the wheel and source archive from `dist/`. Use `python scripts/release.py <version>` to prepare the release commit and annotated tag locally. Use `python scripts/release.py <version> --push` only after explicit confirmation to rebase, push the commit, and push the tag.

## Agent-friendly automation

CDT is agent-first, not agent-only. Humans keep the direct `cdt run <pipeline>` workflow, while automation clients can use JSON planning and detached execution without parsing full build logs.

CDT ships an Agent Skill at `skills/cdt-release/SKILL.md`. It makes agents inspect `cdt.yaml`, avoid production pipelines without exact confirmation, use isolated run IDs, wait on compact status JSON, and return concise structured summaries.

Repository-level guidance lives in `AGENTS.md`; hard release safety rules live in `.agents/rules/cdt-release.md`. Agent Skills compatible clients can copy or link `skills/cdt-release/` according to their installation mechanism. See `docs/ai-agents.md` for setup and the stable automation contract.

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
- `notify.prod_user_agent`
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

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md) for development checks and pull request guidance. Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md). CDT is available under the [MIT License](LICENSE).
