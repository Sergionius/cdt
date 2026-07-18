# Changelog

## Unreleased

- Nothing yet.

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
