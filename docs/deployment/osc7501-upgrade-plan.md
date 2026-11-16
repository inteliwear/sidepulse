# OSC session status: deployment and upgrade plan

Status: **protocol-compatible candidates; production rollout has prerequisites**.
This is a proposed rollout, not a deployment record. This review made no production
changes and sent no production push messages.

## Reviewed versions

| Component | Reviewed source | Release status |
| --- | --- | --- |
| Desktop | `inteliwear/sidepulse`, `1b937cff788388b2b5ed890d6ed2166ed58a3868`, PR #72 | Pushed; merge into `main` before normal installer updates |
| iOS | `inteliwear/sidepulse-ios`, `00f9de159456c3a598a5010734907724e20c9300` | Clean checkout; signed distribution build still needs verification |
| Bridge | `inteliwear/sidepulse-bridge`, `0144bff605515132251752da608cbd53c7c9f7c0` **plus eight uncommitted files** | Essential compatibility/security changes are not yet an immutable release |

Reviewed bridge patch SHA-256: `41a6c9bf0a0252799f0c90a6ff53badddd5866ad245a67840d7eabcaa37adb8d`
(`git diff --binary HEAD`). Freeze a real commit before building a release.
Deploying bridge commit `0144bff` alone does not include the reviewed sender-key
recovery isolation or 64 KiB desktop-message allowance.
The bridge's origin/main is `e8e363df51388a000c03c8e693a531235f7d63d8`;
its local OSC branch is not published on origin. Commit and push that complete
candidate for review before assigning a deployment artifact to it. The reviewed
iOS revision is already on its origin/main.

A read-only production health request returned `200 OK`, body `OK`, and
`x-sidepulse-release: 23f25cffbb7d-e3e58f1f`. Its HTTP Date was
`Mon, 09 Nov 2026 08:01:30 GMT`. This matches the prefix of the legacy artifact
recorded in the bridge's October 1 deployment notes. It is not proof of the
complete binary hash, runtime configuration, or working APNs credentials.
Obtain that inventory before production work.

## Compatibility contract

- Wire version is **`v: 1`, `type: program_status`**. Phone pairing capability
  **`status_protocol: 2`** is a separate number. Do not change the JSON version to 2.
- One current state per authorized sender, publisher, and scope. States are
  `working`, `blocked`, `done`, `error`, `idle`, and `clear`; IDs are metadata.
  iOS accounts sessions independently and chooses its own LED patterns.
- Desktop POSTs phone updates as `{"data": <update>}`. The bridge binds the
  URL's sender key to outer `shared_key` metadata and adds `sidepulse_push_id`.
  iOS authorizes that outer key; nested fields cannot authorize a message.
- Phone requests and retained recovery bodies are limited to **2,048 bytes**.
  Desktop clips outbound `msg` to **1,024 UTF-8 bytes**, then reduces it further
  if necessary to fit the envelope. Identity and sequencing are not clipped.
- iOS receives object or JSON-string `data`, preserves multiple sources, rejects
  snapshots/batches/session-control messages, and keeps at most 64 active
  sessions, preferring newer accepted updates. Its 128-entry recent-session
  window keeps sequence guards; stale updates cannot revive a clear while its
  guard is retained. This is bounded deduplication, not indefinite replay history.
- Bridge recovery shares **ten latest items per device**, across senders. Session
  values coalesce by sender/publisher/scope; manual writes keep their latest
  command alongside session values. TTL defaults to and cannot exceed **300 seconds**.
- Recovery is destructive **per paired sender key**. Current iOS uses
  `/api/leds/apns_<token>_<key>/queued`, once for each authorized key. Current
  Android recovery also supplies its key; Android app builds/hardware were not
  retested in this audit.
- The bridge checks key syntax and filters recovery by that key; the phone's
  registry decides authorization. The ten-item queue remains shared, with the
  existing rate limits rather than per-sender reservations.
- New/enabled forwarding destinations receive future updates only. Daemon
  restart loses its session ledger and pending delivery. Bridge replacement
  loses its in-memory recovery queue. Neither action reconstructs old sessions.

| Combination | Expected result |
| --- | --- |
| Current desktop + current iOS + reviewed bridge candidate | Compatible, including keyed recovery and independent phone display settings |
| Current desktop + current iOS + legacy deployed bridge | Individual payloads can travel through the existing routes; new queue limits/coalescing and sender-isolated recovery are not established by the old release |
| Current iOS + old desktop/manual sender | Authorized direct LED writes remain supported; old automatic LED forwarding can bypass session display preferences and must be disabled for migration |
| Old desktop sender + old desktop receiver through new bridge | Opaque payloads remain supported up to 64 KiB **after the ingress limit is fixed** |
| Mixed old/new desktop relay endpoints | Unsupported: raw provider-hook envelopes and normalized session updates are different protocols; update both ends together |
| Old saved phone link with capability 0/1 | A valid keyed manual push can remain usable; session forwarding stays off until upgrade/re-pairing. An unkeyed legacy link needs re-pairing for current iOS |
| Older iOS builds using bare `/queued` | New bridge returns 400 for that recovery URL; those app builds need upgrading before cutover |

