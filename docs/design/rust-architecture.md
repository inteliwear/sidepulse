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

## Current migration state

The Rust workspace currently contains the portable event/status core, a
versioned protocol, local IPC, a development service, a development CLI, and
the `sidepulse-next-hook` hook executable. The hook handles Cursor names,
Junie's missing context, Grok routing, provider JSONL, legacy audit JSONL, and
best-effort service delivery. The service can replay provider logs at startup
when launched with `--log PROVIDER PATH` pairs. Hook and service are separate
from the UI. The `sidepulse-ui-model` crate turns service snapshots into common
tray state without owning monitoring or output. A `sidepulse-next-tray` client
now renders the mode, active agents, and a quit action. It polls and reconnects
to the service; its UI event loop is native on macOS and Windows and uses the
KSNI tray backend on Linux. The development tray compiled and stayed running
during a macOS smoke test with a temporary Rust service and hook event.
Rust formatting, linting, and workspace tests pass in CI on macOS, Linux, and
Windows. Visual behavior on macOS and runtime UI and device behavior on Linux
and Windows still need validation. These executables are not installed by the
existing setup flow.

The `sidepulse-device` crate now owns the default LED programs, program
validation, candidate discovery, and synced writes. An explicitly configured
development service can write one device with `--device PATH` and optional
`--brightness 0-255`. The service polls its own state and skips unchanged
programs; the tray never touches the device file. A macOS smoke test passed
from the Rust hook through the service into `LEDS.LED` in a temporary folder.
Physical hardware behavior, automatic selection, and all custom animation
styles remain to be ported.

The development CLI now shares one fail-open hook handler between
`sidepulse-next hook-log`, `sidepulse-next agent-monitor hook-log`, and
`sidepulse-next-hook`. It also supports `status` and `agent-monitor status` as
one-shot offline views of the Rust monitor, with the existing status flags and
JSON field names. Status reads the last requested number of lines per provider
and accepts explicit log paths. It reads configured hook commands for custom
log paths in the five provider configuration files, then falls back to the
default state directory. The `sidepulse-sources` crate shares source selection,
bounded replay, and appended-row recovery between CLI and service. The service
replays those sources at startup and tails new complete rows so events written
while its socket is unavailable can still reach the monitor. Optional Codex
and Claude transcript sources can now be supplied to one-shot status with
`--codex-transcripts DIR` or `--claude-transcripts DIR`, and to the service
with `--transcript codex|claude DIR`. They replay recent files and detect later
file changes; they remain opt-in as in the Python defaults. The Rust reader
needs comparison against a larger set of captured transcript cases and a
more efficient bounded tail read for large files.

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
It uses each device's saved brightness. Windows discovery recognizes a drive
root containing `LEDS.LED`; volume-label discovery and physical-device runtime
validation remain open.

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
partial fills and charging pulses. Battery acquisition and service arbitration
with agent status are still pending.

The `sidepulse-hook-config` crate can build install and uninstall plans for
Codex, Claude, Grok, Cursor, and Junie configurations. Its tests cover
preserving unrelated hooks, idempotence, backups, and rejecting a config that
changed after planning. The development CLI exposes those plans with explicit
`--config`, `--log`, and `--hook` paths; a temporary-home process test covers
dry run, apply, and uninstall. No production hook config has been changed.
The Rust CLI also supports read-only `agent-monitor doctor` and `doctor --json`
reports of hook events and log paths for all five providers.
Codex hook trust refresh and legacy Grok file cleanup still need implementation
before the native installer can own cutover.

Further origin parity, relay delivery, production provider hook installation,
settings-controlled transcript selection, production device discovery,
physical device selection, virtual device output, full status bar settings and
controls, native helpers, full CLI parity, and release packaging still need
implementation before cutover. The Python application
remains authoritative for users until the delivery gates above pass.

## Remaining work, in delivery order

1. Implement and test native hook installation and removal for all five
   providers, preserving unrelated config and backing up changed files.
2. Finish source parity: transcript edge cases, captured process ancestry and
   Junie cases, and Python-to-Rust transition comparisons.
3. Port the remaining settings controls, relay, link, battery and sleep policy,
   custom animations, platform device discovery, and virtual device output
   into service modules.
4. Complete tray and settings controls behind the shared UI model, including
   macOS-specific status bar behavior and Windows/Linux capability adapters.
5. Cover the remaining CLI entry points and package native service, hook,
   tray, and installer binaries for all three operating systems.
6. Verify upgrades and rollback against an existing Python setup, then switch
   hook and state-file ownership only after the delivery gates pass.
