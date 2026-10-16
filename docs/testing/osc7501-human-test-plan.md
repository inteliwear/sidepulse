# OSC status integration: human acceptance test

Branch: `codex/osc7501-integration` in desktop, bridge, iOS, and Android. Record the actual build IDs and test date. This checklist validates the installed applications and real devices; automated test results alone do not check these paths.

Allow roughly 15 minutes for local smoke tests, 15–20 minutes per phone, and 20 minutes for remote/restart tests. The Done-expiry soak adds at least 21 minutes and can run alongside other tests on a separate scope/device.

## 1. Prepare the test environment

- [ ] Use the existing bridge or an isolated test instance. No OSC-specific bridge deployment is needed. Use only an isolated instance for bridge restart tests.
- [ ] Install branch builds of desktop/service and both phone apps. Check `sidepulse report --help` and `sidepulse service status`. Restart an already running daemon after updating its installation.
- [ ] Attach a SidePulse device to the receiving computer. Attach a device to each phone when testing that phone's output. Record the device model and firmware version.
- [ ] Pair each updated phone to the test bridge again. Older links do not receive automatic state or colors; manual LED pushes still work and are not evidence that status accounting works.
- [ ] Set devices to agent/status display rather than Battery or Custom. Pause other agent sessions or use a clean test installation so unrelated statuses do not win aggregation.
- [ ] Keep phone apps in the foreground for initial tests. Test background behavior separately.
- [ ] Save the table below and mark every case Pass, Fail, or Not tested. Do not record full pairing tokens or sender keys in screenshots or bug reports.

Use these test scopes only: `osc-human-main`, `osc-human-other`, and `osc-human-remote`. Clear a leftover test scope with:

```sh
sidepulse report --scope osc-human-main --state clear
sidepulse report --scope osc-human-other --state clear
```

**What to observe:** the desktop's named state/message and the connected device animation. Phone status messages may appear in the app's existing message area. Animations depend on settings and device model; Blocked and Error can currently share an animation. Verify their semantic distinction in the desktop state, not by color alone. There is no promise of a progress bar or a full record inspector in the phone UI.

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
| L8 | `sidepulse report --scope osc-human-main --end` | Done remains visible. Ending a scope does not erase its completion result. |
| L9 | `sidepulse report --scope osc-human-main --state clear` | The test record disappears and output returns to Idle, assuming no other active records. |
| L10 | Send Working, then `--end`. | Working is removed immediately; it must not turn into a false Done. |

An acknowledgement means saved locally. It does not prove remote delivery or a successful hardware write.

### Raw OSC CLI input

Use Bash or Zsh for the quoted sequence below:

```sh
sidepulse report --scope osc-human-main --osc $'\e]7501;state=blocked:kind=permission:msg=QXBwcm92ZSBkZXBsb3k/\e\\'
printf '\033]7501;state=working:app=human-test\007\n' | sidepulse report --scope osc-human-main --osc
sidepulse report --scope osc-human-main --state clear
```

- [ ] The first command displays Waiting for Input with `Approve deploy?`, decoded from base64.
- [ ] The piped command displays Working and removes the old message. Raw OSC and ordinary status flags update the same scope.
- [ ] The final command clears that scope. No duplicate legacy hook row appears.
- [ ] Repeat with the daemon stopped: the command acknowledges local persistence; restarting the daemon delivers the stored update.
- [ ] With a test remote link/phone configured, repeat these commands and verify the same resulting status on the receiver.
- [ ] Omitting the final ESC-backslash or BEL works for both an argument and stdin. Two concatenated sequences or mixed terminal output returns an error without changing the last accepted status.

## 3. Hierarchy and multiple sessions

Run:

```sh
sidepulse report --scope osc-human-main --state idle --app human-test
sidepulse report --scope osc-human-main --state working --id build/compile --title Compile
sidepulse report --scope osc-human-main --state blocked --id build/link --title Link --kind permission --msg 'Approve link?'
sidepulse report --scope osc-human-main --state working --id builder --title Unrelated
```

- [ ] Blocked wins even with an Idle parent and Working siblings.
- [ ] Children inherit the parent's application identity when they omit `app`.
- [ ] The desktop counts this as one active session, not three active sessions.

Then:

```sh
sidepulse report --scope osc-human-main --state clear --id build
```

- [ ] `build/compile` and `build/link` disappear.
- [ ] `builder` remains Working. Clearing `build` must not clear an unrelated prefix match.

Add a second scope:

```sh
sidepulse report --scope osc-human-other --state error --msg 'Other session failed'
sidepulse report --scope osc-human-main --state clear
```

