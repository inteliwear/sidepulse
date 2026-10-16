# Program status integration, version 1

SidePulse accepts JSON using OSC 7501 status fields. Hook adapters interpret provider events; receivers account explicit records and render their own LEDs. The CLI also accepts a raw OSC 7501 sequence and converts it to the same JSON before submission. Existing hook presentation remains compatible with the legacy UI and relay during migration. The daemon/bridge protocol carries JSON, without terminal escape sequences or arbitrary LED programs.

The semantic reference is [Program Status OSC 7501, revision 0.4](https://www.superlogical.com/rex/docs/build/program-status). This JSON transport, sequencing, lifecycle messages, and snapshots are SidePulse extensions. Native terminal capture and capability probing are not implemented by this change.

## Local quickstart

Run the SidePulse background service for live device output and remote delivery, then report from any integration:

```sh
sidepulse report --scope deploy-42 --state working --app deploy --progress 10
sidepulse report --scope deploy-42 --state blocked --kind permission --msg 'Approve deployment?'
sidepulse report --scope deploy-42 --state done --msg 'Deployment complete'
sidepulse report --scope deploy-42 --end
sidepulse report --scope deploy-42 --state clear
```

Pass a raw OSC line directly (Bash and Zsh quoting shown):

```sh
sidepulse report --scope deploy-42 --osc $'\e]7501;state=working:app=deploy:progress=10\e\\'
sidepulse report --scope deploy-42 --osc $'\e]7501;state=blocked:kind=permission:app=deploy:msg=QXBwcm92ZSBkZXBsb3k/\a'
# The terminator is optional when submitting through the CLI:
sidepulse report --scope deploy-42 --osc $'\e]7501;state=working:app=deploy'
```

Or pipe one sequence from a producer:

```sh
printf '\033]7501;state=done:app=deploy\033\\' | sidepulse report --scope deploy-42 --osc
```

`--osc -` also reads stdin. Supply actual ESC/BEL bytes, not literal backslash escapes or the printed words `ESC` and `ST`. Both ESC-backslash and BEL terminators are supported, but the terminator can be omitted: the CLI argument or stdin EOF supplies the boundary. One optional trailing LF or CRLF is accepted in either case. The opening ESC ] 7501 ; prefix is still required. `--scope` supplies the session boundary that a terminal would normally provide; use the same scope for subsequent updates and a distinct scope for unrelated jobs. `--request-id` can be used for retries just as with JSON reports.

OSC `msg` and `title` are standard base64 UTF-8 (padding optional); the CLI decodes them to plain JSON strings. For example, `QXBwcm92ZSBkZXBsb3k/` becomes `Approve deploy?`. Sequence size is capped at 4,096 bytes. Invalid framing, text, identifiers, or exceeded limits reject the report before submission. Malformed pairs are skipped, unknown keys are ignored, and the last valid pair for a repeated key wins. Invalid optional progress/kind/app values follow the normalization rules below. The decoder checks text validity and limits on every pair before duplicates are replaced.

Each invocation accepts exactly one status sequence. Mixed terminal output, multiple sequences, and capability queries are rejected. This is explicit CLI ingestion, not terminal capture or emulation. `--osc`, `--json`, `--state`, and `--end` are separate input modes; put all status fields inside the OSC sequence when using `--osc`.

For exact control, send JSON on stdin:

```sh
printf '%s\n' '{"v":1,"type":"program_status","scope":"deploy-42","request_id":"deploy-42-step-2","status":{"state":"blocked","kind":"permission","app":"deploy","id":"release","msg":"Approve deployment?"}}' | sidepulse report --json
```

The acknowledgement is `{"ok":true,"seq":2,"revision":2}` (numbers depend on earlier reports). It means **durably accepted locally**, not delivered to a bridge, phone, or LED device. If the daemon is unavailable, the CLI and hook handler save to the same SQLite ledger for later processing. Reuse a `request_id` only when retrying the identical JSON object. The last 4,096 request receipts are retained; retries beyond that window need an explicit sequence to guard against replay.