The older iOS implementation at `d4fd48b` used bare recovery URLs. The current
app accepts any locally authorized outer key when processing an old server's
unfiltered recovery response, so app-first rollout is possible. That old
server behavior still lacks sender isolation; keep the transition short.

## Release gates

1. **Freeze the bridge changes.** Review and commit the eight dirty files,
   including sender-filtered recovery, legacy desktop size support, and token
   handling. Pin the commit, Cargo.lock, toolchain/container, Linux architecture,
   binary SHA-256, and deployment helper/config revision. Do not package an
   unexplained working-tree binary as the release.
2. **Fix the ingress limit.** `sidepulse-bridge/deploy/nginx.conf` currently has
   `client_max_body_size 2k`, so nginx rejects legacy desktop bodies before the
   backend's 64 KiB allowance can apply. The simplest matching configuration is
   a 64 KiB ingress ceiling while Rust continues enforcing 2 KiB on phone routes.
   Update the deployment documentation too, and test through nginx: a >2 KiB
   desktop message succeeds; a >2 KiB phone message and >64 KiB desktop message
   fail. This implements the already-agreed size policy; it adds no protocol mode.
3. **Use a release-capable iOS toolchain.** The app and widget target iOS 27.0,
   as previously agreed. This Mac currently selects Xcode 26.1.1 / SDK 26.1.
   Its unsigned simulator build succeeded with deployment-target warnings;
   this does not validate a signed iOS 27 release. Select/install a compatible
   Xcode and validate the app plus widget archive. Keep the iOS 27 minimum.
4. **Prepare actual distribution metadata.** Marketing version is 1.0 and build
   number is currently 1. Check App Store Connect and increment app/widget build
   numbers together. Preserve bundle ID `io.sidepulse.ios` and the extension's
   identity. Confirm production APNs entitlement/configuration and topic.
5. **Inventory clients before tightening recovery.** Identify older iOS and
   Android builds using bare recovery. Recommended policy: require updated
   apps; do not reintroduce unauthenticated recovery. Agree any older-client
   transition requirement before scheduling the bridge switch.

## Rollout order

### 1. Qualify the frozen candidates locally

Run from the desktop checkout, adjusting sibling paths if necessary:

```sh
cargo test --locked --manifest-path ../sidepulse-bridge/Cargo.toml
cargo clippy --locked --all-targets --manifest-path ../sidepulse-bridge/Cargo.toml -- -D warnings
cargo build --locked --manifest-path ../sidepulse-bridge/Cargo.toml
python3 -m unittest discover -s ../sidepulse-bridge/deploy -p 'test_*.py'
swift test --package-path ../sidepulse-ios/SidePulse
SIDEPULSE_BRIDGE_BINARY="$PWD/../sidepulse-bridge/target/debug/sidepulse-bridge" \
  PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_program_status_bridge.py tests/test_session_delivery_flow.py \
  tests/test_links.py tests/test_program_updates.py
```

Build the signed iOS release with the compatible SDK. Before distribution,
perform an actual TestFlight/production-APNs canary as well as a development
canary. `dev_` tokens use APNs sandbox; unprefixed tokens use production.
Provider acceptance and `/healthz` are not handset-delivery proof.

### 2. Make the updated iOS build available first

- Update participating phones before the bridge rejects bare recovery URLs.
  Keep existing sender keys, display preferences, patterns, and USB-folder
  bookmarks. Do not reset application data as an upgrade step.
- Disable old desktop automatic LED-to-phone output for the participants.
  Explicit manual pushes and Shortcuts remain supported. This prevents the old
  desktop from overriding the phone's session display preferences.
- Check APNs token/environment after installation. A Debug-to-TestFlight change
  can invalidate the saved desktop destination; re-pair when the token changes.
- Require iOS 27 on participating devices. Resolve older-device support before
  the switch if it is required; do not silently lower the deployment target.

### 3. Deploy the bridge during an idle window

- Record the serving binary's complete hash, active release, ingress/unit
  configuration, and credential-file paths/permissions. Back up configuration
  privately; never put keys, phone tokens, or credentials into reports or Git.
- Retain the old binary and everything it needs for rollback. If it uses Redis,
  preserve that existing rollback dependency/configuration through the rollback
  window. The new bridge uses memory; do not introduce Redis into the new design.
- Build the frozen Linux release and use the existing readiness-gated
  `deploy/sidepulse-deploy` procedure documented in `deploy/ROLLING.md`.
  This starts/checks a candidate before switching ingress. Do not invoke it
  as part of this audit or local tests.
- Verify the expected release header through TLS, credential availability,
  the corrected ingress limits, and a disposable desktop channel's SSE
  reconnect/delivery. Then verify one authorized phone's actual receipt.
- If FCM is enabled for existing Android clients, verify an Android receipt
  and keyed recovery too. Preserve its working credentials/configuration;
  health success alone does not establish either push provider's health.
- Expect a small delivery gap and an empty recovery queue after replacement.
  Old SSE listeners and new POSTs can briefly hit different processes. The
  bridge is best-effort, not a zero-loss or durable event service.

### 4. Upgrade desktop senders and receivers together

