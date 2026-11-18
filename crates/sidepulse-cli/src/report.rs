//! `report --json`: submit one program_status update (docs/protocol/program-status.md).
//! The update becomes a hook line with an explicit `sidepulse_status`, so the
//! service accounts it like any provider hook. Only JSON input is supported.

use std::io::{self, Read};
use std::process::ExitCode;
use std::time::Duration;

use chrono::Utc;
use serde_json::{Value, json};
use sidepulse_core::{ClientRequest, PROTOCOL_VERSION, RequestKind, ServerMessage, ServerPayload};

const MAX_STDIN_BYTES: u64 = 64 * 1024;
const USAGE: &str = "usage: sidepulse-next report --json [--endpoint ENDPOINT] < STATUS_JSON";

pub fn run_report(args: impl Iterator<Item = String>) -> ExitCode {
    let (mut json_mode, mut endpoint) = (false, None);
    let mut args = args;
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--json" => json_mode = true,
            "--endpoint" => match args.next() {
                Some(value) => endpoint = Some(value),
                None => return usage(),
            },
            _ => return usage(),
        }
    }
    if !json_mode {
        return usage();
    }
    let mut input = String::new();
    if let Err(error) = io::stdin()
        .take(MAX_STDIN_BYTES + 1)
        .read_to_string(&mut input)
    {
        return fail(&format!("cannot read stdin: {error}"));
    }
    if input.len() as u64 > MAX_STDIN_BYTES {
        return fail("report exceeds 64 KiB");
    }
    let report: Value = match serde_json::from_str(&input) {
        Ok(value) => value,
        Err(error) => return fail(&format!("invalid JSON: {error}")),
    };
    let (provider, line) = match report_to_hook_line(&report) {
        Ok(converted) => converted,
        Err(message) => return fail(message),
    };
    let Some(endpoint) = endpoint.or_else(crate::preview_endpoint) else {
        return fail("provide --endpoint ENDPOINT, or set SIDEPULSE_NEXT_ENDPOINT");
    };
    let request = ClientRequest {
        version: PROTOCOL_VERSION,
        request_id: 1,
        kind: RequestKind::IngestHook { provider, line },
    };
    match sidepulse_ipc::request::<_, ServerMessage>(&endpoint, &request, Duration::from_secs(1)) {
        Ok(ServerMessage {
            payload: ServerPayload::Ack,
            ..
        }) => {
            println!("{}", json!({"ok": true}));
            ExitCode::SUCCESS
        }
        Ok(ServerMessage {
            payload: ServerPayload::Error { message, .. },
            ..
        }) => fail(&message),
        Ok(_) => fail("unexpected service response"),
        Err(error) => fail(&format!("service unavailable at {endpoint}: {error}")),
    }
}

/// Validates a program_status report and converts it to `(provider, hook line)`.
pub fn report_to_hook_line(report: &Value) -> Result<(String, Value), &'static str> {
    if report.get("v").and_then(Value::as_u64) != Some(1) {
        return Err("unsupported report version");
    }
    if report.get("type").and_then(Value::as_str) != Some("program_status") {
        return Err("report type must be program_status");
    }
    let scope = report
        .get("scope")
        .and_then(Value::as_str)
        .filter(|scope| valid_text(scope, 128))
        .ok_or("scope must be nonempty text of at most 128 bytes")?;
    let status = report
        .get("status")
        .and_then(Value::as_object)
        .ok_or("status must be an object")?;
    let state = status.get("state").and_then(Value::as_str).unwrap_or("");
    // `blocked` waits for the user; `error` is the only failure state.
    let (mode, event) = match state {
        "working" => ("working", "UserPromptSubmit"),
        "blocked" => ("waiting_for_input", "UserPromptSubmit"),
        "error" => ("blocked_error", "UserPromptSubmit"),
        "done" => ("completed", "Stop"),
        // Idle is never visible (idle_visible_seconds is 0), so it also clears the row.
        "idle" | "clear" => ("idle_ready", "SessionStart"),
        _ => return Err("unknown status state"),
    };
    let msg = match status.get("msg") {
        None | Some(Value::Null) => None,
        Some(Value::String(msg)) if valid_text(msg, 2048) => Some(msg.as_str()),
        Some(_) => return Err("msg must be text of at most 2048 bytes"),
    };
    let provider = status
        .get("app")
        .and_then(Value::as_str)
        .filter(|app| valid_app(app))
        .unwrap_or("program");
    let context = report.get("context");
    let session = context
        .and_then(|context| context.get("session_id"))
        .and_then(Value::as_str)
        .filter(|id| valid_text(id, 128))
        .unwrap_or(scope);
    let mut line = json!({
        "hook_event_name": event,
        "logged_at": Utc::now().to_rfc3339(),
        "session_id": session,
        "sidepulse_status": mode,
    });
    if let Some(cwd) = context
        .and_then(|context| context.get("cwd"))
        .and_then(Value::as_str)
        .filter(|cwd| valid_text(cwd, 4096))
    {
        line["cwd"] = json!(cwd);
    }
    if let Some(msg) = msg {
        line["message"] = json!(msg);
    }
    Ok((provider.to_owned(), line))
}

fn valid_text(value: &str, max_bytes: usize) -> bool {
    !value.is_empty() && value.len() <= max_bytes && !value.chars().any(char::is_control)
}

fn valid_app(value: &str) -> bool {
    value.len() <= 32
        && value.split('/').all(|segment| {
            (1..=32).contains(&segment.len())
                && segment
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"_.+-".contains(&b))
        })
}

fn usage() -> ExitCode {
    eprintln!("{USAGE}");
    ExitCode::from(2)
}

fn fail(message: &str) -> ExitCode {
    eprintln!("sidepulse-next report: {message}");
    ExitCode::FAILURE
}

#[cfg(test)]
mod tests {
    use super::*;

    fn report(state: &str) -> Value {
        json!({"v":1,"type":"program_status","scope":"opencode-s1",
            "status":{"state":state,"app":"opencode"},
            "context":{"provider":"opencode","session_id":"s1","cwd":"/tmp/p"}})
    }

    #[test]
    fn maps_every_state_to_an_explicit_mode() {
        for (state, mode) in [
            ("working", "working"),
            ("blocked", "waiting_for_input"),
            ("error", "blocked_error"),
            ("done", "completed"),
            ("idle", "idle_ready"),
            ("clear", "idle_ready"),
        ] {
            let (provider, line) = report_to_hook_line(&report(state)).unwrap();
            assert_eq!(provider, "opencode");
            assert_eq!(line["sidepulse_status"], mode);
            assert_eq!(line["session_id"], "s1");
            assert_eq!(line["cwd"], "/tmp/p");
        }
    }

    #[test]
    fn rejects_invalid_reports() {
        assert!(report_to_hook_line(&report("paused")).is_err());
        let mut bad = report("working");
        bad["scope"] = json!("");
        assert!(report_to_hook_line(&bad).is_err());
        bad = report("working");
        bad["v"] = json!(2);
        assert!(report_to_hook_line(&bad).is_err());
        bad = report("working");
        bad["status"]["msg"] = json!("a\u{1b}b");
        assert!(report_to_hook_line(&bad).is_err());
    }

    #[test]
    fn falls_back_to_scope_and_generic_provider() {
        let (provider, line) = report_to_hook_line(&json!({"v":1,"type":"program_status",
            "scope":"deploy-42","status":{"state":"done","app":"bad app"}}))
        .unwrap();
        assert_eq!(provider, "program");
        assert_eq!(line["session_id"], "deploy-42");
    }
}
