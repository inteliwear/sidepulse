# OSC status integration: testing strategy and evidence

Updated October 14, 2026. Branch: `codex/osc7501-integration`. The integration now uses the existing bridge routes and queues. Earlier tests for a separate retained-status endpoint were replaced with tests of the simpler transport.

## Coverage

| Layer | Evidence |
| --- | --- |
| OSC decoder / CLI | Both terminators and omitted terminators; stdin and arguments; base64/Unicode, duplicate and malformed pairs, byte limits, atomic rejection, durable offline submission, idempotent retries. |
| Accounting | Shared fixtures across desktop, Swift, and Kotlin; full replacement, hierarchy, clear/end/reset, stale revision rejection, identity isolation, persistence, concurrent transactions. |
| Hook compatibility | Five providers compared with the existing interpreter; metadata, notifications, failures, permission waits, session resume details, legacy deduplication, timeout presentation, and custom/battery output. |
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

The iOS simulator received sample pushes through the normal app delegate and accounting path. Verified four sessions without hardware, unauthorized-clear rejection, valid clear, stale replay rejection, atomic malformed-record rejection, newer replacement, object/string envelopes, and persistence after process restart. The test used an isolated simulator with a synthetic sender key. Simulator-only build settings set the deployment target to 18.6 and used local signing; repository release settings were unchanged.

Large accessibility text was also checked on iOS; the Sessions header adapts to keep its title readable. Screenshots contain sample sessions: [Home](screenshots/ios-sessions-home.png), [session list](screenshots/ios-sessions-list.png), and [empty state](screenshots/ios-sessions-empty.png). They are app captures, not design mockups.

## Physical iPhone push and pattern settings (October 15)

Refreshed the connected iPhone's pairing after installing a development build (its prior link used a production token). Sent a small test snapshot over the existing bridge and verified that the physical iPhone persisted publisher `sidepulse-phone-test` with state `working`. No LED hardware output was verified. Repeat using the installed Python with `scripts/push-session-test.py working`; other supported states include `blocked`, `error`, `done`, `idle`, and `clear` (only clears this test publisher).

The iOS Session LED patterns screen selects library patterns independently for each OSC state and can disable session LED output without disabling accounting. Swift tests cover preference persistence, condition isolation, default/deleted-pattern fallback, edited library patterns, and write deduplication after changing a pattern at the same state. Device and simulator builds succeeded.

Simulator UI verification: selected Needs You for Needs attention through the navigation picker, then restarted the app and confirmed the saved selection. Installed and launched the updated app on the connected iPhone 16 Pro Max.

## Phone session forwarding separated from LED control (2026-10-15)

- Daemon and tray no longer send automatic LED programs to phones. Manual CLI pushes remain supported. Each capable phone has an independent, persisted **Sync session states** tray toggle, enabled by default.
- Tested custom Mac animations and Agent/Battery/Manual displays with iOS and Android forwarding; local and received remote scopes remain in the phone snapshot. Legacy links receive no automatic colors.
- Tested pause/resume per phone, unchanged snapshots retaining their revision, updated snapshots on resume, and oversized snapshots pausing until the complete snapshot fits. Existing manual push and local hardware output tests pass.
- Desktop suite: **742 passed, 5 skipped, 596 subtests passed**. The 40 packaging deprecation warnings are unchanged. Separate existing-bridge HTTP integration run: **3 passed, 1 skipped** (Redis persistence binary not configured for this run).
- Installed this source locally and verified the six changed runtime modules match the checkout. Both launch agents are running. After installation, the physical iPhone persisted an automatically forwarded snapshot at revision 3589 with five scopes, newer than its prior revision 3070. No manual snapshot publication was used for this verification. This verifies app receipt; physical LED output was not observed.
