# OSC status integration: human acceptance test

Branch: `codex/osc7501-integration` in desktop, bridge, iOS, and Android. Record the actual build IDs and test date. This checklist validates the installed applications and real devices; automated test results alone do not check these paths.

Allow roughly 15 minutes for local smoke tests, 15–20 minutes per phone, and 20 minutes for remote/restart tests. The Done-expiry soak adds at least 21 minutes and can run alongside other tests on a separate scope/device.

## 1. Prepare the test environment

- [ ] Use an isolated local build of `../sidepulse-bridge` for bridge tests. Do not deploy or change production to run this checklist. Test desktops and phones must point to the chosen local test instance.
- [ ] Install branch builds of desktop/service and both phone apps. Check `sidepulse report --help` and `sidepulse service status`. Restart an already running daemon after updating its installation.
- [ ] Attach a SidePulse device to the receiving computer. Attach a device to each phone when testing that phone's output. Record the device model and firmware version.
- [ ] Pair each updated phone to the test bridge again. Older links do not receive automatic state or colors; manual LED pushes still work and are not evidence that status accounting works.
- [ ] Set devices to agent/status display rather than Battery or Custom. Pause other agent sessions or use a clean test installation so unrelated statuses do not win aggregation.
- [ ] Keep phone apps in the foreground for initial tests. Test background behavior separately.
- [ ] Save the table below and mark every case Pass, Fail, or Not tested. Do not record full pairing tokens or sender keys in screenshots or bug reports.

Use the `osc-human-` prefix for test scopes: `osc-human-main`, `osc-human-other`, and `osc-human-remote` below, plus uniquely numbered scopes for capacity checks. Clear only those test scopes, for example:

```sh
sidepulse report --scope osc-human-main --state clear
sidepulse report --scope osc-human-other --state clear
```

**What to observe:** the desktop's named state/message and the connected device animation. Both phone apps show a Sessions summary and a full session list, with status, message, and reported progress. Animations depend on settings and device model; Blocked and Error can currently share an animation. Verify their semantic distinction in the desktop state, not by color alone. Use the full Sessions list to inspect text truncated in the Home summary.

## 2. Local third-party sender — required smoke test

Run one command at a time, allowing the desktop to refresh.

| Step | Action | Expected result |
| --- | --- | --- |
| L1 | `sidepulse report --scope osc-human-main --state working --app human-test --title Build --msg 'Building project' --progress 10` | An acknowledgement prints. Desktop shows Working and the message; the device uses its Working animation. |
| L2 | Leave it quiet longer than the configured legacy idle timeout. | It stays Working. It does not become Done or disappear just because there are no new reports. |
| L3 | `sidepulse report --scope osc-human-main --state blocked --kind permission --msg 'Approve release?'` | Waiting for Input/Ask wins. The message changes. This is not classified as Error. |
| L4 | Repeat L3 with `--kind question`, then `--kind auth`, changing the message each time. | Both are accepted as blocked states. The app remains waiting; messages update even while the state name stays the same. A dedicated kind label is not required in the current UI. |
| L5 | `sidepulse report --scope osc-human-main --state working` | Working returns. The earlier approval message and title must not remain attached to this record. |
| L6 | `sidepulse report --scope osc-human-main --state error --msg 'Build failed'` | Error appears. |
| L7 | `sidepulse report --scope osc-human-main --state done --msg 'Build complete'` | Done/Completed appears; it is not still Error. |
| L8 | `sidepulse report --scope osc-human-main --state clear` | The test record disappears and output returns to Idle, assuming no other active records. |
| L9 | Send Working, then `--state clear`. | Working is removed immediately; it must not turn into a false Done. |
| L10 | Send Done, then choose **Acknowledge Done** beneath that tray session. | The session remains Idle in the tray, `sidepulse status`, and `sidepulse live`. With no other active session, LEDs select Idle. A newer Working or Done report replaces it. Other receivers remain unchanged. |

A report submission acknowledgement means accepted into the running daemon’s memory. It does not prove remote delivery or a successful hardware write. **Acknowledge Done** is the separate user action that changes a completed session to Idle.

