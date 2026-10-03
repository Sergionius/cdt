# <img src="site/assets/logo.png" alt="" width="28">&nbsp;CDT &nbsp;[![CI](https://github.com/Sergionius/cdt/actions/workflows/ci.yml/badge.svg)](https://github.com/Sergionius/cdt/actions/workflows/ci.yml)

CDT is an agent-first release automation CLI built around project-local YAML pipelines, safe preflight checks, and reusable steps for mobile, web, and custom deployments. Direct human operation remains a first-class workflow.

CDT includes built-in steps for Flutter, native iOS/Xcode, Android, web, Firebase/AppTester, TestFlight, Google Play, Python hooks, and custom steps via its SDK.

## Installation

CDT is published as the `cdt-release` Python distribution and installs the `cdt` command. `pipx` is the recommended installation method:

```bash
pipx install cdt-release
```

A specific GitHub release can also be installed directly:

```bash
pipx install "git+https://github.com/Sergionius/cdt.git@v0.6.0"
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
cdt history --pipeline test --status failed
cdt status                         # newest run globally
cdt status <run-id>
cdt status --pipeline test
cdt logs --pipeline test --tail 80
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

Every real run is recorded under `.cdt/runs/<run-id>/` with an atomic status file, manifest, exit code, and log location. Human operators can continue to use `cdt run test` directly. `cdt status` and `cdt logs` resolve the newest run automatically, while `--pipeline` selects the newest run for one pipeline. Direct and detached runs save a redacted `output.log` (direct runs tee CDT diagnostics while streaming to the terminal; detached runs record the redacted combined output), and persisted status errors redact known environment secrets and common credential forms before they are written; `cdt logs` applies the same protection again when reading older records. See [Run records](docs/runs.md) for lifecycle, redaction limitations, concurrency, retention, and recovery.

Experimental Orca sidebar status (opt-in once per user, across all CDT projects):

```bash
cdt settings enable experimental.orca-status
cdt settings show
cdt settings disable experimental.orca-status  # turn it off again
```

When enabled, a direct `cdt run <pipeline>` in an ordinary Orca terminal reports `working` and `done` through Orca's **undocumented OSC 9999 terminal protocol**. The integration is best-effort and may break with Orca updates. It does not run on dry-runs, in detached workers, inside Pi, or with redirected output; it never changes pipeline success/failure. Settings live in `~/.config/cdt/settings.json` (or `$XDG_CONFIG_HOME/cdt/settings.json`), not in `cdt.yaml`. These escape sequences bypass CDT's saved run logs.

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

CDT releases itself with the production `release` pipeline declared in this repository's `cdt.yaml`. The version is always passed explicitly; there is no automatic version selection. The pipeline verifies a clean, synced `main` and an unused version (changelog, git tags, GitHub Releases, PyPI), runs `ruff check .` and `pytest -q`, prepares `pyproject.toml`, `cdt/__init__.py`, `CHANGELOG.md`, and the GitHub tag-install examples in `README.md`/`docs/getting-started.md`, builds and twine-checks the distributions, creates the release commit and annotated tag, pushes branch and tag with one atomic `git push`, and then waits for the green GitHub Actions run, the published GitHub Release assets, and the PyPI version before reporting success.

Inspect the pipeline before releasing:

```bash
cdt pipeline list
cdt pipeline inspect release
cdt pipeline preflight release
cdt run release --input version=X.Y.Z --dry-run
```

Planning and dry-run commands never execute steps or create run records. The real release requires the exact production confirmation:

```bash
cdt run release --input version=X.Y.Z --confirm release
```

A single push is not a finished release: the pipeline finishes successfully only after `github.wait_release` confirms the workflow, the GitHub Release, and PyPI.

### Required tools

The release pipeline requires `git`, `gh` (GitHub authentication through an active `gh auth` session), `ruff`, `pytest`, `python -m build`, and `twine`; the version preflight also queries the public PyPI JSON API. `cdt pipeline preflight release` reports which tools are missing.

### Bootstrap from a fresh checkout

Before CDT is installed globally, run the same commands through the repository virtualenv:

```bash
.venv/bin/cdt pipeline validate --strict
.venv/bin/cdt run release --input version=X.Y.Z --dry-run
.venv/bin/cdt run release --input version=X.Y.Z --confirm release
```

See [CDT pipelines](docs/pipelines.md) for pipeline inputs and [Run records](docs/runs.md) for status, resume, and detached execution.

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
- `appstore.upload_testflight_ipa`
- `appstore.complete_testflight`
- `appstore.submit_review`
- `google_play.upload_aab`
- `artifact.copy_to_downloads`
- `hook.python_script`
- `notify.prod_user_agent`
- `notify.success`

Build steps use `profile` for CDT presets (`profile: prod` adds `--dart-define=ENV=prod`). Flutter `flavor` is separate. Build steps do not run `flutter pub get` or increment versions implicitly.

`appstore.upload_testflight` keeps the full upload cycle in one step and remains supported. New pipelines should prefer the resumable pair `appstore.upload_testflight_ipa` (iTMSTransporter upload only) followed by `appstore.complete_testflight` (find the uploaded build, wait for processing, set the changelog), so a failed completion can resume without re-uploading the IPA. See [Pipelines](docs/pipelines.md) for details.

## App Store review submission

`appstore.submit_review` sends the completed TestFlight build for App Store review: it creates or reuses the App Store version, binds the exact verified build, fills localized "What's new" text, sets the release mode and phased release, and submits through the ASC `reviewSubmissions` flow with durable checkpoints and safe recovery. The app comes from `IOS_BUNDLE_ID`; the build is taken from the current pipeline version context or the last recorded TestFlight completion — never an arbitrary latest Apple build. Required options: `whats_new` (locale → text mapping), `release_mode` (`manual`/`automatic` release after Apple approval), and `phased_release` (a real boolean for Apple's standard seven-day rollout).

Every pipeline containing the step requires `risk: production` and the exact CLI confirmation (`cdt run <pipeline> --confirm <pipeline>`). Success means the submission was sent for review — not Apple approval and not user availability. Existing pipelines never start submitting automatically: add the step explicitly, either after `appstore.complete_testflight` or as a standalone `submit-review` pipeline that needs no rebuild or re-upload. The app card, version localizations, and all other mandatory review data must already exist in App Store Connect. See [App Store review submission](docs/pipelines.md#app-store-review-submission) for pipeline examples, the meaning of release modes, prerequisites, and recovery rules.

## Firebase App Distribution

`firebase.upload_app_distribution` supports either the existing `FIREBASE_TOKEN` or a Google service account JSON key. For service-account uploads, grant the account the **Firebase App Distribution Admin** IAM role (`roles/firebaseappdistro.admin`) on each Firebase project it must access. One service account can be shared across multiple Firebase projects; grant it the role separately in every project.

Create and manage the service account and key in Google Cloud Console. Keep the JSON key outside the repository and do not commit it. Set `GOOGLE_APPLICATION_CREDENTIALS` to its path, either in the project's `.env` file:

```dotenv
GOOGLE_APPLICATION_CREDENTIALS=/secure/path/firebase-service-account.json
```

or in the terminal environment:

```bash
export GOOGLE_APPLICATION_CREDENTIALS=/secure/path/firebase-service-account.json
```

Terminal environment variables override values from `.env`. For Firebase uploads, a non-empty `FIREBASE_TOKEN` takes precedence over the service account. To switch an existing project to service-account authentication, remove `FIREBASE_TOKEN` from both `.env` and the terminal environment, then verify configuration with `cdt pipeline preflight <pipeline>`.

## Google Play upload (AAB)

`google_play.upload_aab` uploads one Android App Bundle and creates one release on an explicitly chosen Google Play track. It is separate from Firebase App Distribution: Firebase delivers builds to testers, while this step publishes releases through the Google Play Android Publisher API.

One-time setup happens in Google Cloud Console and Play Console — it is independent of any Firebase configuration:

1. In Play Console, link the app to a Google Cloud project and enable the **Google Play Android Developer API** for that project.
2. Grant the publishing identity Play Console permissions for the target app under **Users and permissions** (for example, release-to-testing or release-to-production rights). These are Play Console app permissions — not Firebase IAM roles and not the Firebase App Distribution Admin role.
3. The application and the target track must already exist in Play Console. CDT does not create apps or tracks and does not perform the initial app setup (store listing, Play App Signing).
4. Authenticate with Application Default Credentials: set `GOOGLE_APPLICATION_CREDENTIALS` to a service-account JSON key (relative paths resolve from the project root) or use the ambient CI identity. `FIREBASE_TOKEN` is not used by this step. Credentials never prove Play Console permissions; missing rights surface as an error from Google at publication time.

Every pipeline containing a Google Play step must declare `risk: production` and runs only with the exact confirmation (`cdt run <pipeline> --confirm <pipeline>`). See [Google Play upload](docs/pipelines.md#google-play-upload-aab) for step options and ready-made production pipelines (internal, draft, full production, staged rollout).

A successful run never means more than it says:

- `release_status: draft` creates a draft only — it was not sent for review and no user can install it.
- `release_status: inProgress` or `completed` commits the edit, which sends the changes through Google's standard review flow. Google accepting the changes is neither review approval nor user availability; CDT verifies neither.
- Managed publishing is toggled per app in Play Console, and where it applies the final **Publish** after approval is also pressed manually. CDT does not automate these actions and does not infer the mode; its final message explains both outcomes instead of claiming one.

CDT stops instead of guessing when the target track holds an unfinished release (draft, in-progress or halted rollout, staged rollout, multiple releases), when Google rejects the commit because the app is under review, or when Google reports an error that requires Play Console action.

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
