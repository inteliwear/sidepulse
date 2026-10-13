# Grok Bot status integration investigation

Inspected the installed macOS Grok Bot 0.56.1 on 2026-09-19. This is a separate
product from the Grok CLI integration in `sidepulse.providers`.

## Findings

Full status monitoring appears feasible through the desktop app's private
authenticated APIs. This is established from the installed application's
bundled protocol definitions and client implementation, **not yet from a
successful live request**. These interfaces are not a documented public API and
may change between releases.

The app uses two transports:

1. A computer gateway with `POST /api/listAgents` and `GET /events` (SSE).
   Requests use the saved connection's headers and optional bearer token.
   SSE JSON envelopes contain `channel` and `payload`.
2. A backend Connect service, `aiserver.v1.GrokBotService`, with the
   server-streaming method `WatchGrokBotTranscripts`. The coordinator uses this
   source for newer `temporal` bots; implementing only the gateway would miss
   this distinction. Backend access is resolved using the signed-in account's
   authentication and request headers, separately from the gateway connection.

The backend stream includes:

| Frame / field | Integration use |
| --- | --- |
| `agentState.live` | Per-agent/session status updates |
| `agentState.snapshot` | Replace old state on a full snapshot |
| `isRunning`, `hasRunningSubagents` | Work in progress, including child work |
| `isComposingMessage`, `isRetrying` | Additional activity information |
| `activity.kind`, `activity.tool` | Thinking versus tool activity |
| `awaiting.reason`, `awaiting.sinceMs` | Requests for input |
| `updatedAtMs`, `staleAfterMs` | Expire stale work indicators |
| `turnFailed` | Explicit error, turn/session identity, and failure time |
| `rosterChanged` | Refresh bot discovery |
| `connected`, `heartbeat` | Stream lifecycle |

An assistant message alone does not mean the bot finished. Likewise, a dropped
connection or expired running status must not produce a completion signal.
Completion semantics still need to be verified with real transitions; the
presence of `isRunning=false` alone does not distinguish success, cancellation,
and inactivity.

## Local files and authentication

- App: `/Applications/Grok Bot.app`, bundle ID `com.anysphere.sand`.
- Active account scope: `~/.grokbot/settings.json`.
- Gateway descriptor: `~/Library/Application Support/Grok Bot/gateway-descriptor.json`.
  Version 2 contains account-scoped, timestamped encrypted connections. The app
  rejects cached entries older than seven days.
- Local roster: `sand-client-persistence` under that same Application Support
  directory. Base32 filenames identify account-scoped persistence slices.
  The roster envelope has schema version 4.
- Roster persistence deliberately excludes live running/composing fields. It
  must not serve as an authoritative activity source.
- The gateway descriptor uses Electron safeStorage (`v10` on this installation),
  backed by the macOS Keychain item `Grok Bot Safe Storage`.

A noninteractive, read-only Keychain check returned `errSecAuthFailed` (-25293).
No credential was obtained, decrypted, printed, or saved, and no authenticated
network request was made. A subsequent normal Keychain request also failed
(exit 128). The user then explicitly restricted this investigation to local-only
operation with no login. Do not retry credential access or cloud requests under
that scope. Do not modify the app, bypass its Keychain ACL, or use debug injection
as a workaround.

## Local-only investigation

The coordinator receives three private Electron message ports from its parent:
control, renderer data, and main-process data. The Node fallback likewise uses
inherited parent/child IPC. These are not named sockets that an independent
SidePulse process can subscribe to. The installed coordinator has no listener.
The running app exposed no TCP listening port; the Unix socket found was the
Chromium SingletonSocket, which is not a status feed. The app's development
control server is explicitly excluded from packaged builds.

There is a promising local alternative: the rendered bot-list accessibility
labels include working and attention markers. In the renderer, `eoe` constructs
the accessible name from the bot name, optional main-bot marker, blocked/unread
marker, and `isWorking`. `toe` installs that name as the bot-row `aria-label`.
Do not treat `aria-busy` alone as work status: the row assigns it from a separate
loading flag.

The macOS accessibility tree was readable and exposed the two currently visible
bot rows. Both were inactive during observation, so working/attention labels
are supported by source inspection but still need live verification. No test
prompt was sent. An accessibility adapter could observe the already-running app
without reading credentials or making network requests. It would require macOS
Accessibility access, must accommodate localization and duplicate bot names,
and cannot assume a missing/hidden row means completion. A transition to inactive
also cannot by itself distinguish completion from cancellation or failure.

## Next validation

Under the current local-only scope, validate accessibility labels during naturally
occurring bot work and input requests, without sending prompts or logging
transcripts. Test hidden/minimized windows, sidebar virtualization, localization,
and app shutdown. Do not implement cloud authentication as a fallback.

Observe a real run through working, tool activity, awaiting input, successful
completion, cancellation, and failure. Test reconnection, expired credentials,
account changes, stale state, and concurrent bots before wiring a separate
`grokbot` provider into the live monitor. The existing `grok` CLI hook installer
must remain separate.

## Evidence locations

The installed `app.asar` contains:

- `dist/electron-main/main-app.cjs`: gateway descriptor storage,
  `resolveServerTranscriptTail`, and account authentication resolution.
- `dist/node-agent-coordinator/main.cjs`: gateway HTTP/SSE transport,
  backend transcript stream client, and live-state reducer.
- `dist/electron-main/proto.cjs`: Grok Bot service and frame schemas.
- `dist/renderer/assets/index-*.js`: durable roster serialization, which omits
  running state.

No vendor bundles or user data are checked into this directory.
