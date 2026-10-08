# iOS build failure diagnostics

## Problem

A failed `ios.flutter_build_ipa` can leave only Flutter's generic `xcodebuild encountered an error (74)` in CDT's temporary command log. The permanent run record repeats that message but does not include Xcode's actual error. Re-running the pipeline is costly and increments the app version.

## Design

Run the original Flutter IPA command once with `-v`, so its Xcode transcript is available on the first attempt. Capture the combined stream into a private temporary file. On success, delete it. On failure, redact known secrets and save it as owner-only `.cdt/runs/<run-id>/ios-build.log`, then delete the raw file. Show up to three specific error lines from the redacted log and the log path in the failure summary. If no specific line exists, explicitly say so. Do not infer that a generic Xcode exit code is a diagnosis.

The normal run's `output.log` retains the summary, while the dedicated diagnostic log contains the full redacted verbose transcript. Capture uses bounded-memory streaming; existing redaction limits still apply. This does not guarantee an explanation when Flutter or Xcode suppresses the underlying failure or removes its private result bundle. It does not retry the build or change the pipeline's production confirmation rules.

## Verification

Cover specific-error extraction, generic-only output, secret redaction, owner-only permissions, success cleanup, and a single verbose invocation. Run the full test suite and lint checks.
