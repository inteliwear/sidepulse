#![cfg(unix)]

use std::fs;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpListener;
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
fn saved_settings_enable_codex_transcript_recovery() {
    let directory = tempfile::tempdir().unwrap();
    let transcripts = directory.path().join(".codex/sessions");
    fs::create_dir_all(&transcripts).unwrap();
    let settings = directory.path().join("settings.json");
    fs::write(
        &settings,
        r#"{"transcript_monitoring":{"codex":true,"claude":false}}"#,
    )
    .unwrap();
    let endpoint = directory.path().join("s.sock");
    let server = Server(
        Command::new(env!("CARGO_BIN_EXE_sidepulse-next-service"))
            .arg(&endpoint)
            .arg("--settings")
            .arg(&settings)
            .env("HOME", directory.path())
            .env("XDG_STATE_HOME", directory.path())
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
    fs::write(
        transcripts.join("rollout-12345678-1234-1234-1234-123456789abc.jsonl"),
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
            state.statuses.iter().any(|status| {
                status.agent_id == "codex:session:12345678-1234-1234-1234-123456789abc"
            })
        }) {
            break;
        }
        assert!(
            Instant::now() < deadline,
            "saved transcript setting was not used"
        );
        thread::sleep(Duration::from_millis(100));
    }
    drop(server);
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
        ..
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
    let response: ServerMessage = sidepulse_ipc::request(
        endpoint,
        &ClientRequest {
            version: PROTOCOL_VERSION,
            request_id: 8,
            kind: RequestKind::SetDisplayMode {
                mode: "battery".into(),
            },
        },
        Duration::from_secs(2),
    )
    .unwrap();
    let ServerPayload::Settings {
        settings,
        display_mode,
        ..
    } = response.payload
    else {
        panic!("service did not update display mode")
    };
    assert_eq!(display_mode.as_deref(), Some("battery"));
    assert_eq!(settings["devices"][0]["led_display"], "battery");
    assert_eq!(settings["unknown"]["keep"], true);
    drop(server);
    fs::remove_dir_all(directory).unwrap();
}

#[test]
fn service_lists_and_selects_mounted_devices() {
    let directory = tempfile::tempdir().unwrap();
    let mounts = directory.path().join("mounts");
    let first = mounts.join("SidePulseDot A");
    let second = mounts.join("SidePulseDot B");
    fs::create_dir_all(&first).unwrap();
    fs::create_dir_all(&second).unwrap();
    fs::write(first.join("LEDS.LED"), "off").unwrap();
    fs::write(second.join("LEDS.LED"), "off").unwrap();
    let endpoint = directory.path().join("s.sock");
    let server = Server(
        Command::new(env!("CARGO_BIN_EXE_sidepulse-next-service"))
            .arg(&endpoint)
            .arg("--auto-device")
            .env("SIDEPULSE_MOUNT_ROOTS", &mounts)
            .env("HOME", directory.path())
            .env("XDG_STATE_HOME", directory.path())
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
        request_id: 9,
        kind: RequestKind::Devices,
    };
    let reply: ServerMessage =
        sidepulse_ipc::request(endpoint, &request, Duration::from_secs(2)).unwrap();
    let ServerPayload::Devices {
        devices,
        active_device,
    } = reply.payload
    else {
        panic!("service did not list devices")
    };
    assert_eq!(devices.len(), 2);
    assert_eq!(active_device.as_deref(), first.join("LEDS.LED").to_str());
    let request = ClientRequest {
        version: PROTOCOL_VERSION,
        request_id: 10,
        kind: RequestKind::SelectDevice {
            root: second.to_string_lossy().into_owned(),
        },
    };
    let reply: ServerMessage =
        sidepulse_ipc::request(endpoint, &request, Duration::from_secs(2)).unwrap();
    let ServerPayload::Devices { active_device, .. } = reply.payload else {
        panic!("service did not select device")
    };
    assert_eq!(active_device.as_deref(), second.join("LEDS.LED").to_str());
    drop(server);
}

