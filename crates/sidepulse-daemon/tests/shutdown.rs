use sidepulse_core::{ClientRequest, PROTOCOL_VERSION, RequestKind, ServerMessage, ServerPayload};
use std::{
    fs,
    process::{Child, Command, Stdio},
    time::{Duration, Instant},
};
struct Process(Child);
impl Drop for Process {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}
fn request(endpoint: &str, kind: RequestKind) -> std::io::Result<ServerMessage> {
    sidepulse_ipc::request(
        endpoint,
        &ClientRequest {
            version: PROTOCOL_VERSION,
            request_id: 1,
            kind,
        },
        Duration::from_secs(1),
    )
}
#[test]
fn ipc_shutdown_flushes_state_and_joins_workers() {
    exercise_shutdown(false);
}
#[cfg(unix)]
#[test]
fn termination_signal_uses_the_same_clean_shutdown_path() {
    exercise_shutdown(true);
}
fn exercise_shutdown(signal: bool) {
    let directory = tempfile::tempdir().unwrap();
    let endpoint = if cfg!(windows) {
        format!("sidepulse-shutdown-{}", std::process::id())
    } else {
        directory
            .path()
            .join("events.sock")
            .to_string_lossy()
            .into_owned()
    };
    let state = directory.path().join("latest.json");
    let settings = directory.path().join("settings.json");
    fs::write(&settings, "{}").unwrap();
    let log = directory.path().join("codex.jsonl");
    let device = directory.path().join("SidePulse Dot");
    fs::create_dir(&device).unwrap();
    let mut process = Process(
        Command::new(env!("CARGO_BIN_EXE_sidepulse-next-service"))
            .env("HOME", directory.path())
            .env("USERPROFILE", directory.path())
            .env("XDG_STATE_HOME", directory.path().join("state"))
            .arg(&endpoint)
            .arg("--state")
            .arg(&state)
            .arg("--settings")
            .arg(&settings)
            .args(["--log", "codex"])
            .arg(&log)
            .arg("--device")
            .arg(&device)
            .stdout(Stdio::null())
            .stderr(Stdio::inherit())
            .spawn()
            .unwrap(),
    );
    let deadline = Instant::now() + Duration::from_secs(10);
    while request(&endpoint, RequestKind::Snapshot).is_err() {
        assert!(Instant::now() < deadline, "service did not start");
        assert!(
            process.0.try_wait().unwrap().is_none(),
            "service exited early"
        );
        std::thread::sleep(Duration::from_millis(50));
    }
    if signal {
        assert!(
            Command::new("kill")
                .args(["-TERM", &process.0.id().to_string()])
                .status()
                .unwrap()
                .success()
        );
    } else {
        assert_eq!(
            request(&endpoint, RequestKind::Shutdown).unwrap().payload,
            ServerPayload::Ack
        );
    }
    while process.0.try_wait().unwrap().is_none() {
        assert!(Instant::now() < deadline, "service did not stop cleanly");
        std::thread::sleep(Duration::from_millis(50));
    }
    assert!(process.0.wait().unwrap().success());
    let saved: serde_json::Value = serde_json::from_slice(&fs::read(&state).unwrap()).unwrap();
    assert!(saved["statuses"].is_array());
    let before = fs::read(device.join("LEDS.LED")).ok();
    fs::write(&log, serde_json::json!({"hook_event":"UserPromptSubmit","session_id":"later","timestamp":chrono::Utc::now().to_rfc3339()}).to_string()).unwrap();
    std::thread::sleep(Duration::from_millis(1200));
    assert_eq!(fs::read(device.join("LEDS.LED")).ok(), before);
    assert!(request(&endpoint, RequestKind::Snapshot).is_err());
}
