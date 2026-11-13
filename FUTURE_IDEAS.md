# Future ideas

## Closed-lid power management

Ideas from the 2026-10-07 review of [Stowaway](https://github.com/SihanCheng0/stowaway), commit `cb7bdb8081511a290587d2a34ae6f3cceaa5f129`. These are proposals, not implemented changes. The review inspected source; Stowaway was not run locally.

### Thermal safeguard

- Monitor `ProcessInfo.thermalState` and its change notification alongside SidePulse's existing battery safeguard.
- Release sleep prevention when macOS reports `.serious` or `.critical` thermal pressure. These are OS thermal classifications, not temperature readings.
- Decide explicitly when a cooled-down Mac may resume sleep prevention, so active agents do not immediately re-enable an override that a safeguard just released.

### Verify power-setting changes

- Read back `SleepDisabled` after changing `pmset -a disablesleep` before reporting success.
- Represent failed or unavailable reads as unknown, rather than assuming normal sleep has been restored.
- Keep failed restoration visible and retry with bounded backoff. Stowaway retries automatic restoration after 30 seconds.

### Crash recovery and ownership

- Persist enough information to identify a sleep override applied by SidePulse and recover an interrupted session on launch.
- Preserve a pre-existing override owned by the user or another application; account for the fact that `disablesleep` is a shared system setting.
- Consider an independent watchdog or expiring lease that restores normal sleep if SidePulse crashes or hangs. Startup recovery alone leaves the override enabled until the app runs again, with its battery and thermal monitoring stopped.
- Handle interruption between applying the setting and recording ownership; a simple flag written afterward leaves a recovery gap.

### Optional session limit and clear stop reasons

- Consider an optional maximum duration for manual or closed-lid awake sessions, with a visible countdown and an explicit extension action.
- Show and log why sleep prevention ended: work completed, timer expired, low battery, thermal pressure, recovery, or an error.

### Evaluate immediate sleep after an automatic stop

- Stowaway restores `disablesleep 0`, then calls `pmset sleepnow` when a timer, battery, or thermal safeguard ends a session with the lid closed and no external display active.
- Evaluate whether SidePulse needs this additional step to ensure a closed Mac sleeps promptly after a safeguard fires.
- Require fresh, known lid and display state, release SidePulse's sleep assertions first, and verify the sleep request result. Avoid applying this automatically to a manual stop or an attached-display workflow.
- `sleepnow` sleeps the whole Mac; it is different from `displaysleepnow`.

### Preserve explicit display sleep

- User-observed behavior: `pmset -a disablesleep 1` leaves the display on. Do not assume the sleep override also handles display shutoff.
- Stowaway has no explicit dimming, brightness adjustment, or `pmset displaysleepnow` call. Its missing display-off step is an issue to investigate for its closed-lid use case; the app itself has not been tested here.
- SidePulse already explicitly requests `pmset displaysleepnow` once the lid is confirmed closed, the sleep override is active, and no external display is active. Preserve this behavior.
- The request affects displays globally, so continue suppressing it when an external display is active or display detection is unknown. Let macOS handle normal display wake-up.
- Keep the existing prohibition on private framebuffer power calls; see [closed-lid power design](docs/design/closed-lid-power.md).

### Possible upstream feedback

- Report the missing explicit display-sleep step, separating the user's observed `pmset` behavior from the source-only Stowaway review.
- Report that crash recovery requires relaunch, leaving the global sleep override active without app safeguards in the meantime.
- Report that Stowaway treats an external-display query failure as “no external display,” which can cause an automatic stop to request whole-system sleep despite an attached monitor.

Source references: [power commands](https://github.com/SihanCheng0/stowaway/blob/cb7bdb8081511a290587d2a34ae6f3cceaa5f129/Stowaway/Services/PowerManager.swift), [session lifecycle and recovery](https://github.com/SihanCheng0/stowaway/blob/cb7bdb8081511a290587d2a34ae6f3cceaa5f129/Stowaway/Models/AwakeController.swift), [safety policy](https://github.com/SihanCheng0/stowaway/blob/cb7bdb8081511a290587d2a34ae6f3cceaa5f129/Stowaway/Models/SafetyPolicy.swift), and [system monitoring](https://github.com/SihanCheng0/stowaway/blob/cb7bdb8081511a290587d2a34ae6f3cceaa5f129/Stowaway/Services/SystemMonitor.swift).
