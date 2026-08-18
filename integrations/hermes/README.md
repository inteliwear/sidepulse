# SidePulse integration for Hermes Agent

This directory contains an installable [Hermes Agent](https://hermes-agent.nousresearch.com/) lifecycle plugin. It reports Hermes session, LLM, tool, approval, and error states to the local SidePulse status-bar socket.

## Privacy model

The plugin sends only lifecycle metadata needed to render status:

- event name and normalized SidePulse mode
- timestamp
- configured agent/profile label plus an opaque, session-scoped status suffix
- active Hermes profile name (never its filesystem path), used to resolve that profile's session lineage safely
- Hermes provider label
- durable Hermes session and turn identifiers
- client surface/origin label
- tool name, but never tool arguments or results

It does **not** send prompts, messages, conversation history, approval details, tool arguments, tool results, response bodies, or approval routing keys. `session_key` values are never promoted to durable session identifiers or written to the socket/log. Final-response text is inspected in memory only to distinguish `completed`, `waiting`, and `idle_ready`; the text is never added to the emitted event or debug log.

Approval hook `surface` values describe the approval mechanism rather than the originating client. The plugin therefore correlates approvals to an already-observed durable session and client surface in memory. Because Hermes may reload a directory plugin between lifecycle callbacks, a cache miss performs a bounded reverse lookup in the existing owner-only metadata log for the same durable session ID. Recovery reads at most the 1 MiB ending at the observed file size, accepts only normalized client-surface tokens, and rejects approval-only lifecycle records and approval-transport values; it adds no new persisted fields or files. If no safe correlation exists, the plugin reports the client surface as `unknown` instead of guessing.

The plugin registers no model-facing tools, so it adds no tool schema to model prompts.

## Prerequisites

1. Install SidePulse and run its setup flow:

   ```bash
   uv tool install git+https://github.com/inteliwear/sidepulse
   sidepulse setup
   ```

2. Confirm the SidePulse status-bar monitor is running and its local socket appears at:

   ```text
   ~/.local/state/sidepulse/agent-monitor/events.sock
   ```

## Install

Hermes supports plugins stored in a repository subdirectory:

```bash
hermes plugins install inteliwear/sidepulse/integrations/hermes --enable
```

For reproducible deployments, pin a reviewed SidePulse commit:

```bash
hermes plugins install inteliwear/sidepulse/integrations/hermes \
  --ref <40-character-commit-sha> \
  --enable
```

SidePulse declares `profile_scope: shared`. On Hermes versions that support shared
user plugins, the single root installation above is discovered by every local
profile, including agents launched from the Desktop **Bots** screen. Root
`plugins.enabled` remains the global consent gate; no per-profile plugin copy is
needed.

An individual profile may opt out and later rejoin without changing the root
installation:

```bash
hermes --profile homelab plugins disable hermes-sidepulse
hermes --profile homelab plugins enable hermes-sidepulse
```

`hermes --profile homelab plugins list --plain --no-bundled` reports the plugin
with source `shared`. Older Hermes releases that do not understand
`profile_scope` keep their existing profile-isolated behavior and require a
separate installation in each profile.

Restart each long-running Hermes runtime after installation or after adding
shared-plugin support. For Hermes Desktop, wait for active turns to finish and
restart only the affected profile backend when possible; a full application
restart is not intrinsically required.

## Configuration

The default label is the active Hermes profile name. To use a friendlier SidePulse label, configure the plugin under its manifest name. The plugin appends an opaque 12-hex session suffix internally so concurrent sessions from the same profile remain independent:

```yaml
plugins:
  entries:
    hermes-sidepulse:
      settings:
        agent_id: EDI
```

Optional settings:

| Setting | Default | Purpose |
|---|---:|---|
| `agent_id` | active profile name | SidePulse display-label prefix; status identity is session-scoped |
| `state_dir` | SidePulse XDG state path | Event-log directory override |
| `socket_path` | `<state_dir>/events.sock` | Local Unix socket override |
| `socket_timeout` | `0.2` | Best-effort socket timeout in seconds |
| `log_events` | `true` | Append privacy-safe metadata to owner-only `hermes.jsonl` and support cross-reload provenance recovery |

## Diagnose and test

```bash
hermes sidepulse doctor
hermes sidepulse doctor --json
hermes sidepulse test --mode working
hermes sidepulse test --mode ask
hermes sidepulse test --mode done
```

Available test modes are `idle`, `working`, `tool`, `ask`, `done`, and `error`.

The status-bar socket is intentionally best-effort. If the monitor is not running, Hermes continues normally and, when `log_events` is enabled, the plugin still writes the privacy-safe event log for diagnosis.

## Compatibility check after a Hermes update

Run this after upgrading Hermes, or whenever the LEDs look wrong. It is the
check that tells you whether SidePulse still matches the live Hermes plugin
API.

Preferred, if the plugin still loads:

```bash
hermes sidepulse compat
```

Standalone, even if the plugin failed to register after the upgrade:

```bash
python3 integrations/hermes/scripts/check_hermes_compat.py
```

What it checks:

1. Hermes is on `PATH` and reports a version.
2. The live Hermes `VALID_HOOKS` set still includes every hook this plugin
   needs (`on_session_start`, `on_session_activate`, `on_session_reset`,
   `pre_llm_call`, `pre_tool_call`, `post_tool_call`, `pre_approval_request`,
   `post_approval_response`, `api_request_error`, `post_llm_call`,
   `on_session_end`).
3. `hermes-sidepulse` is listed and enabled.
4. The SidePulse status-bar socket is up.
5. Synthetic Working, Ask, and Done events still emit and get logged.

Exit codes:

- `0` — contract intact. SidePulse should still work.
- `1` — something needs a fix before trusting the LEDs.

Useful flags:

```bash
hermes sidepulse compat --json
python3 integrations/hermes/scripts/check_hermes_compat.py --json
python3 integrations/hermes/scripts/check_hermes_compat.py --update-baseline
```

`--update-baseline` writes the current Hermes version and hook set to
`~/.local/state/sidepulse/agent-monitor/hermes-compat-baseline.json`. The next
run will also report version drift and any required hooks that disappeared.

If the check fails, use the recovery runbook and the custom-work checklist
rather than reinstalling blindly.

## Status LEDs

The plugin reports the modes in the main [SidePulse README](../../README.md#ai-agent-monitoring). Hermes does not emit Long Task Progress. Visually, SidePulse uses four LED patterns:

| Plugin mode | Typical Hermes hook | LED |
|---|---|---|
| `idle_ready` | session start / inactive activate | Dim idle pulse |
| `working` | `pre_llm_call`, successful `post_tool_call` | Cyan roll |
| `tool_running` | `pre_tool_call` | Cyan roll |
| `waiting_for_input` | `pre_approval_request`, `clarify`, a final `?` | Amber pulse |
| `blocked_error` | denied approval, API error | Amber pulse |
| `completed` | successful `post_llm_call` | Solid green for 12s, then idle |

The status-bar app then applies these display rules so the device stays glanceable:

- Completed is a 12-second green flash, then Idle / Ready.
- A `PermissionRequest` shorter than two seconds stays Working. Hermes fires that hook around auto-approved `terminal` / `execute_code` calls; amber is only for an approval that actually waits.
- Hermes `PostToolUseFailure` stays Working. A failed tool the agent continues past is not Blocked on the LEDs.
- Battery plug/unplug preview never overrides an active agent mode.

## Migrate from shell hooks

If Hermes was previously connected to SidePulse with custom shell hooks, disable those hooks after confirming the plugin works. Running both integrations duplicates events. Keep a backup until a live Hermes turn has produced the expected SidePulse state transitions.

## Development

From the SidePulse repository root:

```bash
python3 -m unittest discover -s tests -p 'test_hermes_plugin.py' -v
hermes plugins doctor
```

The unit tests cover lifecycle translation, routing-key privacy boundaries, approval provenance/failure mapping, cross-registration provenance recovery, concurrent-session identity, owner-only fallback logging, best-effort socket delivery, registration, diagnostics, and synthetic test events.