### Raw OSC CLI input

Use Bash or Zsh for the quoted sequence below:

```sh
sidepulse report --scope osc-human-main --osc $'\e]7501;state=blocked:kind=permission:msg=QXBwcm92ZSBkZXBsb3k/\e\\'
printf '\033]7501;state=working:app=human-test\007\n' | sidepulse report --scope osc-human-main --osc
sidepulse report --scope osc-human-main --state clear
```

- [ ] The first command displays Waiting for Input with `Approve deploy?`, decoded from base64.
- [ ] The piped command displays Working and removes the old message. Raw OSC and ordinary status flags update the same scope.
- [ ] The final command clears that scope. The UI shows only accounted JSON state.
- [ ] Repeat with the daemon stopped: the command reports that the daemon is unavailable. It must not create a session database or local state fallback.
- [ ] With a test remote link/phone configured, repeat these commands and verify the same resulting status on the receiver.
- [ ] Omitting the final ESC-backslash or BEL works for both an argument and stdin. Two concatenated sequences or mixed terminal output returns an error without changing the last accepted status.

## 3. One state per session and multiple sessions

Run:

```sh
sidepulse report --scope osc-human-main --state idle --app human-test
sidepulse report --scope osc-human-main --state working --id build/compile --title Compile
sidepulse report --scope osc-human-main --state blocked --id build/link --title Link --kind permission --msg 'Approve link?'
sidepulse report --scope osc-human-main --state working --id builder --title Unrelated
```

- [ ] Each command replaces the same session. After the last command, one Working row titled Unrelated is visible.
- [ ] Status IDs do not create additional rows or retain old permission requests.

Then:

```sh
sidepulse report --scope osc-human-main --state clear --id build
```

- [ ] The entire main session disappears. A clear removes its session regardless of its status ID.

Add a second scope:

```sh
sidepulse report --scope osc-human-other --state error --msg 'Other session failed'
sidepulse report --scope osc-human-main --state clear
```

- [ ] The other scope remains Error after the first scope is cleared.
- [ ] Clearing `osc-human-other` returns the device to Idle.

## 4. Retry, validation, and daemon lifetime

Create a unique request ID once, then run the same report twice:

```sh
OSC_RETRY_ID="osc-human-retry-$(date +%s)"
sidepulse report --scope osc-human-main --state working --request-id "$OSC_RETRY_ID"
sidepulse report --scope osc-human-main --state working --request-id "$OSC_RETRY_ID"
```

- [ ] Both acknowledgements have the same `seq` and `revision`. The second call does not create another session or restart its animation.
- [ ] Reusing that request ID with `--state done` fails and leaves the existing state unchanged.
- [ ] `sidepulse report --scope osc-human-main --state ask` is rejected.
- [ ] `sidepulse report --scope osc-human-main --state working --id 'a//b'` is rejected. The existing record remains intact.
- [ ] A Unicode message such as `Build café ✓` survives unchanged.
- [ ] Quit/reopen the desktop UI. The current explicit state survives.
- [ ] Stop the test daemon using its normal service controls. Submit a new message. The CLI reports the unavailable daemon. Start it again: its sessions and pending queue start empty; submit a new update and verify it appears.
- [ ] Restart the daemon and verify that it starts with no session state. Closing/reopening only the UI must preserve the running daemon state.

Do not stop a shared production service to run these checks.

## 5. Actual provider hooks: Claude, Codex, Cursor, Grok, and Junie

Run these separately for each installed provider. Record the provider version and hook installation used. These tests exercise hook normalization; native terminal OSC capture is not part of this branch.

