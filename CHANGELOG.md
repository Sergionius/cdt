# Changelog

## Unreleased

- Added `cdt run <pipeline> --capture-output`: an opt-in POSIX foreground supervisor that runs the pipeline in one supervised child CDT process (no shell, no PTY, `stdin=DEVNULL`, verbose transport in the child only), streams the merged stdout/stderr through incremental UTF-8 decoding and secret redaction into the run record's owner-only `output.log` and the terminal at the same time, owns the live PID and final exit code, and records a safe failure instead of a fake success when the child dies without a terminal status. Ordinary direct and detached executions keep their existing behavior; interrupted capture tears down the child process group with bounded SIGINT→TERM→KILL escalation, and unsupported platforms reject the flag before any run record or step exists.
- Added `build_timings` to run status, `cdt status`, and `agent-release status`: every executed build leaf (`risk: "build"`) is measured around its whole call — including option resolution, retries, and retry delays — with `name`, UTC `started_at`/`finished_at`, monotonic `duration_seconds`, and a `success`/`failed`/`cancelled` outcome. Skipped and completed-resume leaves get no measurements, killed processes leave unfinished entries without invented durations, and each run record keeps only its own run's measurements. This is telemetry only: build steps still invoke their build tool on every execution, and CDT still has no up-to-date check or cache.
- Documented [build performance measurement](docs/build-performance.md) and the explicit decision not to implement CDT-level caching without a measured repeated cost and a complete invalidation model covering sources, dependencies, tool versions, build parameters, and signing; old release artifacts are never reused by default.
- Closed the two remaining P2 backlog items: direct-run output capture is delivered as the opt-in `--capture-output` functionality, and build up-to-date checks are resolved by the timing measurements plus the documented no-cache decision; removed `docs/backlog/p2-direct-run-output-capture.md` and `docs/backlog/p2-build-up-to-date-checks.md`.

## v0.7.0 - 2026-10-03

- Added input-based conditional steps: an extended step record accepts `step`/`with` plus a `when` condition with exactly one operator (`equals`, `not_equals`, `present`) over a declared pipeline input. Decisions are computed once before the first step, shown as `run`/`skip`/`unknown` in plans and status files, skipped leaves create no artifacts and stay separate from `completed_steps`, and resume recomputes decisions from the same inputs. Existing string and single-key step records are unchanged, and conditions never bypass production-risk validation or the exact CLI confirmation.
- Changed parallel `values` semantics intentionally: each parallel branch now works on an isolated snapshot of `ctx.values`, sequential steps of one branch see that branch's writes, and siblings never see them. After every branch succeeds, changed keys (including deletions) are merged atomically; conflicting writes or a deletion-versus-write conflict fail the group by step ID and key name without a partial merge, and a failed group leaves the root values unchanged. Only `values` is isolated — the filesystem and external services are not transactional.
- Added an opt-in step retry policy: the extended record accepts `retry` with `max_attempts` (1–5) and `delay_seconds` (0–60), honored only for steps whose metadata declares `retry_safe: true` and only for the explicit `RetryableStepError`. Uploads, publications, webhooks, hooks, and store publishing steps gained no automatic retries, and ambiguous mutation results are never retried; unfinished steps start a fresh bounded attempt cycle on resume.
- Added capability-based step timeouts: `timeout_seconds` is accepted only when the step metadata declares a native timeout option (currently `hook.python_script`). `hook.python_script` now runs in a POSIX process group that receives TERM and then KILL on timeout or interruption, with the direct child always reaped; platforms without process groups reject the envelope timeout instead of promising a non-existent guarantee.
- Added `notify.webhook`: one HTTPS POST of an explicitly declared JSON payload to an `url_env` destination with optional `authorization_env`. Only 2xx responses are successful, redirects are never followed, there are no automatic retries, the response body is never read, and the URL, authorization, and payload are kept out of messages and saved logs; payloads containing known secrets are rejected before sending.
- Added `appstore.update_metadata`: updates `description`, `keywords`, `promotional_text`, and `whats_new` for existing localizations of an existing iOS version in `PREPARE_FOR_SUBMISSION`, matched by `IOS_BUNDLE_ID` and an explicitly provided `version`. The step only patches values that differ, verifies every PATCH by reading back, never creates versions, localizations, submissions, or build bindings, and requires pipeline `risk: production` with the exact CLI confirmation.
- Documented existing capabilities that were previously underrepresented: `firebase.ensure_cli`/`firebase.deploy`, terminal sounds and their headless-CI limits, a repeatable iOS code signing recipe for local and CI use that is separate from App Store Connect authentication, and a minimal recipe for reusing Python step packages across projects through explicit `plugins:` (no entry points, discovery, registry, or installation machinery), with a runnable `examples/reusable-plugin/` package.