#[test]
fn service_ingests_remote_relay_event_through_ipc() {
    let directory = tempfile::tempdir().unwrap();
    let endpoint = directory.path().join("s.sock");
    let server = Server(
        Command::new(env!("CARGO_BIN_EXE_sidepulse-next-service"))
            .arg(&endpoint)
            .env("HOME", directory.path())
            .env("XDG_STATE_HOME", directory.path())
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
    let message = serde_json::json!({
        "v": 1,
        "type": "agent_event",
        "event_id": "remote-1",
        "source": {"name": "Laptop"},
        "provider": "claude",
        "line": {
            "hook_event_name": "PreToolUse",
            "session_id": "remote-session",
            "logged_at": chrono::Utc::now().to_rfc3339()
        }
    });
    let request = ClientRequest {
        version: PROTOCOL_VERSION,
        request_id: 11,
        kind: RequestKind::IngestRelay {
            message: message.clone(),
        },
    };
    let reply: ServerMessage =
        sidepulse_ipc::request(endpoint, &request, Duration::from_secs(2)).unwrap();
    assert!(matches!(reply.payload, ServerPayload::Ack));
    let state = snapshot(endpoint).unwrap();
    assert_eq!(
        state.statuses[0].agent_id,
        "claude:agent:relay:Laptop:remote-session"
    );
    assert_eq!(state.statuses[0].origin.as_deref(), Some("Laptop"));
    let mut duplicate = message;
    duplicate["line"]["hook_event_name"] = "Stop".into();
    let duplicate = ClientRequest {
        version: PROTOCOL_VERSION,
        request_id: 12,
        kind: RequestKind::IngestRelay { message: duplicate },
    };
    let reply: ServerMessage =
        sidepulse_ipc::request(endpoint, &duplicate, Duration::from_secs(2)).unwrap();
    assert!(matches!(reply.payload, ServerPayload::Ack));
    assert_eq!(
        snapshot(endpoint).unwrap().statuses[0].mode,
        state.statuses[0].mode
    );
    drop(server);
}

#[test]
fn opt_in_relay_publishes_local_hook_to_configured_bridge() {
    let directory = tempfile::tempdir().unwrap();
    let endpoint = directory.path().join("s.sock");
    let config_path = directory.path().join("relay.json");
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let server_url = format!("http://{}", listener.local_addr().unwrap());
    let config = sidepulse_relay::RelayConfig::default_for_host("Desktop")
        .with_outbound_channel(&"a".repeat(22), &server_url)
        .unwrap();
    sidepulse_relay::save_config(&config_path, &config).unwrap();
    let server = Server(
        Command::new(env!("CARGO_BIN_EXE_sidepulse-next-service"))
            .arg(&endpoint)
            .arg("--relay-config")
            .arg(&config_path)
            .env("HOME", directory.path())
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
        request_id: 13,
        kind: RequestKind::IngestHook {
            provider: "claude".into(),
            line: serde_json::json!({
                "hook_event_name": "PreToolUse",
                "session_id": "local-session",
                "logged_at": chrono::Utc::now().to_rfc3339(),
            }),
        },
    };
    let reply: ServerMessage =
        sidepulse_ipc::request(endpoint, &request, Duration::from_secs(2)).unwrap();
    assert!(matches!(reply.payload, ServerPayload::Ack));
    let stream = loop {
        match listener.accept() {
            Ok((stream, _)) => break stream,
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                assert!(Instant::now() < deadline, "relay was not published");
                thread::sleep(Duration::from_millis(20));
            }
            Err(error) => panic!("relay listener failed: {error}"),
        }
    };
    stream.set_nonblocking(false).unwrap();
    stream
        .set_read_timeout(Some(Duration::from_secs(2)))
        .unwrap();
    let mut reader = BufReader::new(stream);
    let mut request_line = String::new();
    reader.read_line(&mut request_line).unwrap();
    assert!(request_line.starts_with("POST /api/leds/"));
    let mut body_length = 0;
    loop {
        let mut line = String::new();
        reader.read_line(&mut line).unwrap();
        if line == "\r\n" {
            break;
        }
        if let Some(value) = line.to_ascii_lowercase().strip_prefix("content-length:") {
            body_length = value.trim().parse::<usize>().unwrap();
        }
    }
    let mut body = vec![0; body_length];
    reader.read_exact(&mut body).unwrap();
    reader
        .get_mut()
        .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
        .unwrap();
    let message: serde_json::Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(message["provider"], "claude");
    assert_eq!(message["line"]["session_id"], "local-session");
    drop(server);
}

