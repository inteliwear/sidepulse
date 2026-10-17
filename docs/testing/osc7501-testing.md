# OSC status integration: testing strategy and evidence

Updated October 16, 2026. This section describes the current checkout. Earlier
results are retained in the collapsed historical notes below and do not establish
current protocol support, deployment, or installed-device behavior.

## Current contract

- Hooks submit one `program_status` JSON update to the local daemon. There are
  no batches, scope-end/reset messages, or hook-side state fallback.
- The daemon owns one current value per sender/publisher/session in memory,
  physical LED output, replay guards, and the 4,096-event forwarding queue.
  UI, `sidepulse status`, and `sidepulse live` query that daemon.
- Each receiver keeps at most 64 nonempty sessions. New accepted updates win;
  oldest sessions are evicted while sequence guards remain. Working does not
  become Done through silence; Done is visible for 20 minutes.
- The existing bridge routes carry individual updates. Its in-memory recovery
  queues keep up to 10 items per key, coalescing by sender/publisher/session,
  with at most 24-hour retention. Manual phone pushes keep their latest command.
  There is no Redis dependency, snapshot repair, or periodic state resend.
- Phones select configurable session LED patterns. Direct remote and shortcut
  writes own LEDs until a newer accepted status or explicit settings change.
  Current saved-session data survives relaunch; earlier cache formats start fresh.
- iOS has one combined local Live Activity, independent of LED output. There is
  no new ActivityKit push service. iOS controls app/background execution.

## Coverage

| Layer | Current checks |
| --- | --- |
| OSC / CLI | Raw OSC arguments/stdin, BEL/ST/omitted terminators, Unicode/base64, field and size limits, rejection, retries; identical status/live views and removed-command rejection. |
| Daemon | Shared individual-update fixtures, one state per session, source isolation and active counts, ordering, clears, eviction, malformed-update rejection, concurrency, fresh restart, no database or fallback files. |
| Hooks | Five providers compared with the baseline interpreter; real hook → socket → receiver tests; sparse metadata, notifications, suppression, failures, fast fail-open submission. |
| UI / physical output boundary | Daemon queries, reconnect, output controls, preview holds, and daemon LED ownership while UI is connected. |
| Local bridge | Real isolated POST/SSE and APNs/FCM recovery routes; coalescing, stale retries, ten-item capacity, expiry, and CLI → daemon → bridge → receiver flow. No production calls or provider credentials. |
| Mobile | Identical `program-updates.json` fixtures; persistence, multi-source isolation, dismissals, replay/eviction guards, configurable patterns, manual ownership, object/string envelopes, and rejected legacy payloads never becoming LED writes. |
| iOS activity | Combined summary counts, priority, bounded text, and app/widget compilation; full phone tap/background behavior remains a human check. |

## Reproduce locally

Run from the desktop repository with its test environment and sibling repositories:

```sh
cargo test --manifest-path ../sidepulse-bridge/Cargo.toml
cargo build --manifest-path ../sidepulse-bridge/Cargo.toml
SIDEPULSE_BRIDGE_BINARY="$PWD/../sidepulse-bridge/target/debug/sidepulse-bridge" \
  .venv/bin/python -m pytest -q
swift test --package-path ../sidepulse-ios/SidePulse
(cd ../sidepulse-android && ./gradlew testDebugUnitTest lintDebug assembleDebug)
```

Bridge integration tests launch temporary loopback processes on ephemeral ports
with synthetic tokens and no push credentials. Do not substitute a production
endpoint. The live credential-dependent FCM smoke test remains ignored.

For iOS app/widget compilation with the local Xcode used for verification:

```sh
xcodebuild -project ../sidepulse-ios/SidePulse/SidePulse.xcodeproj \
  -scheme SidePulse -sdk iphonesimulator -configuration Debug \
  -derivedDataPath /tmp/sidepulse-session-activity-build \
  IPHONEOS_DEPLOYMENT_TARGET=18.6 CODE_SIGNING_ALLOWED=NO build
```

The deployment-target override is local build configuration; it does not change
the project's release target. The human checklist is
[osc7501-human-test-plan.md](osc7501-human-test-plan.md).

## Latest recorded verification

| Check | Result |
| --- | --- |
| Desktop, including six local bridge integration checks | 900 passed, 1 skipped; 597 subtests passed. 40 existing packaging deprecation warnings. |
| Swift package | 84 passed. |
| Android unit tests | 89 passed. |
| Android lint and debug build | Passed. |
| Android focused emulator session/source/rejected-payload checks | 3 passed after the receive-path cleanup. |
| iOS simulator app and widget build | Passed. |
| Rust bridge, last source verification | 44 passed, 1 credential-dependent check ignored; local debug build passed. |