## v0.6.0 - 2026-10-03

- Corrected App Store Connect phased-release endpoints and review-submission payloads against Apple's API contract; explicitly request relationship linkage, reject unknown/canceling review states, and safely recover when a submitted version is no longer editable. Added exact-target recovery and ambiguous-creation regression coverage.
- Corrected Google Play edit creation to use the Python SDK's `body` argument and raised the client minimum to `2.201.0`, whose bundled discovery supports the required `ERROR_IF_IN_REVIEW` safeguard. Added offline real-SDK request construction coverage.
- Added `appstore.submit_review`: submits the completed TestFlight build for App Store review — creates or reuses the App Store version, binds the exact verified build, fills localized `whats_new`, sets `release_mode` (`manual`/`automatic` release after Apple approval) and `phased_release` (Apple's standard seven-day rollout), and sends the ASC `reviewSubmissions` request. The app comes from `IOS_BUNDLE_ID` and the build from the current version context or the last recorded TestFlight completion under `.cdt/appstore/uploads/` — never an arbitrary latest Apple build. Documented standalone `submit-review` pipelines, the step after TestFlight completion, and the required App Store Connect preparation.
- Made App Store review submission production-safe and resumable: any `appstore.submit_review` step requires pipeline `risk: production` and the exact CLI confirmation; durable checkpoints under `.cdt/appstore/operations/` with per-app checkout locks reconcile lost responses with read-only verification, block conflicting or ambiguous operations, and confirm success only after Apple accepts the submission — never claiming approval or user availability. Existing pipelines do not start submitting automatically; documented recovery limits and unknown-outcome actions.
- Added `google_play.upload_aab`: uploads one AAB and creates one release on an explicitly chosen Google Play track with `release_status` `draft`, `inProgress` (staged rollout with `user_fraction`) or `completed`, plus localized `release_notes` and `release_name`. Authenticates with Application Default Credentials (`GOOGLE_APPLICATION_CREDENTIALS` or the ambient CI identity) and is configured in Google Cloud/Play Console, separately from Firebase.
- Made Google Play publication production-safe: any Google Play step requires pipeline `risk: production` (recursively, including inside `sequence`/`parallel`) and the exact CLI confirmation; the edit commit always uses `changesInReviewBehavior=ERROR_IF_IN_REVIEW`, never cancels an in-progress review, and final messages distinguish a saved draft, changes accepted for review, and actual approval/availability. Managed publishing and the final manual Publish remain Play Console actions that CDT neither automates nor infers.
- Added durable Google Play publication checkpoints under `.cdt/google-play/operations/` with per-package checkout locks, safe resume, conflict refusal for unfinished releases and in-progress reviews, and explicit "result unknown" errors instead of blind retries; documented ADC/API/permission setup, the internal/draft/production/staged pipeline shapes, and recovery limits.

## v0.5.5 - 2026-09-26

- Added service-account authentication support for Firebase App Distribution uploads through `GOOGLE_APPLICATION_CREDENTIALS`, while retaining `FIREBASE_TOKEN` authentication.

## v0.5.4 - 2026-09-24

- Added opt-in per-user experimental Orca terminal status for direct manual `cdt run` executions, with `cdt settings enable|disable experimental.orca-status`. Uses Orca's undocumented OSC 9999 protocol; no effect on detached/agent runs or saved logs.

## v0.5.3 - 2026-09-12

- Made CLI error-output assertions portable across Rich terminal widths and color settings so the release workflow passes consistently on Linux and macOS runners.

## v0.5.2 - 2026-09-12

- Added declarative non-secret pipeline inputs: pipelines declare `inputs` with `required` and optional regex `pattern`, values are passed as repeatable `--input KEY=VALUE` on `cdt run` and `cdt agent-release start`, interpolated as `${inputs.<name>}`, persisted in run manifests/status, and enforced on resume; pipelines without `inputs` keep their previous behavior.
- Added Python release built-in steps: `release.require_version_available` (explicit semver preflight against the changelog, git tags, GitHub Releases via `gh`, and the public PyPI JSON API), `python.ruff_check`, `python.pytest`, `python.prepare_release` (version bump, changelog transition, tag-reference updates with pre-commit rollback snapshots), `python.build_distribution` (clean build plus `twine check`), `git.require_synced_main`, `git.release_commit` (exact staging only), and the resumable atomic `git.release_tag_push`, plus `github.wait_release` for waiting on the GitHub Actions workflow, GitHub Release assets, and PyPI publication.
- Replaced `scripts/release.py` with a dogfooding production `release` pipeline in the repository root `cdt.yaml`: `cdt run release --input version=X.Y.Z --confirm release` now prepares, pushes, and confirms CDT's own releases; documentation and agent guidance describe the pipeline, required tools, bootstrap via `.venv/bin/cdt`, and the fix-branch repair loop with a three-attempt limit.

## v0.5.1 - 2026-09-12

- Preserved failure diagnostics for Android/Firebase steps: failed `android.build_aab`, `android.build_apk`, and `firebase.upload_app_distribution` runs now report the leaf step, its cause, the actual generated command with a redacted token, and the original exit code instead of collapsing to a generic failure; parallel siblings still finish and previously built artifacts are kept. This improves reporting only and does not change Firebase upload availability or behavior.

## v0.5.0 - 2026-09-11

- Added App Store Connect request resilience: a token-aware client refreshes the JWT before its 20 minute expiry and once after a 401, transient network failures and HTTP 429/5xx are retried with bounded exponential backoff and jitter honoring `Retry-After`, while permanent 4xx responses fail immediately without retries.
- Added resumable TestFlight built-in steps: `appstore.upload_testflight_ipa` runs only the iTMSTransporter upload, and `appstore.complete_testflight` re-finds the already uploaded build from the saved version context, waits for processing, and idempotently sets the changelog without re-uploading the IPA or changing the build number. The existing `appstore.upload_testflight` keeps its full-cycle behavior.
- Improved parallel group failure reports: the top-level error now names each failed child step and its original exception instead of `command: unknown; exit code: unknown`.
- Improved runtime pipeline failure reporting: a failed `cdt run` ends with a readable English summary naming the failed step, its cause, the actual generated command and exit code when available, parallel siblings that were allowed to finish, and artifacts produced — printed without Typer usage help or `Invalid value` wrappers.
- Preserved Flutter build failure details: a failed `ios.flutter_build_ipa` now reports the Flutter command's exit code and points to the streamed Flutter/Xcode output instead of failing with an empty error message.
- Added redacted `output.log` capture for direct `cdt run` executions, including ASC retry diagnostics and the terminal error summary; detached runs continue to record redacted combined output.

## v0.4.1 - 2026-07-28

- Added defense-in-depth secret redaction for detached logs, persisted status errors, worker startup diagnostics, status payloads, and `cdt logs`, including known environment credentials, explicitly configured keys, Bearer credentials, authorization headers, password/token assignments, and JWT-looking values.
- Added recent-run resolution to `cdt status` and `cdt logs`, with `--pipeline` selectors and defense-in-depth redaction when reading older logs.
- Added composable `cdt history --pipeline` and `--status` filters that apply before `--limit` and report active filters in JSON output.
- Hardened detached worker startup failures to write terminal redacted status and atomic exit metadata, and made run navigation tolerate corrupt or invalid record directories.

## v0.4.0 - 2026-07-21

- Renamed the Python distribution to `cdt-release` while preserving the `cdt` command, and added PyPI trusted publishing to the release workflow. Existing pipx users must migrate once with `pipx uninstall cdt && pipx install cdt-release`; project `cdt.yaml` files do not require migration.
- Added isolated `.cdt/runs/<run-id>/` records with atomic manifests, statuses, exit codes, logs, latest-pipeline pointers, and concurrent-run safety.
- Added `cdt history`, `cdt status`, and `cdt logs` while preserving direct `cdt run <pipeline>` as the primary human workflow.
- Added run-ID based detached commands: `cdt agent-release status --run <run-id>` and `stop --run <run-id>`.
- Added explicit pipeline `risk: production` and exact `--confirm <pipeline>` enforcement for interactive and detached execution.
- Added Flutter/mobile project detection and reviewable test pipeline generation through `cdt init`.
- Added a bundled `cdt.yaml` JSON Schema and `cdt schema` command for editor integration.
- Updated Agent Skill guidance to use compact run status rather than parsing healthy logs.
- Added security and contribution policies, refreshed installation and pipeline documentation, and documented separate human and agent workflows.
- Consolidated duplicate CI workflows, added Python 3.13 coverage, expanded wheel smoke checks, and raised total test coverage from 81% to 84%.

## v0.3.6 - 2026-07-18

- Added sequential branches inside parallel pipeline groups, enabling prod flows such as iOS alongside Android AAB followed immediately by APK.
- Added `notify.prod_user_agent` as a registered YAML built-in and included it in the example prod pipeline before `notify.success`.
- Added nested step IDs and child-step resume support for sequential parallel branches.
- Fixed `cdt run --skip-completed` incorrectly treating duplicate step names, including anonymous parallel groups, as the same step.
- Changed pipeline status step fields to store stable step ids instead of step names. Older name-based resume files are rejected because duplicate step names are ambiguous; recreate them or map completed work to ids from `cdt pipeline inspect <pipeline>` / `cdt pipeline plan <pipeline>`.
- Changed resume input handling: `--resume-status-file` is required for `--resume-from` and `--skip-completed`; `--status-file` only writes the current run status.

## v0.3.5 - 2026-07-07

- Added `cdt run --resume-from` and `--skip-completed` to resume long pipeline runs from status JSON.
- Added child-level parallel runtime status fields: `running_steps`, `parallel_completed`, and `parallel_failed`.
- Added `cdt pipeline validate --strict` to fail on planner warnings during safer CI preflight.
- Added metadata-driven `requires_env` and `cdt pipeline preflight <pipeline>` for selected-pipeline tool/env checks.

## v0.3.4 - 2026-07-07

- Added `cdt agent-release start/status/stop` for token-efficient long-running release automation.
- Added `cdt run --status-file` for machine-readable pipeline status.
- Hardened CI/CD checks with clean `dist` builds, quiet pytest, and tag smoke retry regressions.

## v0.3.3 - 2026-07-07

- Automated release pipeline via GitHub Actions.
- Hardened the release helper with explicit push mode, safer changelog formatting, clean builds, and pre-push rebase.
- Added PR build, twine, and wheel smoke checks.
- Added `cdt self-update --check`, `--json`, explicit `--manager`, rate-limit errors, and `uv` support.
- Added `cdt doctor` and a getting-started guide.
- Improved pipeline YAML, unknown-step, and failed-step error messages.

## v0.3.2 - 2026-07-07

- Fixed README install examples to point to the latest release tag.
- Hardened repository URL parsing in `cdt self-update`.
- Clarified `cdt self-update` installation-method limitations.
- Added coverage execution to the documented local command set and CI.

## v0.3.1 - 2026-07-07

- Added `cdt self-update` command to update the CLI to the latest GitHub release via `pipx`.
- Added `cdt self-update --dry-run` to preview the available release tag and update command without executing it.
- Moved planning documents from `plans/` to `docs/plans/`.

## v0.3.0 - 2026-07-06

- Added step metadata for built-in and plugin pipeline steps.
- Added `cdt pipeline plan <pipeline>` with JSON output and static risk classification.
- Added `cdt run <pipeline> --dry-run` as a non-executing planning preflight.
- Extended `cdt.sdk.step` decorator to accept `StepMetadata` and keyword metadata arguments.
- Refined artifact/result metadata contract: `StepMetadata` now uses structured
  `ResultRequirement` and `ResultProduction` objects instead of flat
  `requires_artifacts` / `produces` strings.
- `cdt pipeline plan --json` now exposes grouped `artifact_flow.requires` entries with
  `types`, `mode` (`all` or `any`), and `names` inferred from static YAML options.
- Breaking: SDK/plugin authors must migrate from `requires_artifacts` and flat string
  `produces` metadata to `ResultRequirement` / `ResultProduction`; CI/tools parsing
  `cdt pipeline plan --json` metadata must handle the structured metadata shape.

## v0.2.1 - 2026-07-06

- Improved the `cdt-release` Agent Skill with explicit production confirmation, structured summaries, and observability guidance.
- Added repository-level `AGENTS.md` instructions for AI agents.
- Added `.agents/rules/cdt-release.md` with hard release safety rules.
- Updated AI agent documentation and package manifest entries for agent skills/rules.
- Replaced Hermes-specific setup text with generic Agent Skills guidance.

## v0.2.0 - 2026-07-06

- Added the `cdt-release` Agent Skill for safer AI-assisted CDT test releases.
- Removed `cdt migrate legacy` after all known projects were migrated.
- Switched release automation to YAML-only `cdt.yaml` pipelines.
- Removed legacy direct commands in favor of `cdt run <pipeline>`.
- Added `cdt migrate legacy` with dry-run, merge, backup, and force behavior.
- Added/updated built-ins: Flutter build-number increment, Android APK/AAB, iOS IPA, TestFlight upload, artifact copy, success notify, and Python hook steps.
- Build steps now use `profile` instead of `env`, do not run `flutter pub get`, and do not increment versions implicitly.
- Added artifact duplicate/missing checks, parallel error aggregation, JSON `schema_version`, and unknown-step suggestions.

## v0.1.0 - 2026-07-03

- Initial public release of CDT.
- Includes Flutter release flows, App Store/TestFlight upload helpers, Firebase upload/deploy helpers, notifications, and trusted project-local YAML pipelines.
- Adds plugin steps through `cdt.yaml` and the `cdt.sdk.step` decorator.
