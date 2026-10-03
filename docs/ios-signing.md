# iOS code signing recipe

CDT does not sign iOS builds and ships no signing manager. Signing is delegated
to the toolchain you already use: `flutter build ipa` and
`xcodebuild archive`/`-exportArchive` read the signing identity, the
provisioning profile and the project/team settings from the machine and from
your Xcode project. This recipe describes a repeatable local and CI setup on
those existing interfaces — no Ruby, no Fastlane `match`, no extra CDT flags.

## What signing needs

An App Store distribution build needs exactly two secrets-adjacent assets,
both created in the Apple Developer portal:

- **A distribution certificate with its private key** (`.p12`). The private key
  must be imported into a keychain on the build machine; a certificate without
  the key cannot sign.
- **A provisioning profile** that matches the app's bundle ID, the team of the
  certificate, and the distribution method (`app-store`, `ad-hoc`,
  `development`).

## Local Xcode/Flutter (automatic signing)

For day-to-day work let Xcode manage both assets in your login keychain:

1. Open the iOS project in Xcode, select the target → **Signing & Capabilities**,
   enable **Automatically manage signing** and pick your team. The bundle ID
   (`PRODUCT_BUNDLE_IDENTIFIER`) must match a profile your team can create.
2. `flutter build ipa` (used by `ios.flutter_build_ipa`) and
   `xcodebuild archive` (used by `ios.xcode_build_ipa`) then sign with the
   project settings. Keep the Mac signed into the same Apple ID in Xcode.

If you need explicit export behavior, hand Flutter an export options plist via
the existing step option (no new CDT flag is introduced):

```yaml
- ios.flutter_build_ipa:
    profile: prod
    artifact: ios_ipa
    extra_args:
      - "--export-options-plist=ios/ExportOptions.plist"
```

## Configuring CDT's iOS Xcode steps

The `ios.xcode_build_ipa` flow reads the ordinary project `.env` keys — the
same variables it always used; CDT adds no runtime flags:

| Variable | Meaning |
|---|---|
| `IOS_WORKSPACE` / `IOS_PROJECT` | Workspace or project path for `xcodebuild` (default workspace `ios/Runner.xcworkspace`). |
| `IOS_CONFIGURATION` | Build configuration, default `Release`. |
| `IOS_EXPORT_METHOD` | Export method for the generated `ExportOptions.plist`, default `app-store`. |
| `IOS_SIGNING_STYLE` | `automatic` (default) or `manual`. |
| `IOS_TEAM_ID` | Team ID written to `teamID` in the generated export options. |
| `IOS_EXPORT_OPTIONS_PLIST` | Path to your own `ExportOptions.plist`; when present it is used as-is instead of the generated one. |

With `IOS_SIGNING_STYLE: manual`, provide a complete `ExportOptions.plist`
(method, `signingStyle: manual`, `teamID`, and the `provisioningProfiles`
mapping from profile name to bundle ID) and point `IOS_EXPORT_OPTIONS_PLIST`
at it.

## CI: temporary keychain recipe

On a headless mac runner the login keychain is not available. Create a
short-lived keychain, import the certificate, register the profiles, and always
clean up. Keep the `.p12`, its password and the profiles in your CI secret
store (secret files, not environment values in `cdt.yaml`) — never in the
repository.