Local clients normally omit `seq`; the ledger allocates it atomically per scope. If supplied, it must be exactly the previous sequence plus one. `scope_end` removes live/idle records and preserves Done/Error. `scope_reset` removes every record in a scope. A process that exits or is cancelled must report lifecycle cleanup; silence never means Done. Unknown process death is not detected automatically by this transport.

The local Unix socket is `program-status.sock` in SidePulse's state directory (a private, hashed temporary socket path is used when the directory exceeds Unix socket path limits). It uses one UTF-8 JSON object, a client write-side EOF, then one JSON acknowledgement and server EOF. Files and socket use owner-only permissions. Reads have a one-second total deadline and a 64 KiB bound. Prefer the CLI for durable fallback and idempotency.

Hook adapters submit related updates atomically with a local `program_status_batch` envelope: `{"v":1,"type":"program_status_batch","request_id":"event-unique-id","reports":[...reports...]}`. It contains 1–16 ordinary status/lifecycle reports, cannot nest, and shares the 64 KiB limit. All changes and request receipts commit together or roll back together. The acknowledgement is `{"ok":true,"reports":[...acknowledgements...]}`. Retrying the same batch request ID returns the original acknowledgements. Remote transport still sends full snapshots.

## Responsibilities and flow

```text
Provider hook → hook adapter ─┐
Raw OSC line → CLI decoder ──┼→ JSON report → local daemon → accounting → UI / device
JSON / CLI status flags ─────┘                    │
                                                 └→ relay worker → bridge → receiving daemon / phone
```

- `program_adapter.py` is producer-only: interpret provider hooks and construct explicit status/lifecycle batches. Ordinary hooks submit locally and return; they do no network delivery.
- `osc7501.py` is another producer adapter: decode terminal framing and base64 once. It generates the same status fields without provider metadata or legacy aliases.
- `program_status.py` accounts validated JSON, persists records and ordering, and constructs full snapshots. `program_transport.py` handles local submission and asynchronous delivery over the existing bridge. If the daemon is down, the producer saves to the same local ledger for later delivery.
- `program_presentation.py` combines accounted state with isolated legacy compatibility presentation for the desktop. Daemon, UI, and relay code do not import the provider hook adapter. Explicit OSC/JSON reports keep their state until replacement or lifecycle cleanup; legacy hook timeout heuristics do not apply to them.
- The bridge uses its existing message queues. Receiving desktops and phones account the normalized fields and render locally; raw OSC never needs to be parsed there. The existing mobile JSON contract is unchanged.

This flow reports status only. A blocked permission record does not grant permission, and no approval-response channel is implemented.

## Status object

| Field | Meaning and bounds |
| --- | --- |
| `state` | Required: `idle`, `working`, `blocked`, `done`, `error`, `clear`. Unknown states reject the update. |
| `id` | Optional hierarchical record ID. At most 128 UTF-8 bytes, eight slash-separated segments, each 1–32 ASCII characters from `A–Z a–z 0–9 _ . + -`. Omission targets the root. |
| `app` | Optional application identity, at most 32 bytes and the same segment grammar. Invalid grammar is ignored; invalid type/size rejects. Children inherit the nearest ancestor's app when omitted. |
| `title` | Optional plain UTF-8, at most 192 bytes. |
| `msg` | Optional plain UTF-8, at most 2,048 bytes. It is not base64 in JSON. |
| `kind` | `permission`, `question`, or `auth`, meaningful only with `blocked`. Unknown/irrelevant values are ignored. |
| `progress` | Integer 0–100, meaningful with `working` or `blocked`. Invalid/irrelevant values are ignored. |

Each update **replaces** the complete record. Missing optional fields disappear; omitted progress does not retain the previous value. A named clear removes that ID and descendants at slash boundaries (`a` clears `a/b`, not `ab`). A root clear removes the entire scope. Snapshot records never contain `clear`.

Text and identity fields reject C0/C1 controls and invalid Unicode. Unknown fields are ignored. Scope, publisher, and request identifiers are nonempty text of at most 128 UTF-8 bytes. Integers are nonnegative JSON integers within the interoperable 53-bit range; booleans are not integers. `updated_at` is Unix milliseconds, bounded to year 9999.

