# Changelog

## v0.2.0 - 2026-07-04

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