The latest cleanups were verified from source locally. They were not deployed to
production or reinstalled on the physical iPhone. Earlier installs, simulator UI
checks, and physical push evidence below apply to the specific earlier builds.
Automated checks do not establish current physical USB output, installed hook
versions, real push-provider delivery, or iOS background/tap behavior.

<details>
<summary>Historical verification notes — superseded implementations and earlier builds</summary>

The following notes are preserved as build history. Their old commands, test
counts, database/Redis claims, snapshot behavior, and installation statements
are not the current contract. Use the current sections above for reproduction.

Current contract: one latest state per source/session, including permissions. IDs are descriptive fields. Bridge recovery coalesces by authorized sender/publisher/scope, retains 10 items per routing key, and expires within 24 hours. The evidence below records successive builds; earlier hierarchy, Redis, snapshot, and persistence behavior is historical and superseded.

> **October 16, 2026 current architecture:** live state and pending updates are owned solely by the daemon in memory. The UI uses its socket; the daemon owns physical LED output. SQLite/shared-state and producer fallback persistence have been removed. Earlier persistence results below are historical.

> **Earlier October 16 transport flow:** hook adapters submit only OSC-style JSON; the daemon, bridge receiver, UI, and default CLI status/watch use that accounted state. Raw hook socket/bridge delivery and live log/transcript reconstruction are removed. Phone and desktop delivery send individual updates. Historical snapshot/replay/capacity results below describe the previous implementation. The current contract is in [program-status.md](../protocol/program-status.md); the human checklist has been updated for individual delivery and source isolation.


Updated October 16, 2026. Branch: `codex/osc7501-integration`. The integration now uses the existing bridge routes and queues. Earlier tests for a separate retained-status endpoint were replaced with tests of the simpler transport.

## Coverage

| Layer | Evidence |
| --- | --- |
| OSC decoder / CLI | Both terminators and omitted terminators; stdin and arguments; base64/Unicode, duplicate and malformed pairs, byte limits, atomic rejection, durable offline submission, idempotent retries. |
| Accounting | Shared fixtures across desktop, Swift, and Kotlin; full replacement, hierarchy, clear/end/reset, stale revision rejection, identity isolation, persistence, concurrent transactions. |
| Hook compatibility | Five producer adapters compared with the existing event interpreter; metadata, notifications, failures, permission waits, session resume details, explicit state retention, and custom/battery output. Real hook → JSON socket → accounting → remote UI tests run for all five providers. |
| Local boundary | Real Unix sockets, malformed/stalled clients, acknowledgements, SQLite fallback and restart. |
| Existing desktop bridge | Real POST/SSE; seven snapshots while disconnected retain the last five; receiver rejects stale replay and preserves clears. CLI → daemon → existing bridge → receiving service → UI requires no provider parsing downstream. |
| Existing phone bridge | Snapshot in `data`; authoritative outer sender binding; APNs/FCM queued recovery; latest-only replacement and drain behavior. No push credentials are loaded. |
| Phone clients | Object/string push envelopes, shared accounting fixtures, sender revocation, manual overrides, duplicate handling, reconnect rendering. Oversized snapshots pause phone sync without direct LED fallback. |
| Existing Redis | Ephemeral AOF-backed Redis and bridge restart preserve queued snapshots using the original queue implementation. No new status storage is involved. |
| Builds | Rust tests/build, Swift package tests and iOS simulator build, Android unit tests and APK build, desktop regression suite. |

## Reproduce

```sh
.venv/bin/python -m pytest tests/test_osc7501.py tests/test_program_status.py tests/test_program_hook_parity.py tests/test_relay.py -q
cargo build --manifest-path ../sidepulse-bridge/Cargo.toml
SIDEPULSE_BRIDGE_BINARY="$PWD/../sidepulse-bridge/target/debug/sidepulse-bridge" \
SIDEPULSE_REDIS_BINARY=/path/to/redis-server \
.venv/bin/python -m pytest tests/test_program_status_bridge.py -q
cargo test --manifest-path ../sidepulse-bridge/Cargo.toml
swift test --package-path ../sidepulse-ios/SidePulse
(cd ../sidepulse-android && ./gradlew testDebugUnitTest assembleDebug)
```