- [ ] Start a new agent session and submit a task. SidePulse shows Working.
- [ ] Ask for a harmless operation that requires approval in the test environment. SidePulse shows Waiting for Input.
- [ ] If another hook reports Working while an approval is pending, Working replaces it. There is one current state and no separate pending-approval accounting.
- [ ] Approve the operation. SidePulse returns to Working while the agent continues answering, including after the tool itself finishes.
- [ ] Let the answer finish. Done appears, or Waiting for Input if the adapter recognizes a follow-up question.
- [ ] Interrupt a separate running task. It must not report successful completion merely because it was cancelled.
- [ ] Close/end a session in a provider that emits SessionEnd. The hook reports Done; it is visible for 20 minutes, unless replaced or explicitly cleared. Providers without that hook do not receive invented completion events.
- [ ] Confirm there is one logical session, with one current state from JSON. Verify the provider icon, title, project, origin, tool detail, and session resume action still work.
- [ ] Exercise a failed tool and a notification-driven request for input; compare the displayed state with the existing hook behavior.
- [ ] Verify the bridge receives individual JSON updates only. An old raw `agent_event` envelope must not create a session. Both desktops require the updated protocol.
- [ ] Change a desktop animation and verify the phone still chooses its own session pattern.
- [ ] Add a raw provider log/transcript event without submitting JSON. It must not change daemon, UI, or phone session state.
- [ ] Stop the daemon and run a hook. The provider must continue normally, but no fallback state is written. Restart the daemon and run a fresh hook; it is accounted and forwarded.
- [ ] Leave a UI client connection stalled while sending a hook. Other bounded workers must accept the hook promptly.

If a provider/version does not expose a particular lifecycle or approval hook, record that case as **Not supported by provider** rather than claiming a pass. Abruptly killing a process is not currently an automatic cleanup signal; explicit status can remain until the producer reports cleanup.

## 6. Remote computer → bridge → receiving desktop

Use a local bridge test instance and configure both test desktops to use it. Both desktops and the bridge must use the updated branch; keep these tests separate from production links.

- [ ] Send Working, Blocked, Clear, and Done updates with `sidepulse report`. Verify each arrives individually and updates the receiving UI. The forwarding worker checks every second; record observed latency.
- [ ] Use the same scope name locally and remotely. Both rows remain distinct and the active count matches the number of working/blocked/error rows. Clearing the remote scope preserves the local row and updates the active count.
- [ ] Disconnect a receiver and update 12 different sessions. On reconnect, expect only the newest 10 sessions. Repeated updates to one session must occupy only one queue item. Do not expect a complete-state snapshot to repair missing events.
- [ ] Stop and restart the sender daemon with pending events. Its in-memory queue starts empty; it forwards only new incoming updates. Closing/reopening the UI alone must leave the daemon queue intact.
- [ ] Remove/change the receiving link. Its remote records clear locally and clear updates are queued for phones; local records remain.

## 7. Phone acceptance — repeat on iOS and Android

- [ ] Pair the updated app, advertising `status_protocol: 2`. In the tray, enable **Forward sessions**.
- [ ] Send individual updates from two paired computers. Use identical `publisher` and `scope` strings. Both sessions must appear independently. Clear one; the other stays visible.
- [ ] From a single sender, use two publishers with the same scope. They remain separate too.
- [ ] Send a higher sequence followed by an older one. The older event must not replace or revive state. Repeat after clearing and after restarting the app.
- [ ] Send a newer event for one session followed by a lower sequence for another session. Both are accepted because ordering is tracked per session.
- [ ] Use a long Unicode message containing quotes/backslashes. Its `msg` is clipped to at most 1,024 UTF-8 bytes before sending, and shortened further if needed to fit one push; state, IDs, and lifecycle remain intact. Many existing sessions must not stop new updates.
- [ ] Try an unknown outer sender key and a valid key placed only inside `data`. Neither authorizes an update. Revoke one paired sender and verify only that sender's records disappear.
- [ ] Pause syncing, send changes, then resume. Retained pending events arrive; unchanged state is not periodically republished.
- [ ] Select a manual pattern, then replay a duplicate update. The duplicate must not override the pattern. A newly accepted status may resume the phone's own session rendering.
- [ ] Change Mac patterns and Battery/Manual modes. Session forwarding continues and the phone's pattern choice is unchanged.
- [ ] Disconnect the phone and send multiple events. Recovery returns up to 10 queued items, with only the newest state per source/session. Verify clear replaces an older Working update, queued messages are at most 2048 bytes, and items expire within 300 seconds. Record missed events honestly; no full-state reconstruction is promised.
- [ ] Upgrade from an earlier saved-cache format: old sessions and replay guards start fresh; new individual updates populate the list. Current-format sessions, dismissals, and replay guards survive relaunch. An old `program_status_snapshot` or removed control message must be rejected without becoming a direct LED write.

