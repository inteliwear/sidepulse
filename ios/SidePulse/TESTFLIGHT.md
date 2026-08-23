# Shipping SidePulse for iOS to TestFlight

`.github/workflows/testflight-ios.yml` builds, signs, uploads, and assigns the
iOS app to an internal TestFlight group. It is `workflow_dispatch` only — no
push builds.

Run it with:

```bash
gh workflow run testflight-ios.yml --ref mac8005/remote-host-monitoring
```

## Fork-specific bundle identifier

Upstream ships `io.sidepulse.app`, which lives in the upstream maintainer's
Apple Developer account. Apple bundle identifiers are globally unique, so this
fork builds as **`com.massimo.sidepulse`** instead. The identifier is overridden
on the `xcodebuild` command line, so `project.pbxproj` still matches upstream
and future merges stay conflict-free.

Consequence for the push tooling in `tools/`: set

```bash
export APNS_BUNDLE_ID="com.massimo.sidepulse"
```

instead of the value in `sidepulse.env.example`, because the APNs topic must
equal the bundle identifier.

## Required repository secrets

| Secret | What it is |
| --- | --- |
| `APPLE_TEAM_ID` | 10-character Apple Developer Team ID |
| `APPSTORE_API_KEY_ID` | App Store Connect API key ID |
| `APPSTORE_API_KEY_ISSUER_ID` | App Store Connect API issuer UUID |
| `APPSTORE_API_KEY_BASE64` | base64 of the `AuthKey_<KEY_ID>.p8` file |

The API key needs the **App Manager** role: the workflow creates the bundle ID,
the App Store Connect app record, a distribution certificate, and a
provisioning profile on first run.

## What the workflow does

1. Registers `com.massimo.sidepulse` and creates the App Store Connect app
   record named `SidePulse` (idempotent — later runs are no-ops).
2. Generates a fresh CSR, requests a distribution certificate, and imports it
   into a throwaway keychain. Apple allows one active distribution certificate
   per team, so an existing one is revoked; the private key never leaves the
   runner.
3. Creates the `SidePulse App Store` provisioning profile bound to that
   certificate.
4. Flips `aps-environment` to `production` in the entitlements — the checked-in
   value targets local development, and codesign rejects it against an App
   Store profile.
5. Archives with `MARKETING_VERSION=1.0.<run_number>` and
   `CURRENT_PROJECT_VERSION=<run_number>`, exports the IPA, uploads via
   `altool`.
6. Waits for App Store Connect processing, then attaches the build to the
   `Personal Testing` internal group and adds the account holder as a tester.

`ITSAppUsesNonExemptEncryption=false` is set in `Info.plist` so builds do not
stall on the export-compliance question.

## Certificate note

Step 2 revokes any existing `DISTRIBUTION` certificate on the team. That is
safe for CI-only signing but will invalidate a distribution certificate you
rely on locally in Xcode; regenerate it from Xcode's Settings → Accounts if you
need one.