Integration tests start their own loopback servers with isolated credentials, temporary ports, and temporary ledgers. Do not substitute a production server. Omit the Redis binary to skip the existing-queue restart check. The bridge's credential-dependent FCM smoke test is not run. Shared Redis unit tests require a separate isolated `REDIS_TEST_URL`.

## Verification results

- Focused desktop protocol/decoder/relay checks: 139 passed, including the final headless fallback-cache regression case.
- Real existing-bridge integration: 4 passed, including Redis queue restart recovery.
- Rust: 35 passed, 4 opt-in tests ignored (three isolated Redis tests and one live FCM test).
- Swift: 76 passed; iOS simulator application build succeeded.
- Android: 84 unit tests passed; Debug APK and instrumentation APK builds succeeded.
- Full desktop regression: 738 passed, 1 skipped, and 596 subtests passed. Existing packaging tests emitted 40 deprecation warnings.

The headless output path now clears its cached direct program while a phone uses snapshot accounting. This ensures an oversized snapshot or other compatibility fallback sends the needed direct program even when its text matches an older cached write.

## Limits and human evidence

Queue expiry, missed pushes, and a stopped sender can leave a receiver at its last saved state. This is the accepted existing-bridge behavior. A running sender republishes every 60 seconds. Local snapshot capacity limits remain; oversized phone snapshots pause delivery until they fit, without direct LED fallback or chunking.

Automated tests do not establish physical LED output, actual APNs/FCM delivery, background execution on suspended phones, pairing UX, or installed-provider behavior. Follow the [human acceptance checklist](osc7501-human-test-plan.md). No production deployment, live push, or physical LED write was performed.

## Mobile session lists

Both apps derive one session row per scope from the existing ledger, using the same priority and child inheritance as LED presentation. Shared fixtures verify child permissions, root titles, progress, completed-record expiry, deterministic ordering, sender isolation, persistence, clearing, and stale replay rejection. No additional endpoint or storage was added.

All 8 Android app UI tests passed. The session-screen test also passed after the final layout adjustment at 1.5× font scale: accept a JSON-string envelope without hardware writes, show a blocked child under its session, recreate the activity, reject an unauthorized clear, apply a valid clear, reject stale restoration, and revoke the sender.

The iOS simulator received sample pushes through the normal app delegate and accounting path. Verified four sessions without hardware, unauthorized-clear rejection, valid clear, stale replay rejection, atomic malformed-record rejection, newer replacement, object/string envelopes, and persistence after process restart. The test used an isolated simulator with a synthetic sender key. That simulator invocation used temporary deployment/signing overrides. Separately, the branch project settings differ from main: minimum iOS 27 instead of 26, and iPhone-only instead of iPhone/iPad. Those product choices remain under discussion.

Large accessibility text was also checked on iOS; the Sessions header adapts to keep its title readable. Screenshots contain sample sessions: [Home](screenshots/ios-sessions-home.png), [session list](screenshots/ios-sessions-list.png), and [empty state](screenshots/ios-sessions-empty.png). They are app captures, not design mockups.

## Physical iPhone push and pattern settings (October 15)

Refreshed the connected iPhone's pairing after installing a development build (its prior link used a production token). Sent a small test snapshot over the existing bridge and verified that the physical iPhone persisted publisher `sidepulse-phone-test` with state `working`. No LED hardware output was verified. Repeat using the installed Python with `scripts/push-session-test.py working`; other supported states include `blocked`, `error`, `done`, `idle`, and `clear` (only clears this test publisher).

The iOS Session LED patterns screen selects library patterns independently for each OSC state and can disable session LED output without disabling accounting. Swift tests cover preference persistence, condition isolation, default/deleted-pattern fallback, edited library patterns, and write deduplication after changing a pattern at the same state. Device and simulator builds succeeded.

Simulator UI verification: selected Needs You for Needs attention through the navigation picker, then restarted the app and confirmed the saved selection. Installed and launched the updated app on the connected iPhone 16 Pro Max.

## Phone session forwarding separated from LED control (2026-10-15)

