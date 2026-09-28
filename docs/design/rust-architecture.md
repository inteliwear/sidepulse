# Rust architecture and CLI compatibility

This document tracks the migration while the Python release remains operational.
The Rust implementation must not replace installed hooks or launch agents until
the corresponding behavior has passed parity checks on each target platform.

## Process boundaries

```text
agent hooks -> sidepulse service <- CLI
                         ^
                         |
                    tray / settings UI
                         |
                  platform UI adapter
```

The service owns event ingestion, agent state, persistence, settings, device
selection and output, relay, battery policy, and sleep policy. The UI owns only
presentation, input, and platform-specific windows. The CLI requests service
actions for live operations and may inspect files directly only for offline
diagnostics and installation. The hook path must remain short and best effort.

The protocol uses versioned newline-delimited JSON request, response, and
subscription messages, with a 1 MiB message limit. A local transport adapter
will map Unix sockets and Windows named pipes while retaining the same message
format. Authentication and single-instance ownership are transport concerns.
The service is the sole writer of `latest.json` and the sole active LED writer.
Both the CLI and UI must reconnect and request a full snapshot after a service
restart. AppKit/Win32/Linux tray implementations are adapters over the same
view model. macOS notch rendering and Disk Arbitration have platform-specific
implementations; the absence of these features elsewhere is explicit.

## CLI compatibility contract

The existing `sidepulse` commands are `version`, `update`, `agent-monitor`,
`setup`, `write`, `push`, `link`, `service`, `status-bar`, `settings`,
`sdejectguard`, `battery`, and internal `hook-log`. The standalone
`agent-monitor` executable supports `version`, `doctor`, `status`, `live`,
`watch`, `leds`, `status-bar`, `install`, `uninstall`, and `hook-log`.
`agent-status-bar` and `sidepulse-reply` are additional entry points.

During migration:

- Preserve command names, aliases, flags, exit codes, and documented JSON
  fields. Add new commands only where needed for the service protocol.
- Keep `sidepulse hook-log` and `agent-monitor hook-log` as compatibility paths
  for previously installed provider configs. New configs can invoke a dedicated
  Rust hook executable.
- Keep hook config paths, state paths, settings JSON, provider JSONL, audit JSONL,
  link and relay JSON compatible. Migrate schemas only with a versioned reader.
- Route operating-system operations through platform adapters. Unsupported
  features should return a clear capability error, not disappear from help.
- Keep the current Python distribution available until the native installer can
  upgrade hooks, services, and user data and can roll back on failure.

## Delivery gates

1. Core event normalization and status transitions match captured Python cases.
2. Hook installation and event delivery work with all five providers.
3. One service owns output with and without the tray UI running.
4. CLI and UI observe the same state and can reconnect after a crash.
5. macOS, Linux, and Windows builds pass CLI and service smoke tests; platform
   UI and device capabilities are exercised where available.
6. Installation upgrades an existing Python setup without losing settings,
   links, logs, or hooks. The released runtime invokes no Python code.

The optional reply classifier and maintained examples are part of the final
Rust migration. Their model behavior requires separate parity evaluation.

## Settings window implementation

The cross-platform settings window uses Rust `egui`/`eframe` as a separate
client executable. It reads the same shared presentation model as the tray
and sends versioned service requests for every setting change. A background
client worker performs IPC so reconnects and saves cannot block drawing. The
service remains the owner of settings validation, persistence, monitoring,
power policy, and device output. Native tray clients and the CLI open the
sibling settings executable with their existing endpoint. The window is
included in preview bundles but does not register itself for startup.
The first window covers live activity, device selection, brightness, agent /
battery / manual display, charger baseline and power-change previews,
transcript monitoring, and macOS awake preferences. macOS staging now includes
native `.app` bundles for tray and settings, with the isolated endpoint in
their resources. Local window checks exercised saving, preserving manual LED
output, visible external-edit conflicts, retained edits, and reconnecting
after a service restart. The animation page saves built-in presets and per-status custom LED programs
through the service, validates 2- and 8-LED output before saving, and keeps the
working/tool/progress modes grouped as in the Python application. Existing
named custom assets remain selectable and are preserved. Named asset and
profile editing, session-opening preferences, history, relay controls, virtual
display, and broader sleep controls still need implementation.
The service now applies saved idle timeouts to snapshots immediately, without
rebuilding its monitor or losing pending permissions. The shared presentation
model includes only completed stale sessions within the configured recent
retention, matching Python menu rules. The window can save these durations and
the macOS battery sleep safeguard through validated requests; the development
CLI exposes `service-agent-list` and `service-sleep-safeguard` for the same
operations. Sleep controls still report an unsupported-platform error on
Windows and Linux.

