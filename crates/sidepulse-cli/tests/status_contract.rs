use std::fs;
use std::path::PathBuf;
use std::process::Command;
use std::sync::atomic::{AtomicUsize, Ordering};

use serde_json::Value;

static NEXT_ID: AtomicUsize = AtomicUsize::new(0);

#[test]
fn status_and_live_help_do_not_start_monitoring() {
    let dir = tempfile::tempdir().unwrap();
    for (route, live) in [
        (vec!["status", "--json", "--help"], false),
        (vec!["agent-monitor", "status", "-h"], false),
        (vec!["watch", "--help"], true),
        (vec!["agent-monitor", "live", "--no-color", "-h"], true),
    ] {
        let output = Command::new(env!("CARGO_BIN_EXE_sidepulse-next"))
            .args(route)
            .env("HOME", dir.path())
            .env("USERPROFILE", dir.path())
            .env("XDG_STATE_HOME", dir.path())
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        let help = String::from_utf8(output.stdout).unwrap();
        assert!(
            help.contains("Usage:")
                && help.contains("--claude-log")
                && help.contains("--codex-transcripts"),
            "{help}"
        );
        assert_eq!(help.contains("--recent-seconds"), live);
        assert_eq!(help.contains("--json"), !live);
        assert!(dir.path().read_dir().unwrap().next().is_none());
    }
}

fn scratch_dir() -> PathBuf {
    let path = std::env::temp_dir().join(format!(
        "sidepulse-status-test-{}-{}",
        std::process::id(),
        NEXT_ID.fetch_add(1, Ordering::Relaxed)
    ));
    fs::create_dir_all(&path).unwrap();
    path
}

