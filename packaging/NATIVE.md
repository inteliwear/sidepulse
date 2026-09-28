# Rust native preview packages

The native installer commands are Rust. They call platform signing and package
utilities as separate, bounded processes. The production Python packaging path
remains available while migration validation is unfinished.

## Build and inspect

```sh
cargo build --workspace --release --bins
target/release/sidepulse-next-stage --package target/release build/native 0.1.0-preview
target/release/sidepulse-next-stage --verify-package build/native
target/release/sidepulse-next-stage --archive-package build/native dist/sidepulse-native.zip
```

Create the parent output directories first. Package and archive destinations must
be new. The package contains binaries, compatibility names, three Mac applications,
checksums, and instructions. It excludes runtime settings, logs, hooks, startup
registration, and absolute installation paths. ZIP archives retain executable
permissions. CI builds and smoke-checks these archives on all three platforms.

Extract the full ZIP, then stage it at a chosen permanent path:

```sh
./bin/sidepulse setup --source-dir . --stage-dir /chosen/preview
./bin/sidepulse service install --stage-dir /chosen/preview --dry-run
```

On Windows use `.\bin\sidepulse.exe` and absolute Windows paths. Registration is
an explicit operation. Review the emitted plan, then run the same command without
`--dry-run`. Status bar startup uses `status-bar install`. Updates require the
selected service to be stopped; run replacement from outside that preview.

## Import an existing Python setup

Import is explicit and targets a new preview. Review the list before creating it:

```sh
./bin/sidepulse setup --source-dir . --stage-dir /chosen/preview \
  --import-config /captured/sidepulse-config --import-logs /captured/agent-logs --dry-run
```

Remove `--dry-run` to create the preview. The config directory supplies raw
`settings.json`, `links.json`, and `animations/*.LED`; the logs directory supplies
`codex.jsonl`, `claude.jsonl`, `grok.jsonl`, `cursor.jsonl`, and `junie.jsonl` when
present. Unknown JSON fields and saved profiles are retained. Source files are
checked again before publication and remain unchanged. Invalid JSON, symlinks,
unexpected animation entries, and oversized files are rejected.

The preview keeps its disabled default relay configuration. Hook installation,
startup registration, physical output, and power management require separate
explicit operations. Updating or rolling back a preview retains the latest raw
settings, links, animation assets, relay configuration, and runtime state.

## Mac signing and installer

```sh
target/release/sidepulse-next-stage --sign-package build/native \
  'Developer ID Application: Publisher (TEAMID)' --dry-run
```

Remove `--dry-run` when the publisher identity is available. Signing enables
hardened runtime and timestamps, signs the standalone executables and immutable
app bundles, verifies every signature, and recomputes the package checksums.
Signing failure leaves the incomplete package for inspection; it is not published.

Create a root-owned installer payload:

```sh
target/release/sidepulse-next-stage --macos-pkg build/native dist/SidePulse-native.pkg \
  --installer-identity 'Developer ID Installer: Publisher (TEAMID)' --dry-run
```

Remove `--dry-run` to build. Omitting the installer identity creates an unsigned
local QA installer. The PKG installs immutable source files into
`/Library/Application Support/SidePulse/NativePreview`, with recommended system
ownership. It has no postinstall script and does not activate hooks, helpers, or
startup jobs. User settings and runtime logs belong to a separately staged preview.

A distributor must submit the signed PKG to Apple's notary service, confirm
acceptance, and staple and validate the ticket:

```sh
xcrun notarytool submit dist/SidePulse-native.pkg --keychain-profile PROFILE --wait
xcrun stapler staple dist/SidePulse-native.pkg
xcrun stapler validate dist/SidePulse-native.pkg
pkgutil --check-signature dist/SidePulse-native.pkg
```

No signing credentials are stored in the repository. Actual signing, notarization,
installation, and Gatekeeper validation remain release gates. A checksum manifest
checks file integrity; it does not establish the publisher's identity.

## System SD eject guard

The root-owned native PKG is the source of the privileged helper. Inspect:

```sh
sidepulse sdejectguard install --scope system --no-start --dry-run
```

Explicit application of system mutations requires root. The installer verifies
that the payload and every parent are root-owned and not group/other writable,
rejects symlinks, and checks package hashes before installing or starting. The
job uses a distinct `io.sidepulse.next.sd-guard.system` LaunchDaemon and root-owned
log locations. Existing different jobs are preserved. Stopping or removing an
owned entry remains possible if its payload is missing. User-scoped SD guard
startup remains available through `--stage-dir`. This migration has not enabled
a real eject guard or installed a system job on the development machine.

## Windows and Linux signatures

Windows uses a certificate already available to SignTool:

```powershell
.\target\release\sidepulse-next-stage.exe --sign-package build/native CERTIFICATE_THUMBPRINT --timestamp-url https://TIMESTAMP_SERVICE --dry-run
```

The plan uses SHA-256 file and timestamp digests and Authenticode verification.
Run without `--dry-run`, then build the archive from the resealed package.

For Linux, create a detached archive signature with the publisher's GPG key:

```sh
gpg --armor --detach-sign --local-user KEY_ID dist/sidepulse-native.zip
gpg --verify dist/sidepulse-native.zip.asc dist/sidepulse-native.zip
```

Sources: [Apple distribution signing](https://developer.apple.com/documentation/xcode/creating-distribution-signed-code-for-the-mac/),
[Apple notarization](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution),
[Microsoft SignTool](https://learn.microsoft.com/en-us/windows/win32/seccrypto/signtool).