- Merge desktop PR #72 only when the bridge and updated app are ready. Normal
  `sidepulse update` follows main. For a controlled pre-merge canary, pin the
  tested source rather than relying on a moving branch:

```sh
SIDEPULSE_INSTALL_SPEC='git+https://github.com/inteliwear/sidepulse.git@1b937cff788388b2b5ed890d6ed2166ed58a3868' sidepulse update
```

- Upgrade both endpoints of each desktop relay pair in the same window. Leave
  unaffected old/old pairs alone until their coordinated upgrade.
- Preserve settings, links, and history; verify one installed daemon owns
  physical output and the tray is its client. Restart agents so they load the
  newly installed hook commands and refreshed trust configuration.
- Re-pair phones only for changed tokens or saved capability 0/1. A current
  valid capability-2 link does not need blanket key rotation.
- Enable **Forward sessions** intentionally for each destination. The setting
  is separate from old LED-display settings (`phone_session_sync` in settings).
  Existing sessions are not replayed; generate new hook events to populate phones.
- Update scripts using removed CLI aliases/commands. Use `sidepulse`, `sp`,
  `sidepulse status`, `sidepulse live`, and explicit `state=clear` reports.

### 5. Accept the end-to-end rollout

Use a test phone or account for controlled results; other real Ask sessions can
legitimately keep Ask displayed. Record versions, bridge release ID, APNs
environment, selected pattern names, and Last pattern source without secrets.

- Working -> Ask -> Done -> Clear travels from an actual installed hook through
  daemon/bridge to iOS. The app list updates for every state.
- Two paired sources using the same scope remain separate. Recovering sender A
  does not expose or consume sender B's records. Duplicate/older sequences and
  replay after Clear do not revive sessions.
- A long Unicode message arrives clipped to at most 1 KiB, with identity, state,
  and sequence intact. No ordinary valid session update is rejected for size.
- With only Ask enabled, Ask plays its configured pattern. After Ask -> hidden
  Done, session-owned output turns off and Live Activity ends when no other
  enabled session remains. Hidden states do not take over an existing manual
  pattern. Enabling Done makes a new Done update eligible for its chosen pattern.
- The session master switch stops automatic writes; the list still updates.
  Manual remote LED writes and Shortcuts still work. Explicit settings previews
  show their own provenance and are not mistaken for incoming session output.
- One combined Live Activity follows enabled session conditions. No session
  title/path or authorization secret appears on the Lock Screen.
- Foreground recovery works after an offline interval shorter than five minutes.
  Recovery older than five minutes may be gone; a new source update is required.
- Existing valid pairings, settings, history, and Shortcuts survive upgrade.
  Clean up only the canary's scopes/keys; do not erase unrelated sessions.

## Stop and rollback

Stop the rollout for cross-sender recovery, stale-state resurrection, normal
payload rejection, manual-write regression, missing real APNs receipts, or
session output ignoring the phone's display settings.

1. Turn off the affected forwarding destination or stop the affected source
   daemon in the maintenance window. Do not send global reset/snapshot messages.
2. Roll back a desktop relay pair together, pinned to its recorded known-good
   revision. Plain `sidepulse update` is not a rollback after main has changed.
   Before starting older software, ensure its automatic phone LED output is
   disabled/Manual; it does not understand the new forwarding switch. Restart
   agents. A daemon rollback starts with empty session/pending-delivery memory.
3. Roll back the bridge with its archived compatible binary and matching
   configuration through the existing deployment procedure. Restoring a legacy
   Redis-based binary requires restoring its former dependency/configuration too.
   Rolling back reintroduces the old recovery behavior; assess that explicitly.
4. Do not rely on App Store downgrades. Keep the compatible iOS build installed
   while disabling session forwarding or distributing a corrected build. Retain
   pairing keys and user preferences. A new APNs token requires re-pairing.

No rollback restores lost in-memory queues or synthesizes missing hook events.

## Evidence from this audit

| Check | Result |
| --- | --- |
| iOS Swift package | 96 tests passed |
| Rust bridge working tree | 50 tests passed; 1 credential-dependent test ignored |
| Rust strict Clippy / debug build | Passed |
| Deployment setup helpers | 2 tests passed; no production switch/rollback exercise |
| Desktop/bridge/link/update checks | 68 tests and 21 subtests passed |
| Actual Python producer -> local Rust recovery -> current Swift decoder/accounting | 6 accepted updates plus stale replay rejection; two sender keys, Unicode clipping, persistence, Clear, and disabled Done verified |
| Shared desktop/iOS status fixtures | Identical SHA-256: `abefe9cbeff6d6947101fe233eacf0a551604af35792d4863d122bf776191e25` |
| Full iOS simulator application + widget build | Passed unsigned; SDK 26.1 versus target 27 warnings remain a release gate |
| Production inspection | Health GET only; no production POST, recovery drain, credential access, or deployment |

Still required: committed bridge artifact, ingress correction and nginx-path
tests, compatible signed iOS release, actual APNs/USB/Live Activity checks, and
the approved production switch/rollback rehearsal. Standalone parser/accounting
success is not a claim that a real background APNs callback or USB write ran.
