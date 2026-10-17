# OSC 7501 status integration

Updated October 16, 2026. This plan reflects the simplified implementation on `codex/osc7501-integration` and supersedes the earlier retained-status bridge design.

## Flow and ownership

```text
Local:          provider → hook handler → [OSC JSON] → daemon → physical LEDs
UI:             daemon ↔ UI (status view and output controls)
Remote desktop: provider → hook handler → sender daemon → existing bridge → receiving daemon → UI / SidePulse
Remote phone:   provider → hook handler → sender daemon → existing bridge → iOS / Android → SidePulse
CLI input:      raw OSC or JSON → daemon (same accounting and delivery path)
```

- The hook adapter translates provider events to OSC-compatible status fields. It owns provider-specific interpretation, IDs, and explicit completion/clear states.
- The CLI decodes one raw OSC line or accepts JSON/status flags. OSC terminators are optional for explicit CLI ingestion. Text becomes plain UTF-8 in JSON.
- The daemon owns live records in memory and allocates sequences/revisions. Hooks submit locally and return quickly; a sender worker handles asynchronous delivery and retries.
- The existing bridge carries JSON messages using its existing routes and queues. It recognizes session identity and sequence only to replace queued values; it does not account sessions or choose animations.
- Receiving daemons and phone apps validate individual updates, account independent sessions, ignore stale sequences, and render locally. The desktop UI is a daemon client, not another accounting owner. Desktop UI presentation derives entirely from accounted JSON state. Raw hook socket delivery, raw bridge envelopes, alias deduplication, and log/transcript recovery are removed from the live path.

## Contract

The [protocol reference](../protocol/program-status.md) and [JSON schema](../protocol/program-status-v1.schema.json) define fields, identities, validation, lifecycle, and limits.

Status fields are `state`, `id`, `app`, `kind`, `title`, `msg`, and `progress`. Each source/session has one state. Every report replaces it; IDs are descriptive metadata. Any clear removes that session. Explicit Working/Blocked do not become Done through silence. Each hook reports one complete session state; no permission subtree is maintained.

Network delivery carries one report at a time: source (`publisher`), session (`scope`), monotonically increasing `seq`, `updated_at`, and OSC status fields. Each receiving phone owns its accounting and display. Trusted sender identity plus publisher plus scope isolates multiple sources. Clears and stale-update guards use that same identity.

## Existing bridge transport

Desktop: POST each update to `/api/leds/{channel}` and consume it on the existing SSE receiver. Phone: POST `{"data": <update>}` through existing APNs/FCM delivery and queued recovery. Only the outer bridge-bound key authorizes it.

The daemon keeps live sessions, a bounded 4,096-event queue, receipt checks, and delivery cursors in memory. It owns physical LED output even while the UI is running. Hooks submit over the local socket and return; they never open a state store. The UI reads the daemon view and sends controls over that socket. A daemon restart starts empty; reopening the UI retains the running daemon state. No database or state-file fallback is used. Configuration and diagnostic logs remain separate files. A worker checks the in-memory queue every second, retries failures, and preserves ordering per destination.

Queues retain the latest update per sender/publisher/session, at most 10 items per routing key, for at most 24 hours. Missed intermediate updates are possible; offline delivery is best-effort and a lost clear can leave stale state. This accepted tradeoff does not add bridge endpoints, Redis changes, push hints, or snapshot repair.

## Phone presentation and compatibility

Phones advertise `status_protocol: 2`; snapshot-only links need updated apps and renewed pairing. Desktop raw-hook and snapshot messages are ignored; both desktops must use the individual JSON protocol. Phone snapshot parsing and old saved-cache migration are removed. Both apps accept only individual `program_status` updates and the current saved-session format; older caches start fresh.

Each update has its own 3,000-byte encoded budget. Clip only `msg` at Unicode boundaries to fit, preserving IDs, sequence, state, and clear updates. Each receiver retains the 64 most recently updated nonempty sessions across sources, evicting the oldest on overflow rather than rejecting fresh updates. Accepted arrival order is maintained while the daemon runs; eviction preserves replay guards and does not send synthetic clears. Per-phone **Forward sessions** toggles forwarding; the phone picks patterns. Manual pushes remain explicit. Paused syncing resumes pending retained events, without manufacturing state changes.

## Scope and verification

Permission approval responses and automatic terminal capture/probing are outside this change. The CLI supports explicit OSC submission without adding a terminal emulator.

Verify field normalization and accounting with shared desktop/Swift/Kotlin fixtures. Exercise real local sockets, existing bridge POST/SSE and phone recovery routes, queue bounds, stale replay, sender restart, session coalescing, oldest eviction, and a maximum 24-hour expiry. Preserve provider parity and local-device output; verify phones receive state only and manual pushes remain available. See [automated evidence](../testing/osc7501-testing.md) and the [human checklist](../testing/osc7501-human-test-plan.md).

No production deployment or physical device validation is implied by local tests.

## Mobile session visibility

Implemented a Sessions summary on Home and a full session list in iOS and Android. Rows are derived from the existing accounting ledger and show the session title, status, message, progress, agent, sender, and update time. Each sender/publisher/session has one row; IDs never create child statuses. Errors and blocked work appear first. Sessions remain visible during manual LED playback and without hardware, restore after restart, and disappear on a valid clear or sender removal. This adds no transport or persistence layer.

Both apps support configurable session LED patterns, direct remote LED writes, and local shortcut writes. A successful manual write owns LED output until a newer accepted session update or an explicit presentation-settings change. iOS also has one combined session Live Activity, independent of LED settings, updated locally when iOS runs the app. No ActivityKit push service is added.