- [ ] The other scope remains Error after the first scope is cleared.
- [ ] Clearing `osc-human-other` returns the device to Idle.

## 4. Retry, validation, and persistence

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
- [ ] Stop the test daemon using its normal service controls. Submit a new message. The CLI still acknowledges durable storage. Start the daemon again; the saved state is displayed and forwarded.
- [ ] Clear the scope, restart the daemon, and verify that the cleared Working state does not return.

Do not stop a shared production service to run these checks.

## 5. Actual Claude, Codex, and Cursor hooks

Run these separately for each installed provider. Record the provider version and hook installation used. These tests exercise hook normalization; native terminal OSC capture is not part of this branch.

- [ ] Start a new agent session and submit a task. SidePulse shows Working.
- [ ] Ask for a harmless operation that requires approval in the test environment. SidePulse shows Waiting for Input.
- [ ] If the provider permits concurrent tools, leave one approval pending while another tool starts/finishes. Waiting must remain until the pending approval is resolved.
- [ ] Repeat with identical concurrent tool arguments when the provider supplies distinct `tool_use_id` values. Resolving one invocation must leave the other blocked. If those IDs are absent, record the documented fallback limitation instead of claiming isolation.
- [ ] Approve the operation. SidePulse returns to Working while the agent continues answering, including after the tool itself finishes.
- [ ] Let the answer finish. Done appears, or Waiting for Input if the adapter recognizes a follow-up question.
- [ ] Interrupt a separate running task. It must not report successful completion merely because it was cancelled.
- [ ] Close/end the session normally. Live records disappear; completed/error results may remain according to the lifecycle policy.
- [ ] Confirm there is one logical session, without duplicate legacy and normalized entries. Verify the provider icon, title, project, origin, tool detail, and session resume action still work.
- [ ] Exercise a failed tool and a notification-driven request for input; compare the displayed state with the existing hook behavior.
- [ ] Test an older receiving desktop through the existing relay. It must still receive hook updates while the sender also publishes OSC snapshots.
- [ ] Use a non-default desktop agent animation on a capable phone. The selected animation must still play through the existing direct presentation path.
- [ ] Enable transcript recovery or include a legacy-only remote session. Its state must still contribute to phone output.

If a provider/version does not expose a particular lifecycle or approval hook, record that case as **Not supported by provider** rather than claiming a pass. Abruptly killing a process is not currently an automatic cleanup signal; explicit status can remain until the producer reports cleanup.

## 6. Remote computer → bridge → receiving desktop

Use `sidepulse link` on the receiving Mac and follow its displayed command on the remote test machine. Both desktops must use this branch; the bridge uses its existing message routes.

On the remote machine:

```sh
sidepulse report --scope osc-human-remote --state working --msg 'Remote build running'
```

- [ ] The receiving desktop and its device show the remote state. With a healthy bridge, allow roughly two five-second synchronization cycles; record observed latency.
- [ ] Start a separate local scope. Remote and local sessions coexist rather than replacing each other.
- [ ] Disconnect the receiving computer from the bridge. On the remote machine, update one scope to Blocked and another to Done. Clear one of them before reconnecting.
- [ ] Reconnect within queue retention, or keep the sender running for its next resend. The latest complete snapshot restores both scopes without resurrecting the clear.
- [ ] Restart only the test bridge. Existing queued snapshots survive when Redis is configured; a running sender also repairs missed delivery on its next resend.
- [ ] For a persistence check, restart the test Redis and bridge. Repeat recovery. Record the Redis persistence configuration.
- [ ] Remove/change the receiving link. Its previously received remote records are removed locally without deleting local records.

Disconnecting a receiver or losing the network must not manufacture a Done result.

## 7. Phone acceptance — repeat the entire section on iOS and Android

Keep the desktop's local test scope clean, and use the same commands from section 2 as the sender. For offline recovery cases, reconnect within the queue TTL or keep the sender daemon running until its next 60-second resend.