The [producer schema](program-status-v1.schema.json) describes recommended output. Receiver normalization intentionally tolerates unknown fields and invalid optional `kind`/`progress` values as described above. Byte limits are enforced by implementations, beyond JSON Schema character counts.

## Remote transport and recovery

Configure the existing remote link on the sending and receiving computers. The daemon checks for outgoing changes every five seconds and sends complete local snapshots over the existing bridge. The service receives snapshots on its existing SSE connection alongside legacy hook messages. Updates remain in SQLite during outages and are retried; unchanged snapshots are resent every 60 seconds while the sender daemon runs. Network delivery stays separate from device rendering.

An HTTP producer can also POST a complete snapshot to the existing `/api/leds/{channel}` route. The name of this generic relay route does not require LED content:

```json
{
  "v": 1,
  "type": "program_status_snapshot",
  "publisher": "persistent-installation-uuid",
  "revision": 42,
  "scopes": {
    "deploy-42": {
      "seq": 7,
      "records": [
        {
          "status": {"state": "blocked", "kind": "permission", "app": "deploy", "id": "release", "msg": "Approve deployment?"},
          "updated_at": 1791936000000
        }
      ]
    }
  }
}
```

Keep `publisher` stable across restarts and persist its monotonically increasing `revision`. Every snapshot contains **all** current scopes/records for that publisher. Omitting a previously present scope or record removes it. A snapshot with `scopes: {}` clears that publisher. It does not clear other publishers. Send the next full snapshot after missing acknowledgements; there is no unsafe per-record high-water shortcut.

POST returns the existing `OK` or `OK QUEUED` acknowledgement. This confirms bridge acceptance, not receiver persistence or hardware output. Desktop receivers use `GET /api/leds/{channel}` as an SSE stream. The bridge treats the snapshot as an ordinary message; receivers ignore revisions at or below their stored revision. Publishers must not share an identity unless they coordinate its revision and complete state.

The route capability binds the sender. `publisher` and `scope` are identifiers, not authentication. Use high-entropy channels and HTTPS outside loopback development. Phone POST routes use the existing `apns_<device>_<sender-key>` or `fcm_<registration>_<sender-key>` capability; the bridge injects the authoritative `shared_key` and strips a forged envelope key. Phone recovery uses `GET /api/leds/{device-route}/queued` without a sender suffix and consumes the queued messages. Phone POST bodies wrap the complete snapshot as `{"data": <snapshot>}`. The app trusts only the outer bridge-supplied sender key, never a key inside `data`. Apps accept only their active sender keys and namespace state by local sender identity plus publisher.

Receivers limit snapshots to 64 KiB, 64 scopes, and 256 records, and hold at most 64 publishers. These are client accounting limits; the bridge does not interpret or validate OSC fields. Exceeding local capacity rejects the update without silently evicting accepted state.

Recovery intentionally uses the existing bridge guarantees: desktop channels queue the last five messages while disconnected, with a default five-minute TTL; phone recovery keeps the latest push per device and drains it on fetch. Complete snapshots make skipped intermediate updates harmless. If a receiver misses the final snapshot and the sender stops before recovery, the queue may expire and the receiver can remain at its last saved state. A running sender repairs this on the next periodic snapshot. Expiry or silence never means Done. No new status endpoint, server-side snapshot registry, Redis namespace, or push-hint/fetch protocol is required. The bridge's existing Redis-backed queues and deployment behavior are unchanged.

Removing/changing a desktop receiving link removes its previously received state locally. Receiver persistence preserves accepted records across local restarts independently of queue retention.

## Phones and direct LED control

Updated iOS/Android apps advertise `device.status_protocol: 1` when paired. Re-pair existing links to enable status delivery. Old/manual links default to direct LED delivery, preserving compatibility. The existing bridge supports this transport; no bridge update is required.

