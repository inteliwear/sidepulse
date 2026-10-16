# OSC 7501 status integration

Updated October 14, 2026. This plan reflects the simplified implementation on `codex/osc7501-integration` and supersedes the earlier retained-status bridge design.

## Flow and ownership

```text
Local:          provider → hook handler → daemon → UI / SidePulse
Remote desktop: provider → hook handler → sender daemon → existing bridge → receiving daemon → UI / SidePulse
Remote phone:   provider → hook handler → sender daemon → existing bridge → iOS / Android → SidePulse
CLI input:      raw OSC or JSON → daemon (same accounting and delivery path)
```

- The hook adapter translates provider events to OSC-compatible status fields. It owns provider-specific interpretation, IDs, and lifecycle cleanup.
- The CLI decodes one raw OSC line or accepts JSON/status flags. OSC terminators are optional for explicit CLI ingestion. Text becomes plain UTF-8 in JSON.
- The daemon durably accounts records and allocates sequences/revisions. Hooks submit locally and return quickly; a sender worker handles asynchronous delivery and retries.
- The existing bridge carries JSON messages using its existing routes and queues. It does not interpret status fields, aggregate sessions, or choose animations.
- Receiving daemons and phone apps validate snapshots, account independent sessions, ignore stale revisions, and render locally. Desktop UI presentation preserves existing hook behavior through an isolated compatibility layer.

## Contract

The [protocol reference](../protocol/program-status.md) and [JSON schema](../protocol/program-status-v1.schema.json) define fields, identities, validation, lifecycle, and limits.

Status fields are `state`, `id`, `app`, `kind`, `title`, `msg`, and `progress`. Each report replaces the complete addressed record. Hierarchical clears affect the addressed record and descendants. Explicit Working/Blocked do not become Done through silence. Hook batches commit related state and permission cleanup together.

Network delivery carries complete publisher snapshots. Removing a record from a newer snapshot removes it at the receiver; an empty snapshot clears that publisher. The publisher ID survives restart, and revisions increase monotonically. Optional desktop context preserves names, projects, session-opening metadata, and legacy deduplication. Phone snapshots omit this context.

## Existing bridge transport

Desktop: POST the snapshot to `/api/leds/{channel}` and consume it on the service's existing SSE receiver alongside legacy hook messages. No second connection or polling endpoint is needed. A running sender checks for changes every five seconds and republishes unchanged state every 60 seconds.

Phone: POST `{"data": <complete snapshot>}` through the existing APNs/FCM route. The app processes snapshots directly from normal push delivery or its existing `/queued` recovery. Only the outer bridge-bound sender key authorizes a snapshot.

Recovery uses the existing guarantees: the last five messages for disconnected desktop channels, the latest push per phone, and the default five-minute queue TTL. Receivers keep accepted records locally. Missing intermediate snapshots is harmless; missing a final update after the sender stops and the queue expires can leave old state. Recovery after that depends on a later sender resend. This limitation is accepted instead of adding server-side snapshot retention.

The extra status endpoint, Redis status namespace, per-publisher server storage, revision-conflict handling in the bridge, push hints, and independent mobile status fetchers have been removed. Existing bridge Redis queues, FCM support, and deployment work are preserved. No OSC-specific bridge deployment is needed.

## Phone presentation and compatibility

Phones advertise `status_protocol: 1` when paired. Updated clients account status independently from physical LED writes. Unsupported links require an app update and renewed pairing for session sync; manual pushes still work. A successful direct write pauses automatic presentation; a newly accepted snapshot resumes it, while duplicates do not.

Full snapshots must fit a conservative 3,000-byte push budget including JSON-string escaping. Oversized snapshots pause delivery until they fit; there is no direct LED fallback, truncation, or chunking. Each phone has a tray **Sync session states** toggle, enabled by default for capable links and independent of desktop patterns or display mode. The daemon and tray forward state only; the phone owns its LED rendering. Manual pushes remain explicit. Disabling sync leaves the last received state; enabling sends the latest snapshot without manufacturing a revision.

Raw diagnostics and existing legacy hook delivery remain for compatibility. Normalized and legacy desktop records are deduplicated. Capacity limits remain a known difference from main; the implementation does not claim unlimited accounting.

## Scope and verification

Permission approval responses and automatic terminal capture/probing are outside this change. The CLI supports explicit OSC submission without adding a terminal emulator.

Verify field normalization and accounting with shared desktop/Swift/Kotlin fixtures. Exercise real local sockets, existing bridge POST/SSE and phone recovery routes, queue bounds, stale replay, sender restart, and existing Redis queue persistence. Preserve provider parity and local-device output; verify phones receive state only and manual pushes remain available. See [automated evidence](../testing/osc7501-testing.md) and the [human checklist](../testing/osc7501-human-test-plan.md).

No production deployment or physical device validation is implied by local tests.

## Mobile session visibility

Implemented a Sessions summary on Home and a full session list in iOS and Android. Rows are derived from the existing accounting ledger and show the session title, status, message, progress, agent, sender, and update time. Child records contribute to their parent session's status. Errors and blocked work appear first. Sessions remain visible during manual LED playback and without hardware, restore after restart, and disappear on a valid clear or sender removal. This adds no transport or persistence layer.
