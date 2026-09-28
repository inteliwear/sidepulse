# Settings webview proposal

Status: proposed renderer change; the preview still uses egui. The shared Rust
client has been extracted so either renderer can use the same behavior.

## Recommendation

Use a Tauri webview for Settings and history, with HTML/CSS and a small JavaScript
presentation layer. Keep hooks, monitoring, validation, persistence, device
output, relay, phone delivery, and power decisions in Rust. Keep the tray and
virtual LED overlay as native Rust clients.

The current egui window already builds on macOS, Windows, and Linux. The reason
to choose a webview is easier layout/styling and reuse of web components and
browser-based presentation checks. Native webviews still need platform testing:
Tauri uses WebView2 on Windows, WKWebView on macOS, and WebKitGTK on Linux.
[Tauri process model](https://v2.tauri.app/concept/process-model/)

If the UI source must also be Rust, Dioxus desktop provides Rust components
rendered through a webview. It uses the same underlying Wry webview library.
[Dioxus desktop](https://dioxuslabs.com/learn/0.7/guides/platforms/desktop/)

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

The webview host should expose named Settings actions through the existing Rust
client. Service access, startup management, file opening, and session opening
remain in Rust. The frontend should load bundled assets and receive a display
view, with no direct filesystem or generic shell interface.

## Implementation sequence

1. Extract the shared client from the current window — completed.
2. Choose Tauri with a web presentation layer or Dioxus with Rust components.
3. Embed the frontend and preserve the existing executable, endpoint, and app
   bundle entry points.
4. Port the twelve Settings pages, including conflict handling, retained drafts,
   offline recovery, profiles, pairing, diagnostics, and the six-row chart.
5. Verify state/action contracts and browser layout, then exercise each actual
   system webview. Browser checks do not replace native window checks.
6. Update the platform runtime dependencies, packaging, and CI. Preserve the
   installed Python owner until the native migration gates pass.

The backend protocol and hook integration do not need to change for this UI port.
