# Settings webview architecture

Status: Tauri with HTML/CSS was selected by the user. The preview now implements
that renderer over the shared Rust client. Native webview checks remain a delivery
gate while the desktop is locked.

## Selected approach

Use a Tauri webview for Settings and history, with HTML/CSS and a small JavaScript
presentation layer. Keep hooks, monitoring, validation, persistence, device
output, relay, phone delivery, and power decisions in Rust. Keep the tray and
virtual LED overlay as native Rust clients.

The previous egui Settings window has been replaced. The virtual LED overlay
still uses egui/eframe in its separate `sidepulse-virtual-ui` crate and native executable. The webview provides
shared layout/styling and browser presentation checks. Native webviews still need platform testing:
Tauri uses WebView2 on Windows, WKWebView on macOS, and WebKitGTK on Linux.
[Tauri process model](https://v2.tauri.app/concept/process-model/)

## Python UI structure

The Python `build_settings_window` is the source of the Settings structure. The
webview uses the same five top tabs in the same order, rather than the expanded
sidebar introduced in the first Tauri port:

| Tab | Groups and order |
| --- | --- |
| Agents | Agent Hooks (Codex, Claude, Grok, Cursor, Junie), Session Opening with Terminal App beside it, Transcript Monitoring |
| Animations | Profile selection/save/delete, Export JSON/Import JSON/Add Custom, eight State/Animation/Live Preview/Actions rows |
| Advanced | Agent List retention and idle timeout, Sleep Prevention battery threshold, battery plug/unplug preview, menu bar visibility |
| History | Status History, timeframe, six chart rows, Refresh |
| Diagnostics | Debug Log and exports, Settings File |

Working, tool running and long tasks share one animation row. Lid Open and Lid
Closed appear in the same table. Profile and custom-animation editors open as
dialogs. The chart follows Python's order: Agent, Battery, Charger, SidePulse,
macOS Sleep, Lid. The window starts at Python's 680 by 560 Settings size and
allows resizing for accessibility and other desktop platforms.

Additional Rust preview controls remain reachable through a separate Controls
menu and Setup entry outside the five Settings tabs. Hook installation belongs
on Agents; startup/helper management belongs in Setup. Previewing hook changes
remains available in Setup. These additional controls do not become Settings tabs.

A browser test reads the Python source with `ast`, without importing AppKit, and
compares the actual tab names/order, section headings, providers, animation
columns/state rows and history row order. Chromium and WebKit also exercise the
controls, dialogs, retained drafts and narrow layouts.

Live preview frames are produced in Rust by the bundled firmware LED engine.
The browser only paints returned RGB values. Show requests are bounded to the
selected connected agent-display device for three seconds (ten seconds in the
editor), after which current agent output resumes. They do not save preferences.
Saving an edited row creates/updates its named asset and assigns it to that row
in one atomic Rust settings save, including the grouped working states.

## Boundaries

```text
hooks -> Rust service <- Rust CLI
              ^
              |
       shared Rust UI client
              ^
              |
      Settings host / webview

native Rust tray and LED overlay -> service
```

`sidepulse-ui-client` collects the Settings view, validates protocol responses,
polls off the renderer thread, and returns typed save/export/setup results.
`sidepulse-ui-model` remains a shared presentation projection. The service owns
all application policy and storage; the frontend keeps temporary input drafts
and draws the returned state.

The `sidepulse-settings-web` host exposes a typed Settings action allowlist
through the existing Rust client. Service access, startup management, file opening, and session opening
remain in Rust. The frontend loads embedded HTML/CSS/JavaScript, receives a serialized display
view, and has no direct filesystem or generic shell interface. The capability
allows only the main local window to poll/send Settings actions and write text
to the clipboard. Remote navigation and new windows are denied. Report opening
uses only a path returned by the Rust client after an export.

State polling uses revisions to omit unchanged history payloads. The Rust update
queue is bounded. Drafts stay in the frontend across polling, failed saves and
reconnection; successful saves await a newer service snapshot before refreshing
input values. Startup recovery remains available when the service is offline.

## Implementation sequence

1. Extract the shared client from the current window — completed.
2. Choose Tauri with HTML/CSS — accepted.
3. Embed the frontend and preserve the existing executable, endpoint, and app
   bundle entry points — implemented.
4. Restore the Python five-tab Settings structure, dialog editors and animation
   table; retain additional preview controls outside those tabs — implemented.
5. Verify state/action contracts and browser layout, then exercise each actual
   system webview. Browser checks do not replace native window checks.
6. Update the platform runtime dependencies, packaging, and CI. Preserve the
   installed Python owner until the native migration gates pass.

The backend protocol and hook integration do not need to change for this UI port.

## Build and runtime

The canonical Settings executable remains `sidepulse-next-settings`. Assets are
embedded by Cargo; no frontend build, Node runtime, local HTTP server or asset
sidecar is required by the installed application. Node and Playwright are used
only for presentation tests (`npm ci`, `npx playwright install chromium webkit`,
then `npm test` inside `crates/sidepulse-settings-web`).

The preview sets macOS 11 as its minimum, uses the system WKWebView there,
requires Microsoft WebView2 on Windows and WebKitGTK 4.1 on Linux. The portable
package README records these requirements. `sidepulse-next-settings --check-runtime`
checks the webview version without creating a window or contacting the service.
The current portable preview does not install a webview runtime automatically.
A future Windows release installer must include an appropriate WebView2 setup
policy. Linux CI installs GTK/WebKit development dependencies; the browser job
checks Chromium and WebKit with fixture data.

[Tauri prerequisites](https://v2.tauri.app/start/prerequisites/),
[Windows webview distribution](https://v2.tauri.app/distribute/windows-installer/)

## Validation boundary

Rust tests exercise serialized view compatibility, command validation, export
path ownership, offline startup dispatch and the actual Tauri capability/IPC
boundary with its mock runtime. Browser tests compare the Python structure, render every tab/utility panel and exercise
Settings actions, failed saves, live updates, retained drafts, history hover and
keyboard selection, QR drawing, clipboard requests and narrow/dark layouts.
Those tests use local fixtures and do not change real startup, hooks, devices,
phone links, power control or clipboard contents.

The system webview window, its clipboard integration, terminal activation and
tray/overlay behavior must still be exercised on actual desktops. Earlier egui
native checks are evidence for the Rust service/client behavior; they are not
native validation of the new Tauri renderer.