A simulator/emulator does not establish physical LED behavior or background APNs/FCM reliability. Record those separately.

## 8. Soak and cleanup

- [ ] Run several providers concurrently for an hour. Track phone arrival latency, dropped events, and source/session separation.
- [ ] Fill 64 sessions across multiple sources, update the oldest, then add a new session. Verify the updated session and new session remain, the least recently updated session disappears, and the list stays at 64.
- [ ] Reopen the UI without restarting the daemon and repeat. A rejected stale/duplicate message must not refresh recency; a genuinely newer update for an evicted session must bring it back. All accepted updates are forwarded individually, including when the sender evicts an older session.
- [ ] Clear test scopes and remove test sender keys. Do not clear unrelated sessions.

## Result sheet

| Case | Platform/build | Device/firmware | Pass / Fail / Not tested | Observed result and latency |
| --- | --- | --- | --- | --- |
| Local L1–L9 | | | | |
| Single-state replacement / source isolation | | | | |
| Retry / daemon restart | | | | |
| Claude hooks | | | | |
| Codex hooks | | | | |
| Cursor hooks | | | | |
| Grok hooks | | | | |
| Junie hooks | | | | |
| Remote recovery / expiry / process restart | | | | |
| iOS phone acceptance | | | | |
| Android phone acceptance | | | | |
| Soak / cleanup | | | | |

For a failure, capture the exact command or provider action, build IDs, previous state, observed state, app foreground/background state, device attachment state, and timestamp. Include a sanitized screenshot or log; omit channel capabilities, device push tokens, and sender keys.

**Acceptance:** individual updates arrive within normal bridge latency; source/session isolation and replay guards hold; a large ledger does not block new updates; messages are clipped safely; no automatic desktop LED commands target phones. Offline delivery remains limited to the existing queue guarantees.

## Mobile Sessions screen

- Open Sessions from Home on iOS and Android. With no reports, verify the empty-state instructions.
- Send two sessions, then change one to Blocked with kind permission. Verify one row per session, Needs approval above Working, the session title, agent and sender labels, message, and progress if provided.
- Disconnect the Dot and send another update. The list must update even though LEDs cannot be written.
- Relaunch the app. Sessions should restore immediately. Replay an older update; cleared sessions must stay cleared.
- Play a manual pattern. Sessions remain visible independently of LED control.
- Remove a sender key. Only that sender's sessions disappear.
- Check a long title/message and large accessibility text. Open the full list to read messages truncated in Home's three-row summary.


## Daemon-owned state and LED output

- [ ] Quit the UI while a session is Working. The daemon continues driving physical LEDs and forwarding individual phone updates.
- [ ] Reopen the UI. It obtains the same session from the daemon without reading a database or provider logs.
- [ ] Change brightness/display settings and verify the daemon applies them. A UI preview or lid animation must not be overwritten by the automatic loop during its hold.
- [ ] Quit only the tray while a session is working. Session LEDs, forwarding, sleep prevention, low-battery safeguards, and history sampling should continue. Reopen the tray and verify it shows the existing daemon state without restarting a preview or duplicating history records.
- [ ] With the tray closed, test lid close/open animations and plug/unplug battery previews. Each should restore normal output after its configured hold; a sleep-allowed closed lid should keep its final frame until the lid or policy changes.
- [ ] Stop the daemon and verify its sleep-prevention assertion is released. Restart it and verify the saved sleep policy applies again.
- [ ] Disable and re-enable LED output from the UI. Session state remains intact and phone session forwarding continues.
- [ ] Restart the daemon. Its local session list and pending updates start empty. Phone apps retain their last received state until subsequent updates; startup does not send an aggregate reset or snapshot.