- Daemon and tray no longer send automatic LED programs to phones. Manual CLI pushes remain supported. Each capable phone has an independent, persisted **Forward sessions** tray toggle, enabled by default.
- Tested custom Mac animations and Agent/Battery/Manual displays with iOS and Android forwarding; local and received remote scopes remain in the phone snapshot. Legacy links receive no automatic colors.
- Tested pause/resume per phone, unchanged snapshots retaining their revision, updated snapshots on resume, and oversized snapshots pausing until the complete snapshot fits. Existing manual push and local hardware output tests pass.
- Desktop suite: **742 passed, 5 skipped, 596 subtests passed**. The 40 packaging deprecation warnings are unchanged. Separate existing-bridge HTTP integration run: **3 passed, 1 skipped** (Redis persistence binary not configured for this run).
- Installed this source locally and verified the six changed runtime modules match the checkout. Both launch agents are running. After installation, the physical iPhone persisted an automatically forwarded snapshot at revision 3589 with five scopes, newer than its prior revision 3070. No manual snapshot publication was used for this verification. This verifies app receipt; physical LED output was not observed.

## Individual update transport — October 16, 2026

- Desktop: **846 passed, 5 skipped, 608 subtests passed**. The 40 existing packaging deprecation warnings remain. An initial run had a stale editable-install version-metadata mismatch; it passed in the fresh process and the full final run.
- Existing-bridge HTTP plus new stream tests: **11 passed, 1 skipped** (Redis-specific optional test not configured). Real local CLI/socket/bridge/SSE flow passes with individual updates.
- Swift: **81 passed**. Signed iPhone build succeeded. Android: **89 unit tests passed**, debug and instrumentation builds succeeded. The actual Android app test with two paired senders using identical publisher/scope IDs passed, including unauthorized clears, stale replay, and preserving the other source.
- Added coverage for over 64 abandoned sessions, a large ledger producing bounded individual pushes, safe Unicode/escaped-message clipping, per-source/session ordering, clear tombstones across restart, source revocation, legacy-to-stream migration, late legacy snapshot rejection, and durable delivery cursors.
- Updated Mac installed and runtime files verified against the checkout; service and tray are running. Updated iPhone app installed and its development link upgraded to capability 2. iOS refused foreground launch because the device was locked. Bridge acknowledgement of individual events is confirmed; live receipt in the updated physical iPhone remains unverified until the app can run. Physical LED behavior was not observed.

### October 16: retain 64 sessions with newest accepted updates preferred

Each receiver now keeps the 64 most recently updated nonempty sessions across sources. Accepted arrival order is persisted separately from sender timestamps and sequence numbers. Capacity eviction removes local session records, preserves replay guards, and neither blocks forwarding nor emits a synthetic clear.

Verified refresh-before-overflow, duplicate/stale rejection without refreshing recency, eviction and re-entry with a newer sequence, differing source clocks, multiple sender namespaces, restart persistence, and transactional rollback at capacity. Clearing a session frees a place. The full desktop suite passed (848 tests, 5 skipped, 608 subtests); the focused update suite including an additional mixed local/remote rollback case passed (11 tests). Swift passed 82 tests and Android passed 90 unit tests. Signed iOS and Android debug builds succeeded; the iPhone build was installed. No live 65-session test was sent to the user's phone, to avoid evicting their current sessions.


## Single OSC-style JSON flow — October 16, 2026

Confirmed decisions: remote listeners recover automatically; hook status uses only normalized OSC-style JSON, with no raw hook fallback. Removed raw UI/service hook listeners and raw bridge publishing/receiving, legacy alias deduplication, and live log/transcript reconstruction. The settings-window command socket remains UI control only. Local raw/audit logs remain diagnostic data; the default status/watch commands use accounted JSON state. Working does not become Done because a hook stream goes quiet.

The full desktop suite passed: **855 tests, 5 skipped, 607 subtests** (40 existing packaging deprecation warnings). Existing-bridge integration passed **3 tests, 1 skipped**; the optional Redis test was not configured. Tests cover all five provider hooks through a real status socket into accounting and a remote UI, malformed/failed conversion without raw delivery, native Codex title updates, retained sparse-event metadata, UI commands rejecting raw hook envelopes, retrying the same update after a database error, dead-listener restart, and finding the daemon when the shell's state directory differs. CLI subprocess tests isolate both HOME and XDG state to avoid the running local daemon.


## Hook presentation text cleanup — October 16, 2026

Confirmed decision: clean hook display text rather than reject an otherwise valid status update. Producer context uses the receiver's shared field limits; control characters become spaces, invalid Unicode is made encodable, and long strings are clipped at UTF-8 boundaries. Identity, state, permission matching, and session-opening paths are not rewritten. Direct JSON input remains strict.

Focused regression passed **185 tests and 204 subtests**, covering five providers with control characters in prompts, long Unicode native titles, malformed Unicode messages, oversized origins/tool/source labels, unchanged permission IDs and matching clears, and atomic rejection of invalid direct JSON context. Hook fail-open behavior, raw OSC ingestion, accounting, and individual forwarding checks pass in the same run.


