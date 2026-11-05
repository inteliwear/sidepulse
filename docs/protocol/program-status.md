# Program status integration, version 1

SidePulse accepts JSON using OSC 7501 status fields. Hook adapters interpret provider events; receivers account explicit records and render their own LEDs. The CLI also accepts a raw OSC 7501 sequence and converts it to the same JSON before submission. The daemon/bridge protocol carries JSON, without terminal escape sequences or arbitrary LED programs.

The semantic reference is [Program Status OSC 7501, revision 0.4](https://www.superlogical.com/rex/docs/build/program-status). The JSON envelope, source/session identity, and sequencing are SidePulse extensions. `program_status` is the only session-message type; completion, errors, and clearing are explicit states. `sidepulse wrap` captures native terminal reports and answers capability queries on macOS and Linux.

## Local quickstart

Run the SidePulse background service for live device output and remote delivery, then report from any integration:

```sh
sidepulse report --scope deploy-42 --state working --app deploy --progress 10
sidepulse report --scope deploy-42 --state blocked --kind permission --msg 'Approve deployment?'
sidepulse report --scope deploy-42 --state done --msg 'Deployment complete'
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

Each invocation accepts exactly one status sequence. Mixed terminal output, multiple sequences, and capability queries are rejected. This is explicit CLI ingestion, not terminal capture or emulation. `--osc`, `--json`, and `--state` are separate input modes; put all status fields inside the OSC sequence when using `--osc`.

For exact control, send JSON on stdin:

```sh
printf '%s\n' '{"v":1,"type":"program_status","scope":"deploy-42","request_id":"deploy-42-step-2","status":{"state":"blocked","kind":"permission","app":"deploy","id":"release","msg":"Approve deployment?"}}' | sidepulse report --json
```

The acknowledgement is `{"ok":true,"seq":2,"revision":2}` (numbers depend on earlier reports). It means **accepted by the running daemon**, not delivered to a bridge, phone, or LED device. Session state, ordering, recent request receipts, and latest outgoing values live in daemon memory. Current values use one flat entry per authorized sender, publisher, and session. A bounded window of 128 session sequence guards remains after clear or eviction; status IDs do not create children or extra records. There is no session database, shared state file, or hook-side persistence. A daemon restart starts with no sessions or queued messages. Closing/reopening the UI preserves sessions because the daemon remains running.

If the daemon is unavailable, the CLI returns a clear error and the hook exits successfully without storing the missed update. A later hook can submit a new update once the daemon is running. Reuse a `request_id` only when retrying the identical JSON object during the same daemon lifetime; the latest 64 request receipts are retained in memory.

The local Unix socket is `program-status.sock` in SidePulse's state directory (a private, hashed temporary socket path is used when the directory exceeds Unix socket path limits). It uses one UTF-8 JSON object, a client write-side EOF, then one JSON response and server EOF. The socket uses owner-only permissions. Requests have a one-second total deadline and a 64 KiB bound. Four bounded workers keep UI/control requests from blocking hooks. UI state responses may be up to 1 MiB to accommodate 64 session rows.

Each hook submits one normalized JSON update directly. Requests containing batches are not supported; send each status update in its own request.

The Codex producer treats ordinary tool failures (including denied tool calls)
as `working`, because Codex can handle them without user input. Input/approval
requests remain `blocked`; explicit error statuses and session-level
`StopFailure` remain `error`. Receivers use these normalized states directly.

## Interactive terminal capture

```sh
sidepulse wrap codex
sp wrap claude
sidepulse wrap -- codex --help
```

`sp` is an installed alias for `sidepulse`. The first argument after `wrap` is
the command; subsequent arguments belong to that command. The optional `--`
separator is accepted. The command is executed directly, without a shell, in a
PTY with keyboard input, window-size propagation, signal forwarding, and the
original terminal settings restored on exit or suspension. Child stdout and
stderr share the PTY output stream. Redirected input disables terminal echo;
EOF uses the PTY's canonical VEOF character. Raw-mode programs that require a
real stdin EOF should be run interactively, since PTYs cannot half-close input.

Only complete ESC-backslash or BEL-terminated sequences are captured, even if
split across output reads. Unterminated reports are forwarded without applying
status. Ordinary output and status reports pass through unchanged. Capability
queries are consumed and answered inside the PTY, avoiding duplicate replies
from the outer terminal. Candidates are bounded to 4096 bytes and decoded with
the same validation as `report --osc`. Other OSCs and DCS/APC/PM/SOS strings are
opaque; tmux passthrough is not decoded.

Each invocation generates a unique `wrap-...` scope, with the latest report
replacing that scope's state. As with other SidePulse input, `id` is metadata,
and any clear removes that invocation's current state; child record trees are
not maintained. Exit clears `working`, `blocked`, or `idle`; explicit `done` and
`error` remain under the normal daemon retention rules. No completion state is
inferred from silence or a successful process exit. Programs that emit no valid
reports create no wrapper session. Installed provider hooks remain independent
and can create a separate session for the same agent.

One background worker submits JSON to the existing local daemon, retaining
only the latest pending update. A slow or unavailable daemon never blocks
terminal output; failures warn once, and shutdown waits at most one second for
the worker. Delivery remains best-effort. Run the daemon with
`sidepulse service start` before wrapping a command to receive status updates.
For Herdr or another multiplexer, wrap each agent inside its pane: the outer
interface may consume OSCs and cannot supply individual pane identities.

## Responsibilities and flow

```text
Hook handler ── OSC-style JSON ──→ daemon ──→ physical LEDs
                                     ↕
                                     UI

daemon ── OSC-style JSON / existing bridge ──→ iOS / Android
```

- The hook adapter owns provider interpretation and creates one explicit status update per event. Hooks do no network forwarding or local accounting.
- The CLI decodes raw OSC or accepts JSON and submits it to the same daemon.
- The daemon owns live session state, sequence checks, the 64-session retention order, outgoing queue, physical LED rendering, and physical LED writes. On macOS it also owns sleep prevention and grace periods, battery safeguards, lid observations and animations, power-change previews, and 15-second status-history sampling.
- The UI reads a daemon view and sends control requests. It does not instantiate an accounting store or drive physical LEDs directly. The on-screen Notch remains a UI rendering of daemon state.
- The existing bridge only transports individual JSON messages. Phones account their own received sessions and choose their own LED patterns, including updates from multiple paired senders.

### Daemon ↔ UI

The existing status socket also accepts `{"v":1,"type":"get_state"}` and returns `{"ok":true,"snapshot":...,"runtime":...,"outputs_enabled":...,"output_errors":...}` with the daemon's current display state. `runtime` includes cached battery observations, lid state, sleep-control indicators, preview expiry, and observation/history errors. Reading state has no output or history side effects. The UI/default CLI never reconstruct sessions from local logs or a previous UI process.

`output_control` changes output enablement, a temporary hold, battery-preview duration, or the keep-awake switch; it does not submit a session mode. Omitted fields leave existing controls unchanged. Explicit UI previews use `write_led` with a target, program, and bounded hold duration. `play_lid_animation` requests a configured lid animation, or an explicit unsaved preview program and bounded duration. The daemon selects per-device brightness, serializes physical writes, and owns animation expiry and restoration. Firmware installation requests an explicit output pause. Lid observation and normal lid animations require no UI. Preferences remain ordinary JSON configuration files read by the daemon; they are not a session database.

Closing or reopening the tray does not stop sleep control, restart timers, or create duplicate history samples. The daemon releases its sleep controls when it stops. History remains an append-only diagnostic JSONL file, not a source of live session state. Linux daemons continue session accounting, history, forwarding, and device output without running macOS power controls.


## Status object

| Field | Meaning and bounds |
| --- | --- |
| `state` | Required: `idle`, `working`, `blocked`, `done`, `error`, `clear`. Unknown states reject the update. |
| `id` | Optional OSC identifier, retained as descriptive metadata for the session. At most 128 UTF-8 bytes, eight slash-separated segments, each 1–32 ASCII characters from `A–Z a–z 0–9 _ . + -`. |
| `app` | Optional application identity, at most 32 bytes and the same segment grammar. Invalid grammar is ignored; invalid type/size rejects. |
| `title` | Optional plain UTF-8, at most 192 bytes. |
| `msg` | Optional plain UTF-8, at most 2,048 bytes. It is not base64 in JSON. |
| `kind` | `permission`, `question`, or `auth`, meaningful only with `blocked`. Unknown/irrelevant values are ignored. |
| `progress` | Integer 0–100, meaningful with `working` or `blocked`. Invalid/irrelevant values are ignored. |

Each source/session has **one current state**. Every accepted update replaces that state; IDs never create child records or concurrent permission states. Missing optional status fields disappear. Any `clear` clears the session, even if an `id` is present. The latest accepted hook determines the session state, so a new tool/working update replaces a prior permission request. Across different sessions, Error > Blocked > Working > Done > Idle selects the displayed pattern.

Text and identity fields reject C0/C1 controls and invalid Unicode. Unknown fields are ignored. Scope, publisher, and request identifiers are nonempty text of at most 128 UTF-8 bytes. Integers are nonnegative JSON integers within the interoperable 53-bit range; booleans are not integers. `updated_at` is Unix milliseconds, bounded to year 9999.

The [producer schema](program-status-v1.schema.json) describes recommended output. Receiver normalization intentionally tolerates unknown fields and invalid optional `kind`/`progress` values as described above. Byte limits are enforced by implementations, beyond JSON Schema character counts.

## Remote transport and recovery

The daemon forwards **individual updates**, never a combined session snapshot. Hooks submit locally and return; the worker checks the local event queue every second. Desktop delivery uses the existing `/api/leds/{channel}` POST and SSE connection. Phone delivery wraps the same update in `data`:

```json
{
  "data": {
    "v": 1,
    "type": "program_status",
    "publisher": "persistent-source-uuid",
    "scope": "session-7f3a",
    "seq": 42,
    "updated_at": 1792173600000,
    "status": {"state": "blocked", "kind": "permission", "app": "codex", "id": "permission", "msg": "Approve deployment?"}
  }
}
```

`publisher` identifies a source and stays stable across restarts. `scope` identifies its session. `seq` increases; receivers track it **per source and session**, so events from separate sessions may arrive in any order. A duplicate or older sequence is ignored. Gaps are accepted because the bridge is best-effort. The daemon assigns epoch-based, increasing transport sequences; these need not equal the local report acknowledgement sequence, and remain newer across normal daemon restarts with a progressing system clock. `updated_at` is the original update time, preserved on retries.

Phones isolate state by **authorized paired sender + publisher + scope**. Two computers may use identical publisher/session strings without colliding. Multiple publishers behind one sender remain separate. `state: clear` affects only its addressed source/session. Receivers retain the last sequence after a clear so stale events cannot resurrect it. When forwarding a remote source to a phone, the receiving daemon gives it a stable namespace separate from local and other remote sources.

The existing bridge binds the outer `shared_key` to the phone sender route. Apps trust that outer key only; a key inside `data` cannot authorize a message. APNs/recovery carry `data` as an object; FCM carries JSON text. Identifiers are not credentials.

The daemon keeps one latest outgoing value per source/session, capped at 64 values, with per-destination cursors in memory. A new or re-enabled destination starts with the next update; it never receives values stored before it was enabled. Temporary failures use exponential backoff (up to five minutes) and honor a longer `Retry-After`; invalid individual values are skipped so later sessions can proceed. Retired or invalid destinations stop retrying until their link changes. Destinations send independently with four bounded workers. A daemon restart drops pending delivery; no database or event-history replay is involved. A dead status socket or remote listener is recovered by the daemon.

The publisher ID is stored once in installation configuration, preserving the previous installation identity on upgrade. Changing the hostname or network interface does not change it. This file contains an identifier, not session state.

Bridge acknowledgement means provider/queue acceptance, **not phone receipt**. The bridge queues at most 10 items per routing key, replacing the older queued value for the same authorized sender + publisher + scope. Status IDs do not split a session. Duplicate/stale sequences do not replace newer queued values or refresh their expiry. The oldest retained item is dropped on overflow. Default expiry is 300 seconds; `QUEUE_TTL_SECS` can shorten it but cannot exceed 300 seconds. Phone requests and retained phone recovery messages each have a 2048-byte limit; oversized messages are rejected before retention. Opaque desktop channels allow 64 KiB for existing old sender/receiver pairs, without adding legacy parsing to updated OSC-only receivers. Updated OSC senders remain bounded to 2048 bytes. Delivery/drain removes the queued entries. Manual phone pushes retain only the newest manual command alongside session values.

The daemon forwards complete resulting session values. Completion, errors, and removal are explicit `done`, `error`, and `clear` states in ordinary `program_status` messages; a recovering receiver needs no preceding event. Phone updates include a session title when available. There is no snapshot repair, extra endpoint, database, or periodic full-state resend. Expired or evicted sessions can still miss a final update. Silence never means Done.

## Phones and direct LED control

Updated apps advertise `device.status_protocol: 2` for individual updates. Version-1 snapshot-only phone links must update and pair again. Desktop SSE accepts only individual updates; raw hook envelopes and legacy snapshots are ignored. Phone push parsers also accept only individual updates. Only the current saved-session format is supported. Earlier mobile caches start fresh; their old sessions and sequence guards are not imported.

The tray's **Forward sessions** checkbox controls forwarding per phone, independently of Mac animations and display settings. Disabling it pauses delivery and leaves the phone's records intact. Re-enabling starts with the next arriving update. Values received while disabled are not forwarded later. The daemon and tray never automatically send phone colors. Explicit manual `sidepulse push` commands still work; each phone chooses its own patterns.

Hook adapters and bridge senders truncate `msg` to at most **1,024 UTF-8 bytes**, preserving complete Unicode characters. Existing OSC/JSON receiver validation still accepts up to 2,048 bytes for compatibility. The full encoded request may require further clipping.

Each phone update is bounded to 1,800 encoded bytes, accounting for FCM JSON-string escaping and reserving room beneath the bridge's 2048-byte limit for sender metadata. Desktop relay messages are also bounded to 2048 bytes. They retain only provider/native-session identity in `context`, and copy a session title into the OSC title field. Full paths and presentation labels stay in the source daemon. `msg` is clipped further if necessary; metadata that still cannot fit is skipped for that destination. The sender clips only `msg`, at valid Unicode boundaries, until that individual update fits. IDs, ordering, state, and clears are preserved. Optional desktop presentation context is omitted from phone messages. Each receiver retains at most **64 nonempty sessions across all sources**, identified by paired sender, publisher, and scope. A valid new update always takes preference: updating an existing session makes it most recent; adding a session at capacity evicts the least recently updated session. Recency follows accepted arrival order within the daemon lifetime (phone apps retain their own accounting), and is unaffected by sender clock differences or rejected duplicate/stale messages. Sequence guards for the 128 most recently updated sessions survive clear, dismissal, and eviction. Older guards are dropped to bound memory; replay protection does not extend beyond that window. Eviction is local accounting only and does not emit a clear or prevent forwarding the original update.

For plain OSC statuses and phone presentation, aggregate priority is Error, Blocked, Working, Done, Idle; newest update wins ties, then stable identity. Desktop hook context can retain Tool Running and Long Task Progress as Working details, prioritized ahead of plain Working in that order. `blocked` maps to Waiting for Input, not Error. Missing messages do not imply inactivity. Done is visible for 20 minutes; explicit Working/Blocked/Error do not expire based on silence. Hook and third-party updates share these explicit state rules. The UI does not reconstruct state from provider logs or transcripts and does not infer Done after a quiet period. Desktop active counts count each distinct source/session, even when provider or session names match. Apps may use different animations and device capabilities for the same aggregate state.

Mobile **Session display** settings expose Working, Done, and Ask. Ask groups Blocked and Error. **Only Ask is enabled by default**; saved explicit preferences are preserved. Switches affect LED signaling and the iOS Live Activity, never session accounting or list visibility.

Each new accepted update in an enabled condition grants one LED attempt. Hidden/Idle states stop the previous session pattern when no enabled state remains, while preserving manual output. Startup, reconnect, duplicates, periodic accounting refresh, and failed automatic writes do not replay LED commands. Direct remote writes, Shortcut actions, and enabled session signals share the device as writers; the next eligible write wins. Automatic failures remain quiet when the Dot is unplugged. Manual Off makes one attempt and releases session output. Explicit display-settings changes apply the selected current state, or Off if none is enabled. Local dismissal can refresh an existing session output but preserves a later manual write.

The combined iOS Live Activity contains only the SidePulse Pro-style eight-light state visualization. Session names, paths, messages, and counts stay in the app. Updates use brief transitions supported by ActivityKit, not an indefinitely looping animation. The LED master switch and Live Activity setting remain independent. Foreground refresh reevaluates Done expiry immediately.

### Mobile session links

`sidepulse://sessions?publisher=<publisher>&scope=<scope>` opens the matching local session rows in either mobile app. `sidepulse://sessions` opens the full list. Session rows can share/copy their link; the Mac session options menu offers **Copy mobile session link**, and daemon/CLI JSON includes `session_link`. URLs use the same publisher namespace and scope as phone updates, without sender keys. Opening a link does not recover historical state, pair a sender, acknowledge a session, or write LEDs. A missing/cleared session shows an explanation and a Show all sessions action.

Recovery URLs require the paired sender-key suffix. Each app requests its active sender keys separately; the bridge returns and drains only matching records. Bare phone-token recovery and APNs SSE requests are rejected. Recovery never exposes another sender’s key or consumes their records. All paired senders still share the ten-item device queue and rate limits; the bridge does not maintain a sender-registration database.

### Local dismissal

Mobile apps can clear a session until its next newer update. Duplicate and stale
reports cannot restore it. Clearing changes only the local list and output;
it does not send a response to the agent or add a bridge message type.
When no enabled state remains, the phone stops its previous session pattern.
A later successful manual, Shortcut or settings-preview write stays in control.

## Hook producers

Each interpreted hook reports one complete session state. A permission request reports Blocked; the next accepted hook replaces it with Working, Error, Done, or another explicit state. The daemon does not track individual pending approvals. A SessionEnd hook reports its interpreted completion state. Late notifications cannot revive a completed session, and internal Codex sessions remain suppressed across sparse hooks.

Hook adapters submit individual normalized JSON updates. Raw provider events are never sent through UI sockets or the bridge; conversion/submission failures do not trigger a legacy delivery path. Provider JSONL and audit logs remain local diagnostics (and producer context for sparse Junie hooks), not UI or daemon state sources. Both computers must run the OSC-style JSON integration. Hook adapters replace control characters in presentation text with spaces, replace invalid UTF-8 characters, and clip text at UTF-8 boundaries before submission. This applies to status messages/titles and context titles, display names, origins, tool labels, and source names. Session/agent IDs, status/permission IDs, state, and session-opening paths stay exact; invalid identifiers or paths still fail validation. Receiver and direct CLI JSON validation stay strict. Optional `context` outside the OSC status fields preserves provider identity, session/agent IDs, project paths, names, origins, tool detail, and source host for desktop presentation. A producer marks native session titles with `title_source: "session"`; other prompt-derived titles retain the first title. It is validated as bounded text and forwarded with desktop updates. Phone updates copy the session title into the OSC title field and omit the remaining desktop context. Phone status parsers need no provider-specific interpretation.

The desktop displays one row per source/session. Session-opening metadata is retained in daemon memory as normalized context. The UI and default status/watch commands query the running daemon over its socket. `sidepulse status` and `sidepulse live` read the same daemon view; neither accepts log-file inputs or reconstructs sessions offline. Hook notifications use a local `unless_state: "done"` guard; request receipts still make those no-op submissions retryable. A local `unless_kind` guard (`permission`, `question`, or `auth`) preserves an existing Blocked status of that kind. Codex producers use `unless_kind: "question"` for ordinary background events after a question is queued. Queuing an asynchronous question is Blocked/Question; an immediate `accepted: true` acknowledgement is not an answer. A user prompt, question-tool answer/rejection/failure, interrupt, or explicit turn/session ending replaces that wait. These guards are checked atomically at local submission, do not advance sequences or forward ignored updates, and are omitted from outgoing status messages. They are local producer accounting and do not create a second transport or display path.

See the [design plan](../design/osc7501-integration-plan.md) and [testing strategy and evidence](../testing/osc7501-testing.md).
