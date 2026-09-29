//! Process-boundary regression for live state retention and redirected output.
use std::{
    fs::{self, OpenOptions},
    io::{BufRead, BufReader, Write},
    process::{Child, Command, Stdio},
    sync::mpsc,
    time::{Duration, Instant},
};

struct Process(Child);
impl Drop for Process {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

fn wait_for_success(child: &mut Child) {
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        if let Some(status) = child.try_wait().unwrap() {
            assert!(status.success(), "unexpected exit: {status}");
            return;
        }
        assert!(Instant::now() < deadline, "command did not stop");
        std::thread::sleep(Duration::from_millis(20));
    }
}

fn event(name: &str) -> String {
    serde_json::json!({"logged_at":chrono::Utc::now().to_rfc3339(),"hook_event_name":name,"session_id":"pending-permission","tool_name":"Shell","tool_input":{"command":"date"}}).to_string()+"\n"
}

#[test]
fn watch_retains_permissions_beyond_replay_limit_and_redirects_plain_text() {
    let root = tempfile::tempdir().unwrap();
    let log = root.path().join("claude.jsonl");
    fs::write(&log, event("PermissionRequest")).unwrap();
    let mut child = Process(
        Command::new(env!("CARGO_BIN_EXE_sidepulse-next"))
            .args([
                "agent-monitor",
                "live",
                "--interval",
                "0.1",
                "--max-lines",
                "1",
                "--claude-log",
            ])
            .arg(&log)
            .env("HOME", root.path())
            .env("USERPROFILE", root.path())
            .env("XDG_STATE_HOME", root.path())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .unwrap(),
    );
    let stdout = child.0.stdout.take().unwrap();
    let (sent, received) = mpsc::channel();
    let reader = std::thread::spawn(move || {
        for line in BufReader::new(stdout).lines() {
            let line = line.unwrap();
            assert!(
                !line.contains('\x1b'),
                "redirected output must contain no cursor escapes"
            );
            if line.starts_with("Aggregate:") && sent.send(line).is_err() {
                break;
            }
        }
    });
    let first = received.recv_timeout(Duration::from_secs(10)).unwrap();
    assert!(first.contains("Waiting for Input"), "{first}");
    let mut file = OpenOptions::new().append(true).open(&log).unwrap();
    for _ in 0..4 {
        file.write_all(event("PreToolUse").as_bytes()).unwrap();
    }
    file.sync_all().unwrap();
    // One replay row would lose the permission event. Live state must retain it.
    for _ in 0..3 {
        let line = received.recv_timeout(Duration::from_secs(10)).unwrap();
        assert!(line.contains("Waiting for Input"), "{line}");
    }
    file.write_all(event("PostToolUse").as_bytes()).unwrap();
    file.sync_all().unwrap();
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        let line = received
            .recv_timeout(deadline.saturating_duration_since(Instant::now()))
            .unwrap();
        if line.starts_with("Aggregate: Working") {
            break;
        }
    }
    #[cfg(unix)]
    {
        assert!(
            Command::new("kill")
                .args(["-TERM", &child.0.id().to_string()])
                .status()
                .unwrap()
                .success()
        );
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            if let Some(status) = child.0.try_wait().unwrap() {
                assert!(status.success());
                break;
            }
            assert!(
                Instant::now() < deadline,
                "watch did not stop after SIGTERM"
            );
            std::thread::sleep(Duration::from_millis(20));
        }
    }
    #[cfg(windows)]
    {
        child.0.kill().unwrap();
        child.0.wait().unwrap();
    }
    reader.join().unwrap();
}

#[test]
fn zero_recent_window_keeps_older_agents_and_closed_output_stops_watch() {
    let root = tempfile::tempdir().unwrap();
    let log = root.path().join("claude.jsonl");
    let mut row: serde_json::Value = serde_json::from_str(&event("PermissionRequest")).unwrap();
    row["logged_at"] = (chrono::Utc::now() - chrono::Duration::minutes(2))
        .to_rfc3339()
        .into();
    fs::write(&log, format!("{row}\n")).unwrap();
    let mut child = Process(
        Command::new(env!("CARGO_BIN_EXE_sidepulse-next"))
            .args([
                "agent-monitor",
                "live",
                "--interval",
                "0.1",
                "--recent-seconds",
                "0",
                "--stale-after",
                "3600",
                "--claude-log",
            ])
            .arg(&log)
            .env("HOME", root.path())
            .env("USERPROFILE", root.path())
            .env("XDG_STATE_HOME", root.path())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap(),
    );
    let stdout = child.0.stdout.take().unwrap();
    let (sent, received) = mpsc::channel();
    let reader = std::thread::spawn(move || {
        for line in BufReader::new(stdout).lines() {
            if sent.send(line.unwrap()).is_err() {
                break;
            }
        }
    });
    loop {
        if received.recv_timeout(Duration::from_secs(10)).unwrap() == "Agents:" {
            break;
        }
    }
    let agent = received.recv_timeout(Duration::from_secs(10)).unwrap();
    assert!(
        agent.contains("Waiting for Input") && agent.contains("event=PermissionRequest"),
        "{agent}"
    );
    // Simulate a reader such as `head` closing the pipe after its desired output.
    drop(received);
    wait_for_success(&mut child.0);
    reader.join().unwrap();
    let stderr = child.0.stderr.take().unwrap();
    assert!(
        BufReader::new(stderr).lines().next().is_none(),
        "closed output should not print a panic"
    );
}