For a capable phone with **Sync session states** enabled in its tray submenu, the desktop sends a combined snapshot of its local and received remote sessions inside an ordinary push's `data` field. APNs and queued recovery carry an object; FCM carries JSON text. The phone validates, persists, and renders the snapshot directly using its existing push/recovery handler. There is no separate fetch or status recovery timer. Push delivery and background execution remain best-effort under mobile OS limits.

Phone snapshots use a conservative 3,000-byte budget measured with FCM's JSON-string escaping, leaving room for provider and sender metadata. If the complete snapshot does not fit, syncing waits until it fits again; the phone keeps its last received state. The desktop never falls back to direct LED control, truncates state, or splits messages that would overwrite one another in the latest-only phone queue.

Session syncing is enabled by default for status-capable links, with a saved per-phone checkbox in the tray. It is independent of Mac animations, Battery/Manual display choices, and desktop hook visibility heuristics. Disabling sync pauses forwarding without clearing the phone's records; re-enabling sends the current full snapshot without changing its revision. Older links need an updated app and renewed pairing to advertise status support. The daemon and tray never automatically send phone colors, including to older links. Explicit commands such as `sidepulse push` remain available. Phones choose session patterns and LED automation locally. Status and manual LED messages still share the existing recovery slot: only its latest message is recoverable.

For explicit status records, aggregate priority is Error, Blocked, Working, Done, Idle; newest update wins ties, then stable identity. `blocked` maps to Waiting for Input, not Error. Missing messages do not imply inactivity. Done is visible for 20 minutes; explicit Working/Blocked/Error do not expire based on silence. Existing hook sessions retain main’s presentation rules, including tool detail, idle/stale visibility, and its post-tool completion heuristic. This compatibility projection does not rewrite stored OSC states or apply those heuristics to third-party JSON reports. Desktop active counts count sessions rather than every child record. Apps may use different animations and device capabilities for the same aggregate state.

A successful direct LED write pauses automatic phone presentation without deleting status records. The next **newly accepted** snapshot resumes it, even if the aggregate state has the same name as before the manual write. A duplicate snapshot does not resume it. Toggling Mac session sync does not invent a new revision or override a manual phone pattern when records have not changed. Reconnect retries pending automatic output, while manual mode stays manual. Failed writes leave automatic output retryable; Android uncertain writes are not blindly repeated. Done expiry is reevaluated while an app can run and on recovery, not by a guaranteed background timer.

## Hook migration

Claude, Codex, and the existing provider adapters emit normalized records using the existing event interpreter. Successful tool-return events stay Working; tool failures, permission denials, explicit status overrides, and notification-driven changes retain their existing interpretation. Permission waits are separate child records, so unrelated tools cannot dismiss them. Permission identity uses `tool_use_id` when supplied, falling back to tool name and arguments for providers that omit it. Identical concurrent requests without invocation IDs cannot be distinguished and share a fallback record. Tool completion clears its matching invocation and fallback record; stop/new-turn lifecycle clears that agent's pending permission subtree. A SessionEnd hook preserves the existing completion indication, then cleans live scope records. Notifications are normalized; a late notification cannot revive an already completed record. Internal Codex sessions suppressed by the existing collector remain suppressed across later sparse hooks.

Raw hook diagnostics and existing UI/relay socket delivery remain available even when normalized submission succeeds, allowing older receivers to keep working. Optional `context` outside the OSC status fields preserves provider identity, session/agent IDs, project paths, names, origins, tool detail, and source host for desktop presentation. It is validated as bounded text, forwarded with desktop snapshots, and omitted from combined phone snapshots. Phone status parsers need no provider-specific interpretation.

The desktop merges hook records into one UI row per agent and uses context to suppress duplicate legacy delivery, including on remote receivers. Existing session-opening metadata and recent history remain available. A newer legacy event after failed normalized persistence is presented instead of being hidden. Hook notifications use a local `unless_state: "done"` guard; request receipts still make those no-op submissions retryable. These compatibility controls do not alter OSC status field semantics.

See the [design plan](../design/osc7501-integration-plan.md) and [testing strategy and evidence](../testing/osc7501-testing.md).