| Case | Action | Expected result |
| --- | --- | --- |
| P1 | Foreground app, device attached: send Working → Blocked → Done → Clear. | App/device follow the transitions. Messages remain readable. Clear returns to Idle when no other work exists. |
| P2 | Detach the device, send Blocked, then reattach. | The app accepts/accounts the update without hardware. Reconnection presents the retained Blocked state when device access is available. |
| P3 | While Working, play a manual pattern in the phone app. Trigger “update/recover from server” without publishing a new status. | The manual pattern stays. Duplicate recovery does not resume automatic output. |
| P4 | Now send a new Working report with a new message. | Automatic Working output resumes, even though Working was also the state before the manual pattern. |
| P5 | Play a manual pattern, then disconnect/reconnect the device without publishing new status. | Reconnection does not cancel the manual override. |
| P6 | Send two scopes. Put the phone offline, change both scopes and clear one, then reconnect and perform recovery. | The surviving scope is current and the cleared scope is absent. No stale task is resurrected. |
| P7 | Close/reopen the app and recover. | Accepted records and revision guards persist. Repeated recovery does not create duplicate records or replay Done. |
| P8 | Remove the sender key in the phone, then send another update using the old link. | No new state from that key is accepted. Its retained records are removed from accounting; other senders remain valid. |
| P9 | Background/lock the phone and publish updates; then foreground it and recover. | Record any background delivery and latency separately. Foreground recovery converges from an unexpired queued snapshot or the next resend while the sender runs. |
| P10 | Use an older/unmodified phone link in a separate test. | Manual direct LED control still works; automatic colors are never sent. The tray asks for an app update and renewed pairing. |
| P11 | On iOS, play a pattern through Shortcuts. On Android, use a notification automation if configured. Then recover an unchanged snapshot. | These successful direct writes also remain manual until a newer status is accepted. |
| P12 | Turn **Sync session states** off, change a session, then turn it back on. Repeat without changing any session after a manual push. | Off pauses state delivery; on sends the latest snapshot. An unchanged snapshot does not override the manual pattern. The setting survives restart and affects only that phone. |
| P13 | Disconnect the phone briefly, send a status, then reconnect and recover the existing push queue. | The app receives the latest queued snapshot within the queue TTL, or converges on the next resend while the sender runs. |
| P14 | Send several rapid state changes, then recover queued messages. | The latest received revision wins; older or duplicate messages cannot restore cleared work. |
| P16 | Change the Mac animation profile and Battery/Manual display choices while phone sync is enabled. | Session state still arrives; patterns are chosen on the phone. |
| P15 | Exceed the phone snapshot push budget while keeping desktop accounting within its limits. | The phone keeps its last received snapshot; no automatic colors are sent. Shrinking the snapshot resumes phone accounting. |

A simulator, an emulator, or a foreground-only pass does not verify real APNs/FCM delivery, background execution, USB/filesystem access, or physical LEDs. Mark unavailable paths **Not tested**.

## 8. Soak and cleanup

- [ ] Leave explicit Working active beyond the legacy idle timeout. Verify that it stays Working on every receiver that is running.
- [ ] Leave Done visible for more than 20 minutes, then foreground/recover each phone. It should age out of presentation; no new completion event should be manufactured.
- [ ] Leave a manual pattern active through a repeated snapshot refresh (the sender refreshes unchanged state every 60 seconds). It must not be overridden by the duplicate.
- [ ] End/clear every test scope on the machine that owns it. Verify recovery on desktop and both phones after cleanup.
- [ ] Restore normal device display settings and remove test links if they are no longer needed.

## Result sheet

| Case | Platform/build | Device/firmware | Pass / Fail / Not tested | Observed result and latency |
| --- | --- | --- | --- | --- |
| Local L1–L10 | | | | |
| Hierarchy / isolation | | | | |
| Retry / daemon restart | | | | |
| Claude hooks | | | | |
| Codex hooks | | | | |
| Remote recovery / Redis restart | | | | |
| iOS P1–P11 | | | | |
| Android P1–P11 | | | | |
| Soak / cleanup | | | | |

For a failure, capture the exact command or provider action, build IDs, previous state, observed state, app foreground/background state, device attachment state, and timestamp. Include a sanitized screenshot or log; omit channel capabilities, device push tokens, and sender keys.

**Acceptance:** convergence within existing queue retention or on a running sender's next resend; no resurrection from stale snapshots; no false Done; correct scope isolation and permission waits; no duplicate replay of output; manual overrides behave consistently; all required real-device paths either pass or remain explicitly unverified before release.

## Mobile Sessions screen

- Open Sessions from Home on iOS and Android. With no reports, verify the empty-state instructions.
- Send two sessions, then add a permission child to one. Verify one row per session, Needs approval above Working, the parent session title, agent and sender labels, message, and progress if provided.
- Disconnect the Dot and send another update. The list must update even though LEDs cannot be written.
- Relaunch the app. Sessions should restore immediately. Replay an older snapshot; cleared sessions must stay cleared.
- Play a manual pattern. Sessions remain visible independently of LED control.
- Remove a sender key. Only that sender's sessions disappear.
- Check a long title/message and large accessibility text. Open the full list to read messages truncated in Home's three-row summary.