#[test]
fn both_status_routes_expose_the_legacy_json_shape() {
    let dir = scratch_dir();
    let log = dir.join("claude.jsonl");
    let now = chrono::Utc::now().to_rfc3339();
    fs::write(
        &log,
        format!(
            "{{\"logged_at\":\"{now}\",\"hook_event_name\":\"PreToolUse\",\"session_id\":\"session-1\",\"tool_name\":\"Shell\",\"cwd\":\"/repo\"}}\n"
        ),
    )
    .unwrap();
    for route in [
        ["status"].as_slice(),
        ["agent-monitor", "status"].as_slice(),
    ] {
        let output = Command::new(env!("CARGO_BIN_EXE_sidepulse-next"))
            .args(route)
            .args(["--json", "--claude-log", log.to_str().unwrap()])
            .env("HOME", &dir)
            .env("XDG_STATE_HOME", &dir)
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        let value: Value = serde_json::from_slice(&output.stdout).unwrap();
        assert_eq!(value["aggregate"]["mode"], "tool_running");
        assert_eq!(value["aggregate"]["mode_label"], "Tool Running");
        assert_eq!(value["aggregate"]["active_count"], 1);
        assert_eq!(value["statuses"][0]["agent_id"], "claude:session:session-1");
        assert_eq!(value["statuses"][0]["priority"], 3);
        assert_eq!(value["statuses"][0]["tool_name"], "Shell");
        assert_eq!(value["statuses"][0]["cwd"], "/repo");
        assert!(value["statuses"][0]["age_seconds"].is_number());
        assert!(value["collected_at"].is_string());
        let sources = value["sources"].as_array().unwrap();
        assert_eq!(sources.len(), 4);
        assert!(sources.iter().all(|source| source["provider"] != "cursor"));
        assert!(value["stale_statuses"].is_array());
    }
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn max_lines_and_missing_logs_are_handled() {
    let dir = scratch_dir();
    let log = dir.join("claude.jsonl");
    let now = chrono::Utc::now().to_rfc3339();
    fs::write(
        &log,
        format!(
            "{{\"logged_at\":\"{now}\",\"hook_event_name\":\"PreToolUse\",\"session_id\":\"old\"}}\n{{\"logged_at\":\"{now}\",\"hook_event_name\":\"Stop\",\"session_id\":\"new\"}}\n"
        ),
    )
    .unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_sidepulse-next"))
        .args([
            "status",
            "--json",
            "--max-lines",
            "1",
            "--claude-log",
            log.to_str().unwrap(),
        ])
        .env("HOME", &dir)
        .env("XDG_STATE_HOME", &dir)
        .output()
        .unwrap();
    assert!(output.status.success());
    let value: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(value["aggregate"]["active_count"], 0);
    assert_eq!(value["statuses"].as_array().unwrap().len(), 1);
    assert_eq!(value["statuses"][0]["agent_id"], "claude:session:new");

    let invalid = Command::new(env!("CARGO_BIN_EXE_sidepulse-next"))
        .args(["status", "--max-lines", "oops"])
        .output()
        .unwrap();
    assert_eq!(invalid.status.code(), Some(2));
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn finds_existing_log_paths_from_provider_configs() {
    let dir = scratch_dir();
    let claude_log = dir.join("custom claude.jsonl");
    let codex_log = dir.join("codex-custom.jsonl");
    let now = chrono::Utc::now().to_rfc3339();
    fs::write(
        &claude_log,
        format!(
            "{{\"logged_at\":\"{now}\",\"hook_event_name\":\"PreToolUse\",\"session_id\":\"c1\"}}\n"
        ),
    )
    .unwrap();
    fs::write(
        &codex_log,
        format!("{{\"logged_at\":\"{now}\",\"event\":{{\"hook_event_name\":\"Stop\",\"session_id\":\"x1\"}}}}\n"),
    )
    .unwrap();
    fs::create_dir_all(dir.join(".claude")).unwrap();
    fs::create_dir_all(dir.join(".codex")).unwrap();
    fs::write(
        dir.join(".claude/settings.json"),
        serde_json::json!({"hooks":{"PreToolUse":[{"hooks":[{"command":format!("sidepulse hook-log --provider claude --log '{}'", claude_log.display())}]}]}}).to_string(),
    )
    .unwrap();
    fs::write(
        dir.join(".codex/config.toml"),
        format!("[features]\nhooks = true\n[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ncommand = 'sidepulse hook-log --provider codex --log {}'\n", codex_log.display()),
    )
    .unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_sidepulse-next"))
        .args(["status", "--json"])
        .env("HOME", &dir)
        .env("XDG_STATE_HOME", &dir)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let value: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(value["sources"][0]["path"], codex_log.to_str().unwrap());
    assert_eq!(value["sources"][1]["path"], claude_log.to_str().unwrap());
    assert_eq!(value["aggregate"]["mode"], "tool_running");
    assert_eq!(value["aggregate"]["active_count"], 1);
    assert_eq!(value["stale_statuses"].as_array().unwrap().len(), 1);
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn status_can_include_explicit_transcript_source() {
    let dir = scratch_dir();
    let transcripts = dir.join("sessions");
    fs::create_dir_all(&transcripts).unwrap();
    let path = transcripts.join("rollout-12345678-1234-1234-1234-123456789abc.jsonl");
    fs::write(
        path,
        format!(
            "{}\n",
            serde_json::json!({
                "timestamp": chrono::Utc::now().to_rfc3339(),
                "type": "response_item",
                "payload": {"type": "function_call", "name": "Shell"}
            })
        ),
    )
    .unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_sidepulse-next"))
        .args([
            "status",
            "--json",
            "--codex-transcripts",
            transcripts.to_str().unwrap(),
        ])
        .env("HOME", &dir)
        .env("XDG_STATE_HOME", &dir)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let value: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(value["aggregate"]["mode"], "tool_running");
    assert_eq!(
        value["statuses"][0]["agent_id"],
        "codex:session:12345678-1234-1234-1234-123456789abc"
    );
    assert_eq!(value["sources"].as_array().unwrap().len(), 5);
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn status_text_and_json_exit_cleanly_when_output_is_closed() {
    let dir = tempfile::tempdir().unwrap();
    let log = dir.path().join("claude.jsonl");
    let now = chrono::Utc::now().to_rfc3339();
    let rows = (0..2000).map(|index| serde_json::json!({
        "logged_at":now,"hook_event_name":"PreToolUse","session_id":format!("pipe-{index}"),"tool_name":"Shell"
    }).to_string()+"\n").collect::<String>();
    fs::write(&log, rows).unwrap();
    for json in [false, true] {
        let mut command = Command::new(env!("CARGO_BIN_EXE_sidepulse-next"));
        command
            .args(["status", "--claude-log"])
            .arg(&log)
            .env("HOME", dir.path())
            .env("USERPROFILE", dir.path())
            .env("XDG_STATE_HOME", dir.path())
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped());
        if json {
            command.arg("--json");
        }
        let mut child = command.spawn().unwrap();
        drop(child.stdout.take());
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(10);
        let status = loop {
            if let Some(status) = child.try_wait().unwrap() {
                break status;
            }
            if std::time::Instant::now() >= deadline {
                let _ = child.kill();
                let _ = child.wait();
                panic!("status hung after output closed");
            }
            std::thread::sleep(std::time::Duration::from_millis(20));
        };
        assert!(status.success(), "json={json}: {status}");
        use std::io::Read;
        let mut error = String::new();
        child
            .stderr
            .take()
            .unwrap()
            .read_to_string(&mut error)
            .unwrap();
        assert!(error.is_empty(), "{error}");
    }
}
