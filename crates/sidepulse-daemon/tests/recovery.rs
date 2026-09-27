#![cfg(unix)]

use std::fs;
use std::path::Path;
use std::process::{Child, Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

use sidepulse_core::{ClientRequest, PROTOCOL_VERSION, RequestKind, ServerMessage, ServerPayload};

struct Server(Child);

impl Drop for Server {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

fn snapshot(endpoint: &str) -> Option<sidepulse_core::MonitorSnapshot> {
    let request = ClientRequest {
        version: PROTOCOL_VERSION,
        request_id: 1,
        kind: RequestKind::Snapshot,
    };
    let response: ServerMessage =
        sidepulse_ipc::request(endpoint, &request, Duration::from_millis(300)).ok()?;
    match response.payload {
        ServerPayload::Snapshot { state } => Some(state),
        _ => None,
    }
}

#[test]
fn running_service_recovers_rows_written_without_ipc() {
    let directory = Path::new("/tmp").join(format!(
        "sidepulse-recover-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    fs::create_dir(&directory).unwrap();
    let endpoint = directory.join("s.sock");
    let log = directory.join("claude.jsonl");
    let state = directory.join("latest.json");
    let server = Server(
        Command::new(env!("CARGO_BIN_EXE_sidepulse-next-service"))
            .arg(&endpoint)
            .args(["--log", "claude"])
            .arg(&log)
            .arg("--state")
            .arg(&state)
            .env("HOME", &directory)
            .env("XDG_STATE_HOME", &directory)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap(),
    );
    let endpoint = endpoint.to_str().unwrap();
    let deadline = Instant::now() + Duration::from_secs(5);
    while snapshot(endpoint).is_none() {
        assert!(Instant::now() < deadline, "service did not start");
        thread::sleep(Duration::from_millis(50));
    }
    let now = chrono::Utc::now().to_rfc3339();
    fs::write(
        &log,
        format!(
            "{{\"logged_at\":\"{now}\",\"hook_event_name\":\"PreToolUse\",\"session_id\":\"offline\"}}\n"
        ),
    )
    .unwrap();
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        if snapshot(endpoint).is_some_and(|state| state.aggregate.active_count == 1) {
            break;
        }
        assert!(
            Instant::now() < deadline,
            "service did not recover the log row"
        );
        thread::sleep(Duration::from_millis(100));
    }
    let saved: serde_json::Value = serde_json::from_slice(&fs::read(&state).unwrap()).unwrap();
    assert_eq!(saved["statuses"][0]["agent_id"], "claude:session:offline");
    drop(server);
    fs::remove_dir_all(directory).unwrap();
}

#[test]
fn running_service_recovers_new_codex_transcript() {
    let directory = Path::new("/tmp").join(format!(
        "sidepulse-transcript-recover-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    let transcripts = directory.join("sessions");
    fs::create_dir_all(&transcripts).unwrap();
    let endpoint = directory.join("s.sock");
    let server = Server(
        Command::new(env!("CARGO_BIN_EXE_sidepulse-next-service"))
            .arg(&endpoint)
            .args(["--transcript", "codex"])
            .arg(&transcripts)
            .env("HOME", &directory)
            .env("XDG_STATE_HOME", &directory)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap(),
    );
    let endpoint = endpoint.to_str().unwrap();
    let deadline = Instant::now() + Duration::from_secs(5);
    while snapshot(endpoint).is_none() {
        assert!(Instant::now() < deadline, "service did not start");
        thread::sleep(Duration::from_millis(50));
    }
    let transcript = transcripts.join("rollout-12345678-1234-1234-1234-123456789abc.jsonl");
    fs::write(
        &transcript,
        format!(
            "{}\n",
            serde_json::json!({
                "timestamp": chrono::Utc::now().to_rfc3339(),
                "type": "response_item",
                "payload": {"type": "function_call", "name": "Shell", "call_id": "x"}
            })
        ),
    )
    .unwrap();
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        if snapshot(endpoint).is_some_and(|state| {
            state.aggregate.active_count == 1
                && state.statuses[0].agent_id
                    == "codex:session:12345678-1234-1234-1234-123456789abc"
        }) {
            break;
        }
        assert!(
            Instant::now() < deadline,
            "service did not recover transcript"
        );
        thread::sleep(Duration::from_millis(100));
    }
    drop(server);
    fs::remove_dir_all(directory).unwrap();
}

#[test]
fn service_updates_settings_and_device_from_one_request() {
    let directory = Path::new("/tmp").join(format!(
        "sidepulse-settings-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    let device = directory.join("SidePulse Dot");
    fs::create_dir_all(&device).unwrap();
    let endpoint = directory.join("s.sock");
    let settings_path = directory.join("settings.json");
    fs::write(&settings_path, r#"{"unknown":{"keep":true}}"#).unwrap();
    let server = Server(
        Command::new(env!("CARGO_BIN_EXE_sidepulse-next-service"))
            .arg(&endpoint)
            .arg("--device")
            .arg(&device)
            .arg("--settings")
            .arg(&settings_path)
            .env("HOME", &directory)
            .env("XDG_STATE_HOME", &directory)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap(),
    );
    let endpoint = endpoint.to_str().unwrap();
    let deadline = Instant::now() + Duration::from_secs(5);
    while snapshot(endpoint).is_none() {
        assert!(Instant::now() < deadline, "service did not start");
        thread::sleep(Duration::from_millis(50));
    }
    let request = ClientRequest {
        version: PROTOCOL_VERSION,
        request_id: 7,
        kind: RequestKind::SetBrightness { brightness: 75 },
    };
    let response: ServerMessage =
        sidepulse_ipc::request(endpoint, &request, Duration::from_secs(2)).unwrap();
    let ServerPayload::Settings {
        settings,
        active_device,
        brightness,
    } = response.payload
    else {
        panic!("service did not return updated settings")
    };
    assert_eq!(brightness, Some(75));
    assert_eq!(active_device.as_deref(), device.join("LEDS.LED").to_str());
    assert_eq!(settings["devices"][0]["brightness"], 75);
    assert_eq!(settings["unknown"]["keep"], true);
    let saved: serde_json::Value =
        serde_json::from_slice(&fs::read(&settings_path).unwrap()).unwrap();
    assert_eq!(saved, settings);
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        if fs::read_to_string(device.join("LEDS.LED"))
            .is_ok_and(|program| program.starts_with("brightness 75\n"))
        {
            break;
        }
        assert!(
            Instant::now() < deadline,
            "device did not use updated brightness"
        );
        thread::sleep(Duration::from_millis(100));
    }
    drop(server);
    fs::remove_dir_all(directory).unwrap();
}
