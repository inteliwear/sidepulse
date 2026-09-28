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
named custom assets remain selectable and are preserved. Named assets, profiles, and session-opening preferences now have service-owned
operations and native controls. Relay controls now use the service as well; native helper recovery and release controls remain open.
The service now applies saved idle timeouts to snapshots immediately, without
rebuilding its monitor or losing pending permissions. The shared presentation
model includes only completed stale sessions within the configured recent
retention, matching Python menu rules. The window can save these durations and
the macOS battery sleep safeguard through validated requests; the development
CLI exposes `service-agent-list` and `service-sleep-safeguard` for the same
operations. Sleep controls still report an unsupported-platform error on
Windows and Linux.

## Activity history

The service records status, battery, charger, lid, and sleep observations every
two seconds in the existing `status-history.jsonl` schema. Its 34 fields match
a captured Python record. The file defaults beside the explicit state file,
or beside an explicit settings file; `--history PATH` overrides it. Preview
history stays inside its isolated directory. Recording runs independently of
any window and restores existing rows on startup. The service keeps a bounded
in-memory timeline, replays at most 128 MiB at startup, and limits chart replies
to 2,000 observations, including the first and latest. Long timelines are
sampled and the window labels that summary. The settings window charts agent
state, battery percentage, and charger power, with lid and awake details on
hover. The five legacy timeframes are saved through the service. Native chart
layout checks await an unlocked desktop.

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
saved animation styles and profile editing are covered below.

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
rules. The service now hosts the same bundled firmware WASM engine in Rust, with a
bounded interpreter, to execute presets and custom programs and return RGB
frames. The UI does no program parsing or animation selection. Saving an
animation checks firmware syntax for both device sizes, including line/column
errors. A separate Rust virtual display client draws those frames; macOS uses
native notch geometry and an all-spaces, transparent, non-interactive window,
while Windows and Linux use a movable window. Virtual display enablement,
brightness, and display mode are saved through the service, preserving the
legacy virtual device ID. Tray and settings clients launch one virtual display
per endpoint; manual mode hides it. Preview staging includes its executable and
a macOS application bundle. Screen changes and native Windows/Linux drawing
still require runtime validation.
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
The battery LED CLI renders through the service and supports one-shot and
continuous output, destination previews, explicit filenames, charger baselines,
duplicate suppression, and interruption.
All bundled LED animation programs are now copied into the Rust device crate.
The service resolves saved per-mode agent styles, including custom programs
from the legacy settings `animations` directory, then validates and writes the
selected program. The native animation page now edits profiles and named assets through the service.
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
physical device validation, the remaining status bar settings and controls,
native helpers, full CLI parity, and release packaging still need
implementation before cutover. The Python application
remains authoritative for users until the delivery gates above pass.

## Remaining work, in delivery order

1. Verify native hook installation and removal against captured real configs
   for all five providers, preserving unrelated config and backups.
2. Finish source parity: transcript edge cases, captured process ancestry and
   Junie cases, and Python-to-Rust transition comparisons.
3. Finish native helper lifecycle management and remaining settings controls.
   Phone pairing, automatic output, manual delivery, notifications, and power
   helper recovery are implemented in service modules.
4. Complete tray and settings controls behind the shared UI model, including
   macOS-specific status bar behavior and Windows/Linux capability adapters.
5. Cover the remaining CLI entry points, turn the staged native binaries into
   installable, signed packages, and implement platform startup registration.
6. Verify upgrades and rollback against an existing Python setup, then switch
   hook and state-file ownership only after the delivery gates pass.

## Portable local reply classifier

`sidepulse-reply` contains local CPU inference, pinned model downloads, the
captured prompt, and the existing label parser. `sidepulse-next-reply` is the
ninth staged executable, available through `sidepulse-next reply`. Model
downloads are explicit; inference runs offline. The Rust benchmark example
loads once and reports canonical predictions and warm timing. This optional
component stays outside the monitor and UI.

