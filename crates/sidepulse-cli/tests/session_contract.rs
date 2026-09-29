//! Actual CLI and IPC contract, without activating a desktop application.

use interprocess::local_socket::prelude::*;
use serde_json::{Value, json};
use std::fs;
use std::process::Command;
use std::time::{SystemTime, UNIX_EPOCH};

#[test]
fn session_preferences_and_explicit_openers_are_owned_by_the_service() {
    #[cfg(unix)]
    let directory = tempfile::tempdir_in("/tmp").unwrap();
    #[cfg(windows)]
    let directory = tempfile::tempdir().unwrap();
    let id = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    #[cfg(unix)]
    let endpoint = directory
        .path()
        .join(format!("s-{id}.sock"))
        .to_string_lossy()
        .into_owned();
    #[cfg(windows)]
    let endpoint = format!("sidepulse-session-{id}");
    let path = directory.path().join("settings.json");
    fs::write(
        &path,
        json!({"unknown":7,"session_open_preferences":{
            "origin:claude:terminal":"vscode", "codex":"app"
        }})
        .to_string(),
    )
    .unwrap();
    let service = sidepulse_daemon::Service::new();
    service.configure_settings(&path).unwrap();
    let event = sidepulse_core::parse_log_line(
        "codex",
        &json!({
            "hook_event_name":"UserPromptSubmit", "session_id":"a';echo oops;雪",
            "cwd":"/tmp/project's folder", "timestamp":chrono::Utc::now().to_rfc3339()
        })
        .to_string(),
    )
    .unwrap();
    service.ingest_record(&event).unwrap();
    let agent = service.snapshot().unwrap().statuses[0].agent_id.clone();
    let listener = sidepulse_ipc::bind(&endpoint).unwrap();
    let server = std::thread::spawn(move || {
        for _ in 0..8 {
            service
                .serve_connection(listener.accept().unwrap())
                .unwrap();
        }
    });
    let run = |command: &str, args: &[&str]| {
        Command::new(env!("CARGO_BIN_EXE_sidepulse-next"))
            .args([command, &endpoint])
            .args(args)
            .output()
            .unwrap()
    };
    let success = |command: &str, args: &[&str]| {
        let output = run(command, args);
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        serde_json::from_slice::<Value>(&output.stdout).unwrap()
    };
    let target = success("open-session", &[&agent, "--dry-run"]);
    assert_eq!(target["action"], "app");
    assert!(
        target["target"]["url"]
            .as_str()
            .unwrap()
            .contains("%27%3Becho%20oops%3B")
    );
    success(
        "service-session-preference",
        &["codex", "terminal", "Codex CLI"],
    );
    let saved = success("service-session-preference", &["codex", "terminal"]);
    assert!(
        saved["session_open_preferences"]
            .get("origin:codex:codex_cli")
            .is_none()
    );
    assert_eq!(
        saved["session_open_preferences"]["origin:claude:terminal"],
        "vscode"
    );
    assert_eq!(saved["unknown"], 7);
    success("service-session-terminal", &["custom", "/tmp/My Terminal"]);
    let target = success("open-session", &[&agent, "terminal", "--dry-run"]);
    assert_eq!(target["terminal"], "custom");
    assert_eq!(target["target"]["args"][1], "a';echo oops;雪");
    assert_eq!(target["target"]["cwd"], "/tmp/project's folder");
    let unsupported = run("open-session", &[&agent, "vscode", "--dry-run"]);
    assert!(!unsupported.status.success());
    assert!(String::from_utf8_lossy(&unsupported.stderr).contains("selected action"));
    let missing = run("open-session", &["does-not-exist", "--dry-run"]);
    assert!(!missing.status.success());
    let saved = success("service-session-preference", &["grok", "terminal"]);
    assert_eq!(saved["grok_session_open_action"], "terminal");
    server.join().unwrap();
    assert_eq!(
        serde_json::from_slice::<Value>(&fs::read(path).unwrap()).unwrap()["unknown"],
        7
    );
}