## Current migration state

The Rust workspace currently contains the portable event/status core, a
versioned protocol, local IPC, a development service, a development CLI, and
the `sidepulse-next-hook` hook executable. The hook handles Cursor names,
Junie's missing context, Grok routing, provider JSONL, legacy audit JSONL, and
best-effort service delivery. The service can replay provider logs at startup
when launched with `--log PROVIDER PATH` pairs. Hook and service are separate
from the UI. Unix IPC startup can reclaim an abandoned socket after checking
that it is a socket with no listener; active sockets and regular files remain.
The `sidepulse-ui-model` crate turns service snapshots into common tray state
without owning monitoring or output. A `sidepulse-next-tray` client
now renders the mode, active agents, and a quit action. It polls and reconnects
to the service; its UI event loop is native on macOS and Windows and uses the
KSNI tray backend on Linux. The development tray compiled and stayed running
during a macOS smoke test with a temporary Rust service and hook event.
The shared UI model now interprets one service settings response into portable
tray controls; platform event loops no longer issue separate requests for each
control. A disconnect also resets the visible status title through that model.
Rust formatting, linting, and workspace tests run in CI on macOS, Linux, and
Windows; the workflow also stages and smoke-tests the isolated service on each
OS, then retains portable
preview binaries. A downloaded artifact can be staged at its destination with
`sidepulse-next-stage` so generated startup files contain the right absolute
paths. These are development artifacts, not signed release packages. Visual behavior
on macOS and runtime UI and device behavior on Linux
and Windows still need validation. These executables are not installed by the
existing setup flow.

The `sidepulse-installer` crate can assemble those binaries into a new,
isolated preview directory with a settings file, state directory, manifest,
and platform launch definitions. macOS gets LaunchAgent plists, Linux gets
systemd user units, and Windows gets PowerShell launch scripts. The
`sidepulse-next-stage` command supports a dry run and refuses to overwrite an
existing preview. It does not register startup jobs, install hooks, use the
existing settings file, read provider logs from the user's normal home, or
enable physical device output. All five preview log paths remain inside the
stage directory. This is a staging
step for native installation and rollback testing, not a production cutover.
Its `--smoke-stage` check starts and stops the staged service and verifies
snapshot/settings IPC with no selected device.

The `sidepulse-device` crate now owns the default LED programs, program
validation, candidate discovery, and synced writes. An explicitly configured
development service can write one device with `--device PATH` and optional
`--brightness 0-255`. The service polls its own state and skips unchanged
programs; the tray never touches the device file. A macOS smoke test passed
from the Rust hook through the service into `LEDS.LED` in a temporary folder.
Physical hardware behavior still needs validation. Automatic selection and
saved animation styles are covered below; profile editing remains UI work.

The development CLI now shares one fail-open hook handler between
`sidepulse-next hook-log`, `sidepulse-next agent-monitor hook-log`, and
`sidepulse-next-hook`. It also supports `status` and `agent-monitor status` as
one-shot offline views of the Rust monitor, with the existing status flags and
JSON field names. Status reads the last requested number of lines per provider
and accepts explicit log paths. It reads configured hook commands for custom
log paths in the five provider configuration files, then falls back to the
default state directory. The monitor retains project and prompt titles across
hook events and reads Codex session titles from its local index. An isolated
Python/Rust status comparison matched both provider display names and modes.
Seven additional marker, message-precedence, question, and notification cases
also match Python's status rules.
Default source selection follows Python by including Cursor only when its log
is explicitly requested.
The `sidepulse-sources` crate shares source selection,
bounded replay, and appended-row recovery between CLI and service. The service
replays those sources at startup and tails new complete rows so events written
while its socket is unavailable can still reach the monitor. Optional Codex
and Claude transcript sources can now be supplied to one-shot status with
`--codex-transcripts DIR` or `--claude-transcripts DIR`, and to the service
with `--transcript codex|claude DIR`. They replay recent files and detect later
file changes; they remain opt-in as in the Python defaults. The Rust reader
needs comparison against a larger set of captured transcript cases. Provider
JSONL replay now seeks backward for the last requested lines instead of
scanning entire historical logs.
When an explicit settings document enables Codex or Claude transcript
monitoring, the service discovers their default transcript directories;
explicit `--transcript` paths still take precedence. The service now applies
transcript setting changes while running and preserves JSONL recovery cursors.
The CLI can change those settings with `service-transcript`, and the tray has
Codex and Claude transcript toggles backed by service IPC.

