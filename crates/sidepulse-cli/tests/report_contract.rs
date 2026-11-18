//! `report --json` reaches a real service and changes its session state.

use interprocess::local_socket::prelude::*;
use serde_json::{Value, json};
use std::io::Write;
use std::process::{Command, Stdio};
use std::time::{SystemTime, UNIX_EPOCH};

fn report(endpoint: &str, body: &Value) -> std::process::Output {
    let mut child = Command::new(env!("CARGO_BIN_EXE_sidepulse-next"))
        .args(["report", "--json"])
        .env("SIDEPULSE_NEXT_ENDPOINT", endpoint)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    child
        .stdin
        .take()
        .unwrap()
        .write_all(body.to_string().as_bytes())
        .unwrap();
    child.wait_with_output().unwrap()
}

#[test]
fn program_status_reports_drive_the_service_session() {
    #[cfg(unix)]
    let directory = tempfile::tempdir_in("/tmp").unwrap();
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
    let endpoint = format!("sidepulse-report-{id}");
    let service = std::sync::Arc::new(sidepulse_daemon::Service::new());
    let listener = sidepulse_ipc::bind(&endpoint).unwrap();
    let served = service.clone();
    let server = std::thread::spawn(move || {
        for _ in 0..3 {
            served.serve_connection(listener.accept().unwrap()).unwrap();
        }
    });
    let body = |state: &str| {
        json!({"v":1,"type":"program_status","scope":"opencode-s1",
            "status":{"state":state,"app":"opencode"},
            "context":{"provider":"opencode","session_id":"s1","cwd":"/tmp/project"}})
    };

    let output = report(&endpoint, &body("blocked"));
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(
        serde_json::from_slice::<Value>(&output.stdout).unwrap(),
        json!({"ok":true})
    );
    let snapshot = service.snapshot().unwrap();
    assert_eq!(
        snapshot.aggregate.mode,
        sidepulse_core::AgentMode::WaitingForInput
    );
    assert_eq!(snapshot.statuses[0].provider, "opencode");

    assert!(report(&endpoint, &body("working")).status.success());
    assert_eq!(
        service.snapshot().unwrap().aggregate.mode,
        sidepulse_core::AgentMode::Working
    );

    assert!(report(&endpoint, &body("clear")).status.success());
    let cleared = service.snapshot().unwrap().aggregate;
    assert_eq!(cleared.mode, sidepulse_core::AgentMode::IdleReady);
    assert_eq!(cleared.active_count, 0);
    server.join().unwrap();

    let rejected = report(&endpoint, &json!({"v":1,"type":"program_status"}));
    assert!(!rejected.status.success());
}
