use std::fs;
use std::io::Write;
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicUsize, Ordering};

static NEXT_SCRATCH_ID: AtomicUsize = AtomicUsize::new(0);

fn scratch_dir() -> PathBuf {
    let path = std::env::temp_dir().join(format!(
        "sidepulse-hook-test-{}-{}-{}",
        std::process::id(),
        NEXT_SCRATCH_ID.fetch_add(1, Ordering::Relaxed),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    fs::create_dir(&path).unwrap();
    path
}

fn invoke(args: &[&str], input: &str, state_dir: &std::path::Path) -> std::process::Output {
    invoke_with_socket(args, input, state_dir, false)
}

fn invoke_with_socket(
    args: &[&str],
    input: &str,
    state_dir: &std::path::Path,
    socket_enabled: bool,
) -> std::process::Output {
    invoke_executable(
        env!("CARGO_BIN_EXE_sidepulse-next-hook"),
        args,
        input,
        state_dir,
        socket_enabled,
    )
}

fn invoke_executable(
    executable: &str,
    args: &[&str],
    input: &str,
    state_dir: &std::path::Path,
    socket_enabled: bool,
) -> std::process::Output {
    let mut child = Command::new(executable)
        .args(args)
        .env(
            "SIDEPULSE_DISABLE_EVENT_SOCKET",
            if socket_enabled { "0" } else { "1" },
        )
        .env("XDG_STATE_HOME", state_dir)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let written = child.stdin.take().unwrap().write_all(input.as_bytes());
    if let Err(error) = written {
        // Invalid CLI arguments can exit before reading stdin. That is the
        // behavior under test, and its pipe may already be closed.
        assert_eq!(error.kind(), std::io::ErrorKind::BrokenPipe);
    }
    child.wait_with_output().unwrap()
}

#[test]
fn both_cli_hook_routes_share_the_silent_handler() {
    let dir = scratch_dir();
    for (index, prefix) in [
        ["hook-log"].as_slice(),
        ["agent-monitor", "hook-log"].as_slice(),
    ]
    .into_iter()
    .enumerate()
    {
        let log = dir.join(format!("route-{index}.jsonl"));
        let mut args = prefix.to_vec();
        args.extend(["--provider", "claude", "--log", log.to_str().unwrap()]);
        let output = invoke_executable(
            env!("CARGO_BIN_EXE_sidepulse-next"),
            &args,
            r#"{"hook_event_name":"Stop","session_id":"s1"}"#,
            &dir,
            false,
        );
        assert!(output.status.success());
        assert!(output.stdout.is_empty());
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(&fs::read_to_string(log).unwrap()).unwrap()["hook_event_name"],
            "Stop"
        );
    }
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn hook_stays_silent_and_appends_jsonl_for_hostile_input() {
    let dir = scratch_dir();
    let log = dir.join("claude.jsonl");
    let log_arg = log.to_str().unwrap();
    for payload in [
        "",
        "not-json",
        "null",
        "[1,2,3]",
        r#"{"hook_event_name":"Stop","message":"emoji 🚀\nnext"}"#,
        &format!(
            r#"{{"hook_event_name":"Stop","message":"{}"}}"#,
            "x".repeat(500_000)
        ),
    ] {
        let output = invoke(&["--provider", "claude", "--log", log_arg], payload, &dir);
        assert!(output.status.success());
        assert!(output.stdout.is_empty());
    }
    let lines = fs::read_to_string(&log).unwrap();
    assert_eq!(lines.lines().count(), 6);
    for line in lines.lines() {
        serde_json::from_str::<serde_json::Value>(line).unwrap();
    }
    let audit = dir.join("sidepulse/agent-monitor/event-status.jsonl");
    let audit_lines = fs::read_to_string(audit).unwrap();
    assert_eq!(audit_lines.lines().count(), 2);
    for line in audit_lines.lines() {
        let record: serde_json::Value = serde_json::from_str(line).unwrap();
        assert_eq!(record["hook_event"], "Stop");
    }
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn cursor_mapping_and_junie_context_survive_process_boundaries() {
    let dir = scratch_dir();
    let cursor = dir.join("cursor.jsonl");
    let cursor_arg = cursor.to_str().unwrap();
    let output = invoke(
        &[
            "--provider",
            "cursor",
            "--log",
            cursor_arg,
            "--event",
            "beforeShellExecution",
        ],
        r#"{"conversation_id":"abc","workspace_roots":["/repo"],"command":"pwd"}"#,
        &dir,
    );
    assert!(output.status.success());
    assert!(output.stdout.is_empty());
    let line: serde_json::Value =
        serde_json::from_str(&fs::read_to_string(cursor).unwrap()).unwrap();
    assert_eq!(line["hook_event_name"], "PreToolUse");
    assert_eq!(line["session_id"], "abc");
    assert_eq!(line["cwd"], "/repo");

    let junie = dir.join("junie.jsonl");
    let junie_arg = junie.to_str().unwrap();
    invoke(
        &["--provider", "junie", "--log", junie_arg],
        r#"{"hook_event_name":"SessionStart","session_id":"j1","cwd":"/work"}"#,
        &dir,
    );
    invoke(
        &["--provider", "junie", "--log", junie_arg],
        r#"{"hook_event_name":"Stop"}"#,
        &dir,
    );
    let lines = fs::read_to_string(junie).unwrap();
    let final_line: serde_json::Value =
        serde_json::from_str(lines.lines().last().unwrap()).unwrap();
    assert_eq!(final_line["session_id"], "j1");
    assert_eq!(final_line["cwd"], "/work");
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn invalid_arguments_and_unwritable_log_are_fail_open() {
    let dir = scratch_dir();
    let output = invoke(&["--provider"], "{}", &dir);
    assert!(output.status.success());
    assert!(output.stdout.is_empty());
    let absent_endpoint = dir.join("absent.sock");
    let working_log = dir.join("working.jsonl");
    let output = invoke_with_socket(
        &[
            "--provider",
            "claude",
            "--log",
            working_log.to_str().unwrap(),
            "--endpoint",
            absent_endpoint.to_str().unwrap(),
        ],
        r#"{"hook_event_name":"Stop"}"#,
        &dir,
        true,
    );
    assert!(output.status.success());
    assert!(output.stdout.is_empty());
    assert!(working_log.exists());
    let output = invoke(
        &["--provider", "claude", "--log", dir.to_str().unwrap()],
        r#"{"hook_event_name":"Stop"}"#,
        &dir,
    );
    assert!(output.status.success());
    assert!(output.stdout.is_empty());
    fs::remove_dir_all(dir).unwrap();
}

#[cfg(unix)]
#[test]
fn hook_delivers_logged_event_to_service_endpoint() {
    use interprocess::local_socket::prelude::*;
    use sidepulse_core::{
        ClientRequest, PROTOCOL_VERSION, RequestKind, ServerMessage, ServerPayload,
    };
    use std::io::BufReader;
    use std::sync::mpsc;
    use std::time::Duration;

    let dir = scratch_dir();
    let endpoint = PathBuf::from("/tmp").join(format!(
        "sp-hook-{}-{}.sock",
        std::process::id(),
        NEXT_SCRATCH_ID.fetch_add(1, Ordering::Relaxed)
    ));
    let endpoint_arg = endpoint.to_str().unwrap();
    let log = dir.join("claude.jsonl");
    let listener = sidepulse_ipc::bind(endpoint_arg).unwrap();
    let (sent, received) = mpsc::channel();
    std::thread::spawn(move || {
        let mut stream = listener.accept().unwrap();
        let request: ClientRequest =
            sidepulse_ipc::read_message(&mut BufReader::new(&mut stream)).unwrap();
        sidepulse_ipc::write_message(
            &mut stream,
            &ServerMessage {
                version: PROTOCOL_VERSION,
                request_id: Some(request.request_id),
                payload: ServerPayload::Ack,
            },
        )
        .unwrap();
        sent.send(request).unwrap();
    });
    let mut child = Command::new(env!("CARGO_BIN_EXE_sidepulse-next-hook"))
        .args([
            "--provider",
            "claude",
            "--log",
            log.to_str().unwrap(),
            "--endpoint",
            endpoint_arg,
        ])
        .env("XDG_STATE_HOME", &dir)
        .env("SIDEPULSE_DISABLE_EVENT_SOCKET", "0")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    child
        .stdin
        .take()
        .unwrap()
        .write_all(br#"{"hook_event_name":"PreToolUse","session_id":"s1"}"#)
        .unwrap();
    let output = child.wait_with_output().unwrap();
    assert!(output.status.success());
    assert!(output.stdout.is_empty());
    let request = received.recv_timeout(Duration::from_secs(2)).unwrap();
    let RequestKind::IngestHook { provider, line } = request.kind else {
        panic!("hook should ingest")
    };
    assert_eq!(provider, "claude");
    assert_eq!(line["hook_event_name"], "PreToolUse");
    assert!(log.exists());
    fs::remove_dir_all(dir).unwrap();
}
