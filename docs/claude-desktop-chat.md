# Claude Desktop Chat status (experimental, macOS)

This experimental, opt-in monitor translates the regular **Claude Desktop Chat** app's accessibility states into SidePulse reports. It does **not** depend on Claude Code hooks.

## Current behavior

- `Stop response` accessibility button -> `working` (cyan)
- Active-session `awaiting answer` accessibility button **and** a large answer checkbox in the chat pane -> `blocked --kind permission` (SidePulse's waiting-for-input / amber mode)
- Transition from working or awaiting-answer to neither -> `done` (green), followed by `idle` after 5 seconds
- If Claude is closed -> `idle`

The amber classifier covers an observed answer-card workflow; it is **not** a general classifier for all possible Claude tool approval modals. Detection of `done` does not distinguish a successful response from user cancellation.

## Run manually

1. Install SidePulse and start its background service (`sidepulse service status`).
2. Enable Accessibility permission for the terminal app under System Settings > Privacy & Security > Accessibility.
3. Run `swift examples/claude_desktop_chat_monitor.swift` from the repository checkout. Keep the process running while using Claude Desktop.
4. Use `--approval-debug` to print **redacted** signals for the answer-card classifier. Use `--disable-amber` to disable experimental amber detection.

This monitor intentionally does not read conversation text, text-field values, window titles, or chat transcripts. It reads control roles and accessibility descriptions, plus checkbox geometry. Accessibility permission is powerful: review source and grant only to a trusted process.

## Known limitations

- Relies on Claude Desktop's current accessibility labels and structure; UI updates may break detection.
- Requires the monitor process itself to have macOS Accessibility permission.
- One instance should be running per Mac; concurrent instances can issue conflicting statuses.
- Restarting the SidePulse service can clear status until the next observed state change.
- App restart, minimized windows, and multi-window behavior need regression testing.
- This **does not implement auto-start or a 30-second idle-to-off policy**. A device-wide `sidepulse write off` could override another active agent, so inactivity-off should be implemented in the daemon's aggregate display policy, not this monitor.
- `done` is a best-effort interpretation of the end of generation, not a verified successful completion.

## Live hardware validation (November 11, 2026)

On a SidePulse Pro in macOS Claude Desktop Chat, the author observed a working -> awaiting answer -> working -> completed -> idle sequence. The SidePulse Pro physically displayed cyan, amber, cyan, green, and dim idle, respectively. This does not establish compatibility with every Claude Desktop version or permission dialog type.