#[test]
fn opt_in_relay_receives_remote_event_from_bridge() {
    let directory = tempfile::tempdir().unwrap();
    let endpoint = directory.path().join("s.sock");
    let config_path = directory.path().join("relay.json");
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let mut config = sidepulse_relay::RelayConfig::default_for_host("Desktop")
        .with_receiver_channel()
        .unwrap();
    config.server = format!("http://{}", listener.local_addr().unwrap());
    sidepulse_relay::save_config(&config_path, &config).unwrap();
    let server = Server(
        Command::new(env!("CARGO_BIN_EXE_sidepulse-next-service"))
            .arg(&endpoint)
            .arg("--relay-config")
            .arg(&config_path)
            .env("HOME", directory.path())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap(),
    );
    let deadline = Instant::now() + Duration::from_secs(5);
    let stream = loop {
        match listener.accept() {
            Ok((stream, _)) => break stream,
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                assert!(Instant::now() < deadline, "relay was not connected");
                thread::sleep(Duration::from_millis(20));
            }
            Err(error) => panic!("relay listener failed: {error}"),
        }
    };
    stream.set_nonblocking(false).unwrap();
    stream
        .set_read_timeout(Some(Duration::from_secs(2)))
        .unwrap();
    let mut reader = BufReader::new(stream);
    let mut request_line = String::new();
    reader.read_line(&mut request_line).unwrap();
    assert!(request_line.starts_with("GET /api/leds/"));
    loop {
        let mut line = String::new();
        reader.read_line(&mut line).unwrap();
        if line == "\r\n" {
            break;
        }
    }
    let message = serde_json::json!({
        "v": 1, "type": "agent_event", "event_id": "remote-inbound",
        "source": {"name": "Laptop"}, "provider": "claude",
        "line": {"hook_event_name": "PreToolUse", "session_id": "remote-inbound", "logged_at": chrono::Utc::now().to_rfc3339()}
    });
    let body = format!("data: {message}\n\n");
    reader.get_mut().write_all(format!("HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\n\r\n{body}", body.len()).as_bytes()).unwrap();
    let endpoint = endpoint.to_str().unwrap();
    loop {
        if snapshot(endpoint).is_some_and(|state| {
            state
                .statuses
                .iter()
                .any(|status| status.agent_id == "claude:agent:relay:Laptop:remote-inbound")
        }) {
            break;
        }
        assert!(
            Instant::now() < deadline,
            "remote relay event did not reach the service"
        );
        thread::sleep(Duration::from_millis(50));
    }
    drop(server);
}

#[cfg(target_os = "macos")]
#[test]
fn service_reports_read_only_mac_power_state() {
    let directory = tempfile::tempdir().unwrap();
    let endpoint = directory.path().join("s.sock");
    let server = Server(
        Command::new(env!("CARGO_BIN_EXE_sidepulse-next-service"))
            .arg(&endpoint)
            .env("HOME", directory.path())
            .env("XDG_STATE_HOME", directory.path())
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
        request_id: 14,
        kind: RequestKind::Power,
    };
    let reply: ServerMessage =
        sidepulse_ipc::request(endpoint, &request, Duration::from_secs(3)).unwrap();
    assert!(matches!(reply.payload, ServerPayload::Power { .. }));
    drop(server);
}
