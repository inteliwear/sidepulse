# Rust migration validation

The implementation is available in the Rust preview. The installed Python release
still owns production hooks, startup, settings, and physical output. Production
cutover is a separate delivery gate.

## Automated and isolated checks

- Formatting, workspace lint with all features, 232 tests, and release builds pass
  locally. CI builds, tests, packages, archives, stages, and smoke-checks macOS,
  Windows, and Linux.
- Captured Python comparisons cover 75 origin cases, 20 transcript cases,
  35 transition sequences with 150 snapshots, five provider configurations,
  and 57 maintained example LED programs.
- Actual CLI/service tests cover hook setup, settings, phone and relay controls,
  history, profiles, delivery, recovery, shutdown, tray visibility, and diagnostic exports.
- Hook installation preserves unrelated configuration, backs up changes, cleans
  duplicated legacy Grok handlers, and moves backups outside active hooks.
- Startup recovery resolves a verified staged manifest without a running service;
  tests inspect plans without registering jobs or starting helpers.
- Settings now uses an embedded Tauri HTML/CSS/JavaScript frontend over the
  shared Rust client; the virtual overlay has its own Rust UI crate. Chromium
  and WebKit presentation tests pass for the Python-matched five Settings tabs and separate utility panels,
  typed actions,
  successful/failed saves, retained drafts during external updates, offline
  startup recovery, animation/profile editing, phone pairing/QR, clipboard
  requests, six-row history with keyboard selection, and narrow/dark layouts.
  A source-based check reads Python's AST and compares actual tab names/order,
  section headings, hook providers, animation columns/eight states and six
  history rows. The chart keeps 12-pixel labels at minimum width. These are fixture browser
  checks; they do not verify actual system-webview integration or desktop trays.
- Animation row previews run the bundled firmware engine in Rust; typed Show
  requests temporarily use a selected agent-display device for three or ten
  seconds without saving settings. Mock-device tests verify restoration and
  cancellation when the display mode changes. Saving an edited row assigns its
  named asset and grouped working states in one atomic settings write.
- Completed sessions retain their opener targets after becoming inactive; both
  the service and shared Settings client include them in recent-session actions.
- A real CLI process retains pending permissions beyond its initial replay
  limit, applies appended completion events, writes plain redirected output,
  and exits cleanly on Unix termination signals. Live monitoring keeps one
  monitor instance across refreshes, matching the Python state lifetime.
- Closing a status text/JSON or live-monitor output pipe exits successfully
  without a panic. A zero recent-time window disables age filtering, matching
  Python; actual process tests cover both behaviors. Status/live help returns
  successfully without opening sources or starting the monitor.
- The Settings client now lives in a renderer-independent Rust crate. Local
  transport tests load its complete view without creating a window and reject
  invalid protocol versions/request IDs while preserving service conflict
  messages. The Tauri mock-runtime tests exercise the actual command capability
  and reject requests from other windows/remote origins. Recorded browser
  requests deserialize and validate through the Rust Settings action boundary.
  The update queue is bounded and unchanged history replies are omitted.
- CSV/HTML export tests cover the legacy columns, Unicode and quoting, HTML
  escaping, malformed rows, size limits, atomic publication, multiple audit
  sources, and retained source bytes.
- Explicit legacy import preserves settings, phone links, custom assets, provider
  logs, audit logs, history, latest state, and unknown fields. Updates and rollback
  preserve later runtime edits.
- Local package QA passed archive extraction, ad hoc signing, signature retention
  after staging, and unsigned Mac PKG creation and inspection.
- A fresh rehearsal while the desktop was locked extracted and verified the
  latest archive, imported all five provider logs plus audit/history/latest,
  preserved raw source bytes, and retained later settings, links, relay,
  custom assets, and runtime edits through update, rollback, and redo. The
  staged service passed startup checks before and after replacement. The latest
  unsigned PKG was inspected without installation and has no install scripts.
- Actual one-shot CLI output matches Python for eight isolated event fixtures
  (SessionStart, UserPromptSubmit, PreToolUse, PermissionRequest, PostToolUse,
  Stop, Notification, SessionEnd), ignoring only elapsed age. Live output uses
  that compact status view; Python's colored table is a presentation difference.

## Earlier native macOS checks completed

All five providers also passed staged binary dry-run, install/status/remove, backup,
unknown-field retention, and Grok legacy cleanup checks. Its hook wrote an isolated
audit; CSV/HTML exports preserved their source and escaped hostile event text.

These Settings checks used the earlier egui renderer; they are not native
validation of the replacement Tauri window.

An isolated staged service used private state and a mock LED file. It had no
physical device discovery/output, phone output, or power-control activation.

- Connected settings, saved device/battery preferences, visible save conflicts,
  retained drafts, and reconnection after service restart.
- Activity, session preferences, history timeframe persistence, named animation
  assets, profile saves and exports, and local relay preference controls.
- Phone QR rendering, cancellation, registration through a local fixture server,
  and saved phone display preferences. No real phone delivery occurred.
- Virtual display: battery cyan, a saved pink working program, manual-mode hiding,
  and reappearance when agent display resumes.
- Large history replies and delayed partial reads/writes after the macOS IPC fix.

## Outstanding native and release gates

- Exercise the actual Tauri Settings window on macOS, Windows and Linux,
  including every page, clipboard, saves/reconnection, offline startup recovery
  and the six-row chart. The Mac remains locked and computer control could not
  unlock it. Rust/mock/browser checks do not replace this native window gate.
- Exercise tray menus and terminal focus/reuse. The computer-control adapter could
  not bind the windowless tray and explicitly rejected Terminal control.
- Run native UI and physical-device checks on Windows/Linux; CI verifies their
  builds, services, and packaging, not their desktop or attached hardware.
- Verify live provider activation, Codex hook trust review, real phone receipt,
  physical LED devices, sleep/wake and SD eject behavior, and microphone capture.
- Apply a genuine publisher signature, notarization where required, and native
  installer trust checks with publisher credentials.
- Validate production migration and rollback before transferring ownership from
  the installed Python release.

The portable reply classifier is implemented but differs from MLX predictions;
see [reply-classifier-evaluation.md](reply-classifier-evaluation.md). It is not
silently enabled in monitoring.