## Daemon owns live state — October 16, 2026

Confirmed architecture: `hook → OSC-style JSON → daemon → physical LEDs`, `daemon ↔ UI`, and `daemon → individual OSC-style JSON through the existing bridge → iOS/Android`. The daemon owns session accounting in memory. Removed SQLite, shared UI/store access, producer persistence fallback, durable queue/cursor storage, and UI ownership of automatic physical output. UI previews/holds/enablement use daemon commands; the Notch remains a UI rendering. Preferences and diagnostic logs remain separate files.

The daemon starts empty after restart. Closing/reopening only the UI preserves the running daemon state. Hook calls remain fail-open when the daemon is absent; explicit CLI reporting returns an unavailable-daemon error and writes no fallback database. Mobile apps continue independent accounting of multiple authorized sources. Their last received state is not reset merely because a desktop daemon restarts.

Full regression: **861 passed, 5 skipped, 607 subtests** (40 existing packaging deprecation warnings). Existing-bridge integration: **3 passed, 1 skipped** (Redis option not configured). The final focused run passed **168 tests**, including an additional 64-row Unicode/long-path state response and safe fallback for remote paths beyond local filesystem limits. Verified no state/database file access, fresh daemon lifecycle, stable publisher/new transport ordering, source isolation, global 64-session eviction, UI reconnect and bidirectional output controls, bounded concurrent IPC, daemon output with UI connected, manual preview holds, source-only individual phone forwarding, per-phone pause cursors, and transient startup retry. Earlier durable-state/restart tests were replaced with tests of the explicitly volatile daemon lifecycle.


## October 16: one current state and smart local bridge queues

Confirmed decision: each source/session has exactly one current state. Permission/tool IDs remain OSC metadata. Every accepted hook replaces the session state; any clear removes that session. Mobile apps collapse saved multi-record sessions to their latest value and reject incoming legacy snapshots. Forwarded values include available session titles; explicit Done/Error/Clear updates forward complete resulting values so a recovering receiver needs no earlier update.

Bridge queues retain up to 10 items per routing key and replace older queued values for the same authorized sender, publisher, and scope. Newer accepted values move to the end; stale/duplicate sequences do not replace values or refresh TTL. Overflow drops the oldest item. Retention defaults to 86,400 seconds and is capped there; shorter configured expiry is supported. Manual phone pushes retain their latest command alongside session values. All bridge tests and installation were local. No production deployment occurred.

Verification: desktop **869 passed, 1 skipped, 607 subtests**, including **6 HTTP integration checks against the locally installed bridge**. Swift **82 passed**; Android **90 unit tests passed**, lint/build passed, and both focused session UI tests passed on the emulator. Rust **44 passed, 1 credential-dependent test ignored**, on both macOS and Linux; strict Clippy and Linux release build passed. Linux emulation exposed a timing-sensitive pre-existing health/rate-limit test; its setup now uses a deterministic test-only rejecting limiter. The desktop animation preview was repaired to write through the daemon and retain its preview hold.

Reinstalled desktop daemon, hooks and tray; installed files match checkout and the daemon reports no output errors. Installed the signed iPhone build (matching development APNs entitlement); final launch requires unlocking the phone. The local build overrides the minimum iOS version to 18.6 because this Xcode cannot select the project's iOS-27 target device; release project settings are unchanged. Reinstalled and opened Android on the emulator. No physical Android phone was connected; its old saved link still needs updating/re-pairing after installation.

Local bridge installed at `~/.local/share/sidepulse/bridge-test/sidepulse-bridge`, listening only on `http://127.0.0.1:18401`, without push-provider credentials. PID and log are in that same directory. Earlier physical iPhone checks verified the previous build's CLI/hook/remote ingress through the existing production transport; those are not claimed as physical verification of this final one-state build. Current queue behavior was verified locally only.

### Terminal command coverage

`tests/test_cli_status.py` checks that `sidepulse status` and `sidepulse live
--once` produce identical text and JSON from daemon state, including working
sessions older than an hour and inactive sessions with `--all`. It checks live
polling, JSON lines without terminal control codes, cursor restoration on daemon
failure, invalid refresh intervals, and failure without offline log fallback.
Removed agent and battery command groups and raw-log flags must be rejected.
Installer tests verify upgrades remove owned legacy aliases while preserving
unrelated commands. Hook command tests cover the shared frozen-app entry point.

</details>
