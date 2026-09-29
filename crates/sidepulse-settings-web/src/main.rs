#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod bridge;
mod previews;
use bridge::{Action, Bridge, Poll};
use std::sync::Mutex;

#[tauri::command]
fn settings_poll(
    state: tauri::State<'_, Mutex<Bridge>>,
    after_revision: u64,
    preview_program: Option<String>,
) -> Result<Poll, String> {
    state
        .lock()
        .map_err(|_| "Settings client is unavailable.".to_string())?
        .poll_with_previews(after_revision, preview_program.as_deref())
}

#[tauri::command]
fn settings_action(state: tauri::State<'_, Mutex<Bridge>>, action: Action) -> Result<(), String> {
    state
        .lock()
        .map_err(|_| "Settings client is unavailable.".to_string())?
        .action(action)
}

fn main() {
    let mut args = std::env::args().skip(1);
    let first = args.next();
    if matches!(first.as_deref(), Some("--help" | "-h")) {
        println!("usage: sidepulse-next-settings [ENDPOINT | --check-runtime]");
        return;
    }
    if first.as_deref() == Some("--check-runtime") {
        if args.next().is_some() {
            eprintln!("usage: sidepulse-next-settings --check-runtime");
            std::process::exit(2);
        }
        match tauri::webview_version() {
            Ok(version) => println!("Settings webview runtime: {version}"),
            Err(error) => {
                eprintln!(
                    "Settings webview is unavailable: {error}. Windows requires Microsoft WebView2; Linux requires WebKitGTK 4.1."
                );
                std::process::exit(1);
            }
        }
        return;
    }
    let endpoint = match (first, args.next()) {
        (Some(endpoint), None) => Some(endpoint),
        (None, None) => std::env::var("SIDEPULSE_NEXT_ENDPOINT").ok().or_else(|| {
            sidepulse_installer::endpoint_from_executable(&std::env::current_exe().ok()?)
                .ok()
                .flatten()
        }),
        _ => None,
    };
    let Some(endpoint) = endpoint.filter(|endpoint| !endpoint.trim().is_empty()) else {
        eprintln!("usage: sidepulse-next-settings ENDPOINT");
        std::process::exit(2);
    };
    let result = tauri::Builder::default()
        .plugin(tauri_plugin_clipboard_manager::init())
        .manage(Mutex::new(Bridge::new(endpoint)))
        .invoke_handler(tauri::generate_handler![settings_poll, settings_action])
        .setup(|app| {
            tauri::WebviewWindowBuilder::new(
                app,
                "main",
                tauri::WebviewUrl::App("index.html".into()),
            )
            .title("SidePulse Settings")
            .inner_size(680.0, 560.0)
            .min_inner_size(580.0, 420.0)
            .on_navigation(|url| {
                (url.scheme() == "tauri" && url.host_str() == Some("localhost"))
                    || (matches!(url.scheme(), "http" | "https")
                        && url.host_str() == Some("tauri.localhost"))
            })
            .on_new_window(|_, _| tauri::webview::NewWindowResponse::Deny)
            .build()?;
            Ok(())
        })
        .run(tauri::generate_context!());
    if let Err(error) = result {
        eprintln!("Could not open SidePulse Settings: {error}");
        std::process::exit(1);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{Value, json};

    #[test]
    fn tauri_invocation_uses_the_bundled_window_capability_and_typed_boundary() {
        let app = tauri::test::mock_builder()
            .manage(Mutex::new(Bridge::new(
                "sidepulse-unused-test-endpoint".into(),
            )))
            .invoke_handler(tauri::generate_handler![settings_poll, settings_action])
            .build(tauri::generate_context!())
            .unwrap();
        let main = tauri::WebviewWindowBuilder::new(&app, "main", Default::default())
            .build()
            .unwrap();
        let other = tauri::WebviewWindowBuilder::new(&app, "other", Default::default())
            .build()
            .unwrap();
        let invoke = |window: &tauri::WebviewWindow<tauri::test::MockRuntime>,
                      command: &str,
                      body: Value,
                      url: &str| {
            tauri::test::get_ipc_response(
                window,
                tauri::webview::InvokeRequest {
                    cmd: command.into(),
                    callback: tauri::ipc::CallbackFn(0),
                    error: tauri::ipc::CallbackFn(1),
                    url: url.parse().unwrap(),
                    body: tauri::ipc::InvokeBody::Json(body),
                    headers: Default::default(),
                    invoke_key: tauri::test::INVOKE_KEY.into(),
                },
            )
            .map(|response| response.deserialize::<Value>().unwrap())
        };
        let local = if cfg!(windows) {
            "http://tauri.localhost"
        } else {
            "tauri://localhost"
        };
        let result = invoke(&main, "settings_poll", json!({"afterRevision":0}), local).unwrap();
        assert_eq!(result["connected"], false);
        assert!(invoke(&other, "settings_poll", json!({"afterRevision":0}), local).is_err());
        assert!(
            invoke(
                &main,
                "settings_poll",
                json!({"afterRevision":0}),
                "https://example.com"
            )
            .is_err()
        );
        let error = invoke(
            &main,
            "settings_action",
            json!({"action":{"type":"request","request":{"command":"shutdown"}}}),
            local,
        )
        .unwrap_err();
        assert!(error.to_string().contains("not available from Settings"));
    }
}
