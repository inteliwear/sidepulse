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