```bash
#!/bin/bash
set -euo pipefail

KEYCHAIN=cdt-build.keychain
# Secret files provisioned by the CI system; never committed.
P12=signing.p12          # distribution certificate + private key
P12_PASSWORD_FILE=p12-password.txt
PROFILE=path/to/AppStore.mobileprovision

security create-keychain -p "$(cat "$P12_PASSWORD_FILE")" "$KEYCHAIN"
trap 'security delete-keychain "$KEYCHAIN"' EXIT   # cleanup even on failure
security set-keychain-settings -lut 21600 "$KEYCHAIN"
security unlock-keychain -p "$(cat "$P12_PASSWORD_FILE")" "$KEYCHAIN"
security list-keychain -d user -s "$KEYCHAIN" $(security list-keychain -d user | tr -d '"')

# Import the certificate with its private key and allow codesign to use it
# without an interactive ACL prompt.
security import "$P12" -k "$KEYCHAIN" \
  -P "$(cat "$P12_PASSWORD_FILE")" \
  -T /usr/bin/codesign -T /usr/bin/security
security set-key-partition-list -S apple-tool:,apple: -s \
  -k "$(cat "$P12_PASSWORD_FILE")" "$KEYCHAIN"

# Install the provisioning profile under its UUID.
UUID=$(grep -aA1 '<key>UUID</key>' "$PROFILE" | grep -o '[A-F0-9-]\{36\}')
mkdir -p "$HOME/Library/MobileDevice/Provisioning Profiles"
cp "$PROFILE" "$HOME/Library/MobileDevice/Provisioning Profiles/$UUID.mobileprovision"

# Verify the identity is visible, then run the CDT pipeline.
security find-identity -v -p codesigning "$KEYCHAIN"
cdt run prod --confirm prod
```

Cleanup removes the keychain (the `trap` above runs on success and failure) and
the imported profile:

```bash
security delete-keychain cdt-build.keychain
rm -f "$HOME/Library/MobileDevice/Provisioning Profiles/$UUID.mobileprovision"
```

Run the profile/keychain setup in a pre-step of the CI job (for example a
`hook.python_script` step or a job-level bootstrap script); CDT itself needs no
changes. Self-hosted persistent runners should rotate the imported assets
regularly; ephemeral CI machines discard everything with the job.

## Bundle and team matching

A signing failure is almost always a mismatch. Before a release, check that:

- the profile's bundle ID matches `PRODUCT_BUNDLE_IDENTIFIER` in the Xcode
  target (for Flutter, in `ios/Runner.xcodeproj`);
- the certificate's team matches the profile's team and, for manual export,
  the `teamID` in `ExportOptions.plist` (`IOS_TEAM_ID`);
- the export method matches the profile type (`app-store` profile for
  `method: app-store`, and so on);
- the profile is not expired and the device list is irrelevant for App Store
  distribution but required for `development`/`ad-hoc`.

`security find-identity -v -p codesigning` lists usable identities; `xcodebuild
-allowProvisioningUpdates` is not used by CDT and interactive prompts never
appear in CI, so everything must be installed before `cdt run` starts.

## Code signing is not App Store Connect authentication

These are two independent credential systems and must be kept separate:

- **Code signing** (this document): distribution certificate + private key +
  provisioning profiles, used by `codesign`/`xcodebuild`/`flutter` to sign the
  binary. Needed for `ios.flutter_build_ipa` and `ios.xcode_build_ipa`.
- **App Store Connect API authentication**: `ASC_KEY_ID`, `ASC_ISSUER_ID`,
  `ASC_PRIVATE_KEY_PATH` (plus `IOS_BUNDLE_ID`), used by the TestFlight upload,
  review submission and metadata steps to talk to the ASC API. No certificate
  or profile is involved, and no signing identity is needed for
  `appstore.complete_testflight`, `appstore.submit_review` or
  `appstore.update_metadata`.

Handle both the same way:

- Keep secrets out of `cdt.yaml` and out of the repository. `cdt.yaml` is not a
  secret store and pipeline `inputs` are non-secret by contract; use the
  project `.env` (git-ignored) or the CI secret store.
- Never print credentials into logs. CDT never echoes environment values, and
  saved run logs are redacted — do not defeat that by `echo "$ASC_KEY_ID"` or
  by passing secrets as step options, command arguments or inputs.

## What this recipe intentionally does not do

CDT has no signing manager: it does not create certificates, does not download
or renew profiles, does not sync secrets between machines, and adds no runtime
flags, Ruby dependency, or keychain service beyond what `xcodebuild` and
`security` already provide. When the demand for automated profile rotation is
confirmed, that remains future work outside the current scope.
