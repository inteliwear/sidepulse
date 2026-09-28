use serde_json::Value;
use std::{fs, process::Command};
#[cfg(unix)]
use std::{
    io::Write,
    process::{Child, Stdio},
    time::{Duration, Instant},
};
#[cfg(unix)]
struct Process(Child);
#[cfg(unix)]
impl Drop for Process {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}
const WORKING: &str =
    "off 320ms cosine\n0:#00E5FF 760ms pulse 0ms; 1:#00E5FF 760ms pulse 260ms\nrepeat";
#[cfg(unix)]
const COMPLETED: &str = "#00FF66 320ms cosine";
fn command(root: &std::path::Path, log: &std::path::Path, device: &std::path::Path) -> Command {
    let mut command = Command::new(env!("CARGO_BIN_EXE_sidepulse-next"));
    command
        .args(["agent-monitor", "leds", "--claude-log"])
        .arg(log)
        .args(["--device"])
        .arg(device)
        .args(["--file-name", "status.LED", "--max-lines", "1"])
        .env("HOME", root)
        .env("USERPROFILE", root)
        .env("XDG_STATE_HOME", root)
        .env_remove("SIDEPULSE_NEXT_ENDPOINT");
    command
}
fn event(name: &str) -> String {
    serde_json::json!({"logged_at":chrono::Utc::now().to_rfc3339(),"hook_event_name":name,"session_id":"session-1","cwd":"/repo","tool_name":"Shell"}).to_string()+"\n"
}
#[test]
fn standalone_leds_preview_and_write_a_captured_python_program() {
    let root = tempfile::tempdir().unwrap();
    let log = root.path().join("claude.jsonl");
    let device = root.path().join("SidePulse Dot");
    fs::create_dir(&device).unwrap();
    fs::write(&log, event("PreToolUse")).unwrap();
    let preview = command(root.path(), &log, &device)
        .args(["--once", "--dry-run"])
        .output()
        .unwrap();
    assert!(
        preview.status.success(),
        "{}",
        String::from_utf8_lossy(&preview.stderr)
    );
    let preview: Value = serde_json::from_slice(&preview.stdout).unwrap();
    assert_eq!(preview["program"], WORKING);
    assert!(!device.join("status.LED").exists());
    let written = command(root.path(), &log, &device)
        .arg("--once")
        .output()
        .unwrap();
    assert!(
        written.status.success(),
        "{}",
        String::from_utf8_lossy(&written.stderr)
    );
    assert_eq!(
        fs::read_to_string(device.join("status.LED")).unwrap(),
        WORKING
    );
    let rejected = command(root.path(), &log, &device)
        .args(["--endpoint", "unrelated", "--once"])
        .output()
        .unwrap();
    assert!(!rejected.status.success());
    assert_eq!(
        fs::read_to_string(device.join("status.LED")).unwrap(),
        WORKING
    );
}
#[cfg(unix)]
#[test]
fn standalone_loop_follows_new_events_and_stops_on_a_signal() {
    let root = tempfile::tempdir().unwrap();
    let log = root.path().join("claude.jsonl");
    let device = root.path().join("SidePulse Dot");
    fs::create_dir(&device).unwrap();
    fs::write(&log, event("PreToolUse")).unwrap();
    let mut child = Process(
        command(root.path(), &log, &device)
            .args(["--interval", "0.1"])
            .stdout(Stdio::null())
            .stderr(Stdio::inherit())
            .spawn()
            .unwrap(),
    );
    let deadline = Instant::now() + Duration::from_secs(5);
    let target = device.join("status.LED");
    while !fs::read_to_string(&target).is_ok_and(|program| program == WORKING) {
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(20));
    }
    fs::OpenOptions::new()
        .append(true)
        .open(&log)
        .unwrap()
        .write_all(event("Stop").as_bytes())
        .unwrap();
    while !fs::read_to_string(&target).is_ok_and(|program| program == COMPLETED) {
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(20));
    }
    assert!(
        Command::new("kill")
            .args(["-TERM", &child.0.id().to_string()])
            .status()
            .unwrap()
            .success()
    );
    while child.0.try_wait().unwrap().is_none() {
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(20));
    }
    assert!(child.0.wait().unwrap().success());
}