The [evaluation](reply-classifier-evaluation.md) records the measured prediction
differences between the MLX and portable GGUF formats. The native backend is
implemented; classification parity and GPU performance are not claimed.

## Session opening (Rust preview)

Session targets, URL escaping, provider resume arguments, and preference precedence
live in `sidepulse-core`. The service returns available targets and owns atomic
preference writes. The tray, settings window, and `open-session` CLI call the
`sidepulse-platform` activation adapter. `open-session --dry-run` retrieves the
chosen target without opening an application. An explicitly unsupported action
returns an error instead of launching a different destination.

The Sessions page saves provider defaults and terminal selection. Provider-wide
changes discard that provider's origin overrides, as in the Python settings
model. Custom `.command` files on macOS are unique, private, and remove themselves
when run. Windows resume commands use encoded PowerShell literals, avoiding
Windows Terminal's semicolon parsing. macOS app bundles declare their terminal
automation usage. Ghostty scripting requires its current AppleScript support;
see the [Ghostty documentation](https://ghostty.org/docs/features/applescript) and
[Windows Terminal arguments](https://learn.microsoft.com/en-us/windows/terminal/command-line-arguments).

Portable integration tests exercise the actual CLI and local IPC, preference
persistence, unknown-field retention, missing sessions, explicit-action failures,
and hostile characters in session arguments. Native activation and existing
terminal focus/reuse still require validation. The current activation adapter
opens a new terminal session. Native window checks for the latest history and
virtual clients are pending because the Mac was locked. Automated Rust checks,
tests, release builds, staging, and service smoke checks passed on all three
platforms at `4052e40`.


## Profiles and named animation assets

`sidepulse-core` owns profile documents, identifiers, state defaults, working-mode
grouping, and the Cyan/Ember/Purple profiles. Captured exports from the Python
settings model verify all three built-ins. The service owns named `.LED` assets,
profile capture, apply, delete, import, and export. Imports remap conflicting
custom identifiers and validate every program for 2- and 8-LED firmware before
publishing settings. Inline programs are promoted to named assets when a profile
is saved, making exports portable.

Asset updates publish a new private file and atomically update the settings
pointer. A failed or conflicting save removes only its new files and preserves
previous assets, settings, and unknown fields. Previous asset versions remain
available for rollback. Built-in profiles cannot be replaced or deleted; assets
referenced by statuses or profiles cannot be deleted. Named assets are editable
in the native window. Profiles can be applied, captured, imported from JSON, and
exported as JSON there. The CLI provides `animation-profile` and `animation-asset`
commands for file-based workflows; `service-animation` also accepts `lid_open`
and `lid_closed` state selections. Lid-event output and final-frame holding are described below.

Portable CLI/IPC tests cover import/export, identifier collisions, working-mode
grouping, invalid programs, and built-in protection. Persistence tests cover
external-edit conflicts, rollback of new files, inline promotion, restart, and
symlink directories. Native controls still await visual checks on an unlocked Mac.


## Lid output and device keepalive

The core now owns lid edge detection, transition timing, interrupted transitions,
and final-output holding. The service caches read-only power observations and
applies this policy when it drives a physical device. A close transition can
finish and retain its final frame until opening the lid or resuming work releases
the hold. Unknown observations preserve the last known lid state, and startup
with a closed lid does not invent an animation event. Manual output remains
untouched. The virtual display keeps its existing independent behavior.

The legacy 0.15-second restore allowance, default transition durations, working
awake behavior, and five-minute completion/input/error grace are represented
in pure policy types. Custom transition duration saves are atomic and retain
existing programs and unknown settings. The native animation page and CLI can
save timing. The service touches the firmware's `keepalive` file once a minute,
preserving its content, including while output is manual or held.

Deterministic tests drive simulated lid observations into a real temporary LED
file, verify transition/hold/open/manual behavior, interrupted transitions,
unknown readings, startup, grace expiry, and atomic duration saves. Native lid
hardware and power-control execution still need validation; automatic system
power changes remain behind the explicit `--power-control` preview flag.

## Relay configuration controls

The service now owns relay settings when started with an explicit `--relay-config`
path. The native Link computers page creates and replaces receiving codes,
configures sending links and the computer name, disconnects either direction,
and shows the last successful activity and transport errors. The Rust CLI can
use `link --endpoint` for the same service-owned writes, or manage an explicit
legacy relay file offline. Unknown fields and private permissions are preserved;
external edits produce a conflict until the saved file is explicitly reloaded.

Receiving connections are tied to the configuration generation. Changing or
stopping a link discards later messages from its previous stream, then reconnects
using the new configuration. An idle connection may take up to its bounded
read timeout to close, but its old messages cannot enter the monitor after the
change. Isolated preview bundles include their own disabled relay file. No
network relay starts until a receiving code or sending link is configured.

Portable tests cover actual CLI/IPC updates, code replacement, conflicts and
reload, unknown fields, permissions, and SSE cancellation. Local HTTP process
tests exercise publishing, receiving, and rejecting stale events after a receiver
is disabled. A `--mock-power` JSON input supports deterministic device/lid process
checks on every platform and cannot be combined with system power control.

## Phone pairing and manual delivery (Rust preview)

`sidepulse-core` selects destinations without I/O: `write` prefers a mounted
local device, `push` prefers a saved phone, notifications require a phone, and
ambiguous names require an explicit ID. `--all` fans LED programs out to both
kinds of destination. `--dry-run` returns the same plan without writing or sending.

`sidepulse-links` owns the legacy version-1 `links.json` schema, private atomic
credential saves, pairing URLs and QR matrices, bounded registration SSE, and
HTTP notification transport. Unknown saved fields survive edits; conflicting
external saves require a reload. Public snapshots expose the legacy short phone
ID, name and server, without a push token. Transport errors redact tokens.

The service owns five-minute pairing sessions, cancellation, saved phone
mutations, and bounded asynchronous delivery jobs. A replaced or cancelled
pairing cannot save a late registration. Manual local writes switch the device
to its saved custom display mode while holding the output lock, preserving its
brightness setting. Explicit filenames are resolved once and written as given.
Delivery errors are reported for each destination.

The settings client's Link phones page renders the service's QR matrix and
saved phone summaries. The CLI exposes `phone-link pair|list|cancel|reload|register|remove`
and `write` / `push`, with `--endpoint` or `SIDEPULSE_NEXT_ENDPOINT`. CLI pairing
starts a service-owned session; `phone-link list` reads its subsequent state.
Preview staging supplies an isolated empty `links.json` and an explicit service
path. It never imports the installed phone credentials.

Portable tests cover legacy documents, unknown fields, stale saves, Unix file
permissions, actual local HTTP pairing and notification delivery, invalid
registrations, cancellation before connecting, destination routing, actual
CLI/IPC registration/removal, dry runs, manual output, and custom filenames.
Automatic linked-phone agent output and saved phone display controls are the
next implementation step. Real-device pairing and native visual QA remain
pending while the Mac is locked.

### Automatic linked-phone output

The service now has explicit `--phone-output` activation, requiring both a
settings path and a phone links path. Preview staging leaves it disabled.
An independent worker renders eight-LED agent animations or battery programs
and sends only when the generated program changes. Battery power-change preview
uses the shared service policy. Phone output uses full brightness, matching the
legacy phone path. Failures retain the last successful program, expose a
redacted error and retry after thirty seconds.

The legacy `ios/ID` device settings are preserved and edited atomically by the
service. The native Phone page and `phone-link display ID agent|battery|custom`
control each phone's display. A manual LED delivery serializes against automatic
sends and saves custom mode before transport, so the next automatic cycle
cannot overwrite it. Notification-only delivery leaves the display preference
intact. Network operations do not hold the settings or UI snapshot locks.

Local HTTP tests verify agent-to-manual-to-agent transitions, duplicate
suppression, battery output, saved unknown fields, link removal, error redaction
and retry backoff. Real phone receipt remains a separate device validation gate.

## Native helpers and power recovery (Rust preview)

`sidepulse-helpers` replaces the packaged C SD eject guard with a Rust binary
using the installed macOS DiskArbitration/CoreFoundation APIs. Hardware matching
preserves the legacy Secure Digital protocol / SDXC model rule. It dissents
software ejects, deduplicates five-second mount retries, releases retries when a
disk mounts or disappears, caps retained disks, bounds redirected logs, and
unregisters callbacks on termination. `sdejectguard check` opens and closes a
session without registering a veto. `sdejectguard run [--no-mount]` is the explicit
runtime route. Windows and Linux report the unavailable capability.

The preview now stages eight binaries, including the guard, without registering
or running it. Actual eject/wake behavior and the guard's startup manager remain
separate validation and installer work.

`status-bar install-sleep-helper`, `uninstall-sleep-helper`, and
`sleep-helper-status` are Rust CLI routes. Install/remove support an inspectable
`--dry-run`; applying on macOS requires root. Installation publishes only the
legacy two-command pmset sudoers rule, validates it with visudo, sets root
ownership and mode 0440, and checks for an external edit before publishing.
Symlinks and unrelated existing sudoers rules are refused. Tests use temporary
files and never install a system rule.

The service exposes power-control health, accurate requested/active state even
when the helper fails, a thirty-second retry delay, and an explicit retry
request. The Sleep page shows runtime errors and retry control; CLI routes are
`service-power-control` and `service-power-retry`. Mocked controller tests cover
missing helpers, delay, explicit retry, failed restoration and recovery without
invoking power commands. A real read-only SD session check passed locally.
System helper installation, actual sleep changes and eject protection remain
inactive in the preview.

## Agent and battery LED CLI loops

`agent-monitor leds` and `battery leds` now use the Rust service to select a
destination, render a program for its two- or eight-LED size, and perform manual
delivery. They support explicit devices, filenames, dry runs, one-shot output,
refresh intervals and graceful interruption. Unchanged programs are suppressed
between refreshes. Battery output uses the saved charger baseline or an explicit
`--full-watts` override; explicit `auto` is distinct from an omitted option.
Agent CLI output retains the legacy default animation palette.

Portable real CLI/IPC tests verify a two-LED agent preview without writing and
a battery program written to an explicit custom filename. The shared service
continues to own the monitor, battery snapshot and device write. The optional
per-provider log flags on a detached LED loop still need compatibility work;
the Rust route currently selects the service through its explicit endpoint.

## Native preview startup and shutdown

`sidepulse-next setup --stage-dir DIR` assembles the isolated native bundle;
`--dry-run` produces its manifest without writing it. Service and tray lifecycle
commands accept that explicit bundle:

```sh
sidepulse-next service install --stage-dir DIR --dry-run
sidepulse-next service install --stage-dir DIR --no-start
sidepulse-next service start --stage-dir DIR
sidepulse-next service status --stage-dir DIR
sidepulse-next service stop --stage-dir DIR
sidepulse-next service uninstall --stage-dir DIR
sidepulse-next status-bar install --stage-dir DIR --dry-run
```

Startup registration lives in `sidepulse-installer`, separately from UI and
monitor policy. Each preview has a unique label derived from its path. macOS
uses user LaunchAgents, Linux uses user systemd units, and Windows uses a
Task Scheduler logon trigger with the current user's interactive token and
least privilege. Windows definitions follow Microsoft's
[logon task schema](https://learn.microsoft.com/en-us/windows/win32/taskschd/logon-trigger-example--xml-).
Existing files and task commands must match their expected owner. Plans detect
external edits, refuse symlinks or unrelated entries, and retain a verified
startup file if the manager fails so installation can be retried explicitly.
Windows task inspection checks its action, working directory, user, logon type,
and privileges. Manager queries distinguish registration from running state;
Windows running state is reported as unknown rather than inferred from a task's
presence. No startup registration was applied to the development machine.

`sidepulse-next sdejectguard install|start|stop|uninstall|status --stage-dir DIR`
manages the macOS guard in the user scope. System scope deployment still needs a
root-owned immutable helper package and hardware validation.

The service handles termination signals and explicit IPC shutdown. It cancels
background recovery, relay, discovery, output, history, battery, and power loops,
joins runtime workers, and flushes its final state. The power controller drops
before process exit and restores any override it applied; failed restoration is
logged. Native power queries and mutations have deadlines. Windows service stops
request a graceful IPC shutdown before removing the scheduled task. Real binary
checks verify both IPC and termination shutdown with isolated logs and files;
these checks do not activate power control.

## Native CLI compatibility

The staged `bin` directory now also contains native entry points named
`sidepulse`, `agent-monitor`, `agent-status-bar`, and `sidepulse-reply`. They are
copies of the corresponding Rust executables, with no Python launcher. The
multicall CLI routes the original names to their command groups, supports help
and version flags, and resolves its explicit preview bundle's endpoint when
connection arguments are omitted. These files have not been added to the
user's active PATH.

Agent LED monitoring also runs independently of a background service. Provider
logs, transcripts, replay limits, stale policy, and tool timeout options use the
shared source reader and core monitor. The CLI retains monitor state across
new log records, uses the common device and destination modules, supports
one-shot and continuous previews/writes, restores externally changed output,
refreshes firmware keepalive, and handles interruption. Explicit connection
arguments select service-owned rendering and delivery. Standalone source flags
cannot change the policy of an explicitly selected running service.

Tests compare actual CLI output with captured Python two-LED programs, follow a
new completion event through the continuous loop, verify custom filenames and
no writes during previews, and exercise native compatibility names. Staged
smoke checks now exercise those entry points and require graceful service exit.


## Offline preview updates, rollback, and recovery

`sidepulse update --source-dir DOWNLOAD --stage-dir PREVIEW --backup-dir BACKUP`
prepares all nine native binaries, aliases, application bundles, and launch files
in a sibling temporary directory. The selected service must be stopped before
replacement. Its endpoint and startup paths remain stable. Raw `settings.json`,
`links.json`, `relay.json`, and all ordinary files under `state/` are retained,
including unknown settings and log fields. State is bounded to 128 MiB and
10,000 files; symlinks and special files are refused, except stale IPC sockets.
Unknown files elsewhere in the old bundle remain in the backup.

`sidepulse rollback --stage-dir PREVIEW --backup-dir BACKUP --save-current SAVED`
restores older native executables while preserving the latest runtime settings
and logs. Deleted runtime files are not revived. Both saved bundles remain
available for another rollback. Backups contain identity and payload checksums;
modified backups are refused. Advisory filesystem locks prevent concurrent
native updates. Settings, payloads, and source binaries are rechecked before
publication. `--dry-run` prints each plan without making changes.

A durable receipt is written before the original directory moves. Publication
failures restore it when its original path remains vacant. If a process is
interrupted in the gap between directory moves, `sidepulse recover --stage-dir
PREVIEW --backup-dir BACKUP` restores the checksum-verified original. Recovery
refuses to overwrite any directory at the original location. On Windows, run
bundle replacement from the downloaded binaries outside the selected bundle;
open executables can prevent filesystem replacement. Restart existing preview
startup entries after an update; these commands do not change their registration.

Tests cover raw data preservation, rollback and redo, deleted files, edited
plans and backups, concurrent operations, live endpoints, publication failure,
and interrupted publication recovery. Actual native executable update and
rollback smoke tests use isolated previews. This is a native preview migration
path; ownership transfer from the production Python installation is still gated
on hardware, UI, and release validation.
