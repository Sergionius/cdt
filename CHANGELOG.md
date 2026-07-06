# Changelog

## Unreleased

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