The development service can load and atomically update the legacy
`latest.json` status schema when launched with `--state PATH`. State output is
opt-in during migration so a trial Rust service cannot overwrite the active
Python application's state file. The released installer must hand ownership
of that file to the Rust service during cutover.

The service can also load a legacy settings document with explicit
`--settings PATH`. It preserves unknown fields and rejects a write if another
process changed the document after startup. A device started with `--device`
uses its saved brightness unless `--brightness` overrides it. The Rust CLI
can inspect settings and change brightness through service requests; the
native tray offers brightness presets through the same requests. The service
alone saves the setting and writes the LED program. This is the first settings
control, not complete settings parity. Settings paths remain opt-in during
migration, and native tray interaction still needs visual runtime validation.
An opt-in `--auto-device` mode now scans platform mount roots, keeps the
selected device while mounted, and reconnects after removal and return.
It uses each device's saved brightness. Windows discovery now enumerates
assigned fixed and removable drives and recognizes their volume label or
`LEDS.LED`. Dot LED counts use that volume label too. Native volume queries
suppress missing-media dialogs for their calling thread and restore its error
mode; Windows tests cover the native probe and that restoration. Physical
device runtime validation remains open.
An optional label in the device protocol lets the shared UI model show the
device name instead of just its Windows drive letter. Older payloads without
that label still deserialize and use their mount path.
The service now exposes its discovered devices and accepts a selection over
IPC. The CLI can list or select them, and the native tray renders the same
choices. Only the service changes its active `DeviceOutput`.

The core now resolves explicit, environment, and process-based agent origins
and reads legacy structured origin labels from hook payloads. The hook reads
Unix ancestry through `ps` and Windows ancestry from a native process
snapshot. The Junie hook uses that ancestry to match terminal events to the
right recent session on both platforms. Captured cross-platform process trees
still need parity checks, especially Windows app versus CLI identification.

The `sidepulse-device::virtual_led` module now holds the portable virtual LED
pixel rules: status and battery colors, spatial blending, tone mapping, and
compact program previews. Its behavior is tested against the existing Python
rules. No platform window consumes this model yet; macOS notch rendering and
Windows/Linux virtual display adapters remain separate UI work.
The device layer also has the portable battery LED program policy, including
partial fills and charging pulses. The macOS reader now parses the native
`ioreg` battery plist, including charger power, negotiated profiles, capacity,
health, and a cached model-based charger baseline. Its artificial fixture
matches the legacy Python JSON snapshot. `sidepulse-next battery status`
provides the legacy JSON fields, saved charger baseline, and `--full-watts`
override; a live macOS comparison matched the stable fields and JSON keys.
Linux and Windows readers supply charge percentage and power state while
unavailable richer diagnostics remain explicitly unknown.
When saved per-device settings
select battery display, the service writes that program instead of the agent
program, without UI ownership of the device. The CLI and tray can switch the
saved per-device display between agent and battery through IPC. Power-change
previews now run in the service through a portable transition policy, including
the seven-second default and return to agent output. Battery queries run in a
separate worker so they do not delay device synchronization or IPC. Empty
macOS battery-query output is treated as no battery, covering desktop hosts.
Manual (`custom`) display mode leaves existing device output untouched,
including during previews. All three tray adapters offer this choice and a
power-change-preview toggle through the shared model and service requests.
`sidepulse-next battery configure --endpoint ENDPOINT` (or
`SIDEPULSE_NEXT_ENDPOINT`) updates the legacy battery settings atomically
through the service, preserving unknown settings. Its process test exercises
the actual CLI and service on each target platform. Global display defaults
remain separate from saved per-device choices, matching the Python settings.
The battery LED CLI route remains pending.
All bundled LED animation programs are now copied into the Rust device crate.
The service resolves saved per-mode agent styles, including custom programs
from the legacy settings `animations` directory, then validates and writes the
selected program. Editing animation profiles in the native UI remains open.
The core now validates and annotates legacy relay `agent_event` envelopes.
The service accepts these events through its local IPC and ignores repeated
event IDs using a bounded cache. The separate `sidepulse-relay` crate owns
legacy `relay.json` settings, receiver-code generation, HTTP publishing, and
bounded SSE streaming. `sidepulse-next link` can create a receiving code or
save an outbound code. A development service started with explicit
`--relay-config PATH` sends local hook events and receives remote events;
loopback HTTP tests cover both paths. Relay remains opt-in so the development
service cannot connect through the installed Python setup unexpectedly. A
production service launch and UI control for relay configuration still need
implementation.
The core also owns portable awake-policy decisions, battery safeguard rules,
closed-lid LED and animation decisions, and pure macOS sleep-diagnostic
parsers. A read-only macOS service adapter reports the actual lid state,
active external displays, and system sleep assertions through IPC; the CLI
can inspect this with `service-power`. It uses CoreGraphics for display state.
Non-macOS services return an explicit unsupported-platform error for this
macOS-specific observation. An explicit development `--power-control` service
option applies the saved awake policy on macOS. The service owns the
`caffeinate -ims` process and, only when the saved closed-lid override is
enabled, requests the existing noninteractive `pmset` helper and display
sleep. The policy uses battery safeguards and CoreGraphics display state.
No installer enables this option yet. Physical lid transitions, helper
recovery after a service crash, and the broader settings UI still require
validation. The service can now update the saved awake policy through IPC;
`service-sleep-policy` and the macOS tray use that request. The tray remains a
client and never calls power commands directly.

