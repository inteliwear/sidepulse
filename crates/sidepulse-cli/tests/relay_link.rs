use std::fs;
use std::process::Command;

use sidepulse_relay::{DEFAULT_BRIDGE_SERVER, load_config};

#[test]
fn link_command_creates_receiver_and_configures_source_without_installing() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("relay.json");
    let binary = env!("CARGO_BIN_EXE_sidepulse-next");
    let first = Command::new(binary)
        .args(["link", "--config"])
        .arg(&path)
        .output()
        .unwrap();
    assert!(
        first.status.success(),
        "{}",
        String::from_utf8_lossy(&first.stderr)
    );
    let receiver = load_config(&path, "Host").unwrap().receiver_channel;
    assert_eq!(receiver.len(), 22);
    let second = Command::new(binary)
        .args(["link", "--config"])
        .arg(&path)
        .output()
        .unwrap();
    assert!(second.status.success());
    assert_eq!(
        load_config(&path, "Host").unwrap().receiver_channel,
        receiver
    );
    let source = Command::new(binary)
        .args(["link", &receiver, "--config"])
        .arg(&path)
        .output()
        .unwrap();
    assert!(
        source.status.success(),
        "{}",
        String::from_utf8_lossy(&source.stderr)
    );
    let config = load_config(&path, "Host").unwrap();
    assert_eq!(config.outbound_channel, receiver);
    assert_eq!(config.server, DEFAULT_BRIDGE_SERVER);

    let leading_dash = format!("-{}", "a".repeat(21));
    let dashed = Command::new(binary)
        .args(["link", &leading_dash, "--config"])
        .arg(&path)
        .output()
        .unwrap();
    assert!(
        dashed.status.success(),
        "{}",
        String::from_utf8_lossy(&dashed.stderr)
    );
    assert_eq!(
        load_config(&path, "Host").unwrap().outbound_channel,
        leading_dash
    );
}

#[test]
fn live_link_configuration_uses_service_ipc_and_can_reload_external_changes() {
    use interprocess::local_socket::prelude::*;
    use sidepulse_core::{
        ClientRequest, PROTOCOL_VERSION, RelaySettingsPatch, RequestKind, ServerMessage,
        ServerPayload,
    };
    use std::time::{Duration, SystemTime, UNIX_EPOCH};
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
        .join(format!("r-{id}.sock"))
        .to_string_lossy()
        .into_owned();
    #[cfg(windows)]
    let endpoint = format!("sidepulse-link-{id}");
    let path = directory.path().join("relay.json");
    fs::write(&path, r#"{"version":1,"unknown":7}"#).unwrap();
    let service = sidepulse_daemon::Service::new();
    service.configure_relay(&path).unwrap();
    let listener = sidepulse_ipc::bind(&endpoint).unwrap();
    let server = std::thread::spawn(move || {
        for _ in 0..7 {
            service
                .serve_connection(listener.accept().unwrap())
                .unwrap();
        }
    });
    let run = |command: &str, args: &[&str]| {
        let mut process = Command::new(env!("CARGO_BIN_EXE_sidepulse-next"));
        process.arg(command);
        if command == "link" {
            process.args(["--endpoint", &endpoint]);
        } else {
            process.arg(&endpoint);
        }
        process.args(args).output().unwrap()
    };
    let result = run("link", &[]);
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    let saved: serde_json::Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
    let receiver = saved["receiver_channel"].as_str().unwrap().to_owned();
    assert_eq!(receiver.len(), 22);
    assert!(String::from_utf8_lossy(&result.stdout).contains(&receiver));
    let code = "a".repeat(22);
    let result = run("link", &[&code, "--server", "http://127.0.0.1:7777"]);
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    let result = run("service-relay-settings", &[]);
    assert!(result.status.success());
    let state: serde_json::Value = serde_json::from_slice(&result.stdout).unwrap();
    assert_eq!(state["settings"]["outbound_code"], code);
    let request = ClientRequest {
        version: PROTOCOL_VERSION,
        request_id: 1,
        kind: RequestKind::SetRelaySettings {
            patch: RelaySettingsPatch {
                rotate_receiver: true,
                ..Default::default()
            },
        },
    };
    let reply: ServerMessage =
        sidepulse_ipc::request(&endpoint, &request, Duration::from_secs(2)).unwrap();
    let ServerPayload::RelaySettings { settings } = reply.payload else {
        panic!("missing relay settings");
    };
    assert_ne!(settings.receiver_code, receiver);
    assert_eq!(settings.outbound_code, code);
    let mut external: serde_json::Value =
        serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
    external["external"] = serde_json::json!(true);
    fs::write(&path, external.to_string()).unwrap();
    let result = run("link", &[&"b".repeat(22)]);
    assert!(!result.status.success());
    assert!(String::from_utf8_lossy(&result.stderr).contains("changed externally"));
    assert!(run("service-relay-reload", &[]).status.success());
    assert!(run("link", &[&"b".repeat(22)]).status.success());
    server.join().unwrap();
    let saved: serde_json::Value = serde_json::from_slice(&fs::read(path).unwrap()).unwrap();
    assert_eq!(saved["unknown"], 7);
    assert_eq!(saved["external"], true);
}