The `sidepulse-hook-config` crate can build install and uninstall plans for
Codex, Claude, Grok, Cursor, and Junie configurations. Its tests cover
preserving unrelated hooks, idempotence, backups, and rejecting a config that
changed after planning. The development CLI exposes those plans with explicit
`--config`, `--log`, and `--hook` paths and now defaults to all five providers,
the user's home, the existing state directory convention, and a sibling Rust
hook executable. It accepts a positional provider name like the Python CLI.
It also accepts Python's per-provider `--codex-log`, `--claude-log`,
`--grok-log`, `--cursor-log`, and `--junie-log` overrides, including `~` paths,
for single-provider and batch installation.
Temporary-home process tests cover dry run, apply, uninstall, default paths,
and missing argument values. No production hook config has been changed.
An explicit `--provider all --home DIR --log-dir DIR --hook PATH` batch route
now plans all five providers first and restores earlier config files if a
later apply fails. The test uses a temporary home; live installation still
awaits Codex trust review, full Grok backup-file cleanup, and upgrade verification.
The batch route removes old SidePulse commands from the two legacy Grok hook
JSON files while preserving unrelated commands in those files. It now also
relocates SidePulse backup JSON files out of Grok's live hooks directory into
the legacy backup folder. A relocation error rolls back provider config writes.
The Rust CLI also supports read-only `agent-monitor doctor` and `doctor --json`
reports of hook events and log paths for all five providers.
`agent-monitor live` and `watch` now refresh the same Rust monitor view using
the bounded log reader. Their terminal presentation is simpler than the
Python dashboard and still needs a final CLI output comparison.
Codex requires a user trust review for new or changed non-managed hooks. The
Rust installer reports this step and does not write trust hashes itself. The
Python installer still has its legacy automatic trust refresh, so cutover
must validate the new review flow. This follows the current
[official Codex Hooks documentation](https://learn.chatgpt.com/docs/hooks).

Further origin parity, production relay launch, production provider hook installation,
production device discovery,
physical device selection, virtual device output, full status bar settings and
controls, native helpers, full CLI parity, and release packaging still need
implementation before cutover. The Python application
remains authoritative for users until the delivery gates above pass.

## Remaining work, in delivery order

1. Verify native hook installation and removal against captured real configs
   for all five providers, preserving unrelated config and backups.
2. Finish source parity: transcript edge cases, captured process ancestry and
   Junie cases, and Python-to-Rust transition comparisons.
3. Port the remaining settings controls, relay, link, battery and sleep policy,
   custom animations, platform device discovery, and virtual device output
   into service modules.
4. Complete tray and settings controls behind the shared UI model, including
   macOS-specific status bar behavior and Windows/Linux capability adapters.
5. Cover the remaining CLI entry points, turn the staged native binaries into
   installable, signed packages, and implement platform startup registration.
6. Verify upgrades and rollback against an existing Python setup, then switch
   hook and state-file ownership only after the delivery gates pass.
