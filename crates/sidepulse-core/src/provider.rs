use chrono::{DateTime, NaiveDateTime, Utc};
use serde_json::{Map, Value, json};

use crate::{HookEvent, origin_label_from_payload};

const KNOWN_EVENTS: &[&str] = &[
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "PermissionRequest",
    "PermissionDenied",
    "Notification",
    "PreCompact",
    "PostCompact",
    "SubagentStart",
    "SubagentStop",
    "Stop",
    "StopFailure",
    "SessionEnd",
    "Interrupt",
];

pub fn canonical_event_name(value: &str) -> Option<&'static str> {
    let key = alias_key(value);
    if key == "subagent_end" {
        return Some("SubagentStop");
    }
    KNOWN_EVENTS
        .iter()
        .copied()
        .find(|event| alias_key(event) == key)
}

fn alias_key(value: &str) -> String {
    let mut out = String::new();
    let mut previous_lower_or_digit = false;
    for ch in value.trim().chars() {
        if ch.is_ascii_alphanumeric() {
            if ch.is_ascii_uppercase() && previous_lower_or_digit && !out.ends_with('_') {
                out.push('_');
            }
            out.push(ch.to_ascii_lowercase());
            previous_lower_or_digit = ch.is_ascii_lowercase() || ch.is_ascii_digit();
        } else {
            if !out.is_empty() && !out.ends_with('_') {
                out.push('_');
            }
            previous_lower_or_digit = false;
        }
    }
    out.trim_matches('_').to_owned()
}

pub fn parse_log_line(provider: &str, line: &str) -> Option<HookEvent> {
    let document: Value = serde_json::from_str(line.trim()).ok()?;
    let obj = document.as_object()?;
    let (raw, timestamp) = if provider == "codex" {
        match obj.get("event").and_then(Value::as_object) {
            Some(event) => (
                event,
                obj.get("logged_at").or_else(|| event.get("logged_at")),
            ),
            None => (obj, obj.get("logged_at").or_else(|| obj.get("timestamp"))),
        }
    } else {
        (obj, obj.get("logged_at").or_else(|| obj.get("timestamp")))
    };
    let actual_provider = if provider == "claude" && looks_like_grok(raw) {
        "grok"
    } else {
        provider
    };
    let event_name = first_string(
        raw,
        &[
            "hook_event_name",
            "hookEventName",
            "event_name",
            "eventName",
        ],
    )
    .and_then(|name| canonical_event_name(&name))?;
    let mut normalized = raw.clone();
    normalized
        .entry("hook_event_name".to_owned())
        .or_insert_with(|| json!(event_name));
    if let Some(value) = timestamp {
        normalized
            .entry("logged_at".to_owned())
            .or_insert_with(|| value.clone());
    }
    for (source, target) in [
        ("sessionId", "session_id"),
        ("turnId", "turn_id"),
        ("agentId", "agent_id"),
        ("workspaceRoot", "cwd"),
        ("toolName", "tool_name"),
        ("toolInput", "tool_input"),
        ("toolResponse", "tool_response"),
        ("lastAssistantMessage", "last_assistant_message"),
        ("notificationType", "notification_type"),
        ("agentOrigin", "agent_origin"),
        ("agentOriginKind", "agent_origin_kind"),
        ("sidepulseOrigin", "sidepulse_origin"),
    ] {
        if !normalized.contains_key(target)
            && let Some(value) = normalized.get(source).cloned()
        {
            normalized.insert(target.to_owned(), value);
        }
    }
    Some(HookEvent {
        provider: actual_provider.to_owned(),
        logged_at: timestamp
            .and_then(Value::as_str)
            .and_then(parse_datetime)
            .unwrap_or_else(Utc::now),
        event_name: event_name.to_owned(),
        session_id: first_string(&normalized, &["session_id", "sessionId"]),
        turn_id: first_string(&normalized, &["turn_id", "turnId"]),
        agent_id: first_string(&normalized, &["agent_id", "agentId"]),
        cwd: first_string(&normalized, &["cwd", "workspaceRoot"]),
        tool_name: first_string(&normalized, &["tool_name", "toolName"]),
        message: first_string(
            &normalized,
            &[
                "message",
                "last_assistant_message",
                "lastAssistantMessage",
                "error_details",
            ],
        ),
        origin: origin_label_from_payload(actual_provider, &Value::Object(normalized.clone())),
        raw: Value::Object(normalized),
    })
}

pub fn format_hook_payload(provider: &str, payload_text: &str, logged_at: DateTime<Utc>) -> Value {
    let payload: Value = serde_json::from_str(if payload_text.is_empty() {
        "{}"
    } else {
        payload_text
    })
    .unwrap_or_else(|error| {
        json!({
            "hook_event_name": "ParseError",
            "raw": payload_text,
            "parse_error": error.to_string(),
        })
    });
    let timestamp = logged_at.format("%Y-%m-%dT%H:%M:%SZ").to_string();
    if provider == "codex" {
        json!({"logged_at": timestamp, "event": payload})
    } else if let Value::Object(mut object) = payload {
        object
            .entry("logged_at".to_owned())
            .or_insert_with(|| json!(timestamp));
        Value::Object(object)
    } else {
        json!({"logged_at": timestamp, "event": payload})
    }
}

/// Adapt Cursor's hook names and fields to the common event schema.
pub fn normalize_cursor_payload(event_name: &str, payload: &Value) -> Value {
    let mut normalized = payload.as_object().cloned().unwrap_or_default();
    let canonical = match event_name {
        "sessionStart" => "SessionStart",
        "sessionEnd" => "SessionEnd",
        "beforeSubmitPrompt" => "UserPromptSubmit",
        "preToolUse" | "beforeShellExecution" => "PreToolUse",
        "afterShellExecution" | "afterFileEdit" | "postToolUse" => "PostToolUse",
        "postToolUseFailure" => "StopFailure",
        "stop" => "Stop",
        other => other,
    };
    normalized.insert("hook_event_name".into(), json!(canonical));
    let session = normalized
        .get("conversation_id")
        .or_else(|| normalized.get("conversationId"))
        .filter(|value| !value.is_null())
        .map(value_as_string)
        .unwrap_or_default();
    normalized.insert("session_id".into(), json!(session));
    for (key, value) in [
        ("agent_origin", "Cursor"),
        ("agent_origin_kind", "cursor_app"),
        ("agent_origin_source", "hook:cursor"),
        ("agent_origin_confidence", "explicit"),
    ] {
        normalized.insert(key.into(), json!(value));
    }
    let cwd = normalized
        .get("workspace_roots")
        .or_else(|| normalized.get("workspaceRoots"))
        .and_then(Value::as_array)
        .and_then(|roots| roots.first())
        .map(value_as_string)
        .or_else(|| normalized.get("workspace_root").map(value_as_string));
    if let Some(cwd) = cwd {
        normalized.insert("cwd".into(), json!(cwd));
    }
    match event_name {
        "beforeShellExecution" | "preToolUse" => {
            let name = normalized
                .get("tool_name")
                .filter(|value| !value.is_null())
                .map(value_as_string)
                .unwrap_or_else(|| "shell".into());
            normalized.insert("tool_name".into(), json!(name));
            let input = normalized
                .get("tool_input")
                .filter(|value| !value.is_null())
                .cloned()
                .unwrap_or_else(|| json!({"command": normalized.get("command")}));
            normalized.insert("tool_input".into(), input);
        }
        "afterShellExecution" | "afterFileEdit" | "postToolUse" | "postToolUseFailure" => {
            let name = normalized
                .get("tool_name")
                .filter(|value| !value.is_null())
                .map(value_as_string)
                .unwrap_or_else(|| event_name.into());
            normalized.insert("tool_name".into(), json!(name));
            normalized.insert(
                "tool_response".into(),
                json!({
                    "status": normalized.get("status"),
                    "exit_code": normalized.get("exit_code"),
                    "error": normalized.get("error_message"),
                }),
            );
            if event_name == "postToolUseFailure"
                && let Some(error) = normalized
                    .get("error_message")
                    .filter(|value| !value.is_null())
            {
                normalized.insert("message".into(), json!(value_as_string(error)));
            }
        }
        "stop" => {
            if let Some(status) = normalized.get("status").and_then(Value::as_str) {
                normalized.insert("message".into(), json!(format!("Cursor stopped: {status}")));
            }
        }
        _ => {}
    }
    Value::Object(normalized)
}

pub fn infer_hook_provider(provider: &str, line: &Value) -> String {
    let raw = if provider == "codex" {
        line.get("event").unwrap_or(line)
    } else {
        line
    };
    if provider == "claude" && raw.as_object().is_some_and(looks_like_grok) {
        "grok".into()
    } else {
        provider.into()
    }
}

fn value_as_string(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        _ => value.to_string(),
    }
}

fn first_string(raw: &Map<String, Value>, keys: &[&str]) -> Option<String> {
    keys.iter().find_map(|key| match raw.get(*key) {
        None | Some(Value::Null) => None,
        Some(Value::String(value)) if value.is_empty() => None,
        Some(Value::String(value)) => Some(value.clone()),
        Some(other) => Some(other.to_string()),
    })
}

fn parse_datetime(value: &str) -> Option<DateTime<Utc>> {
    DateTime::parse_from_rfc3339(value)
        .map(|value| value.with_timezone(&Utc))
        .ok()
        .or_else(|| {
            NaiveDateTime::parse_from_str(value, "%Y-%m-%dT%H:%M:%S")
                .ok()
                .map(|value| value.and_utc())
        })
}

fn looks_like_grok(raw: &Map<String, Value>) -> bool {
    let path = first_string(raw, &["transcriptPath", "transcript_path"]).unwrap_or_default();
    path.contains("/.grok/") || path.contains("\\.grok\\") || raw.contains_key("hookEventName")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accepts_native_and_aliased_event_names() {
        assert_eq!(
            canonical_event_name("PostToolUseFailure"),
            Some("PostToolUseFailure")
        );
        assert_eq!(
            canonical_event_name("post_tool_use_failure"),
            Some("PostToolUseFailure")
        );
        assert_eq!(canonical_event_name("beforeSubmitPrompt"), None);
        assert_eq!(canonical_event_name("subagent_end"), Some("SubagentStop"));
    }

    #[test]
    fn normalizes_codex_and_grok_log_lines() {
        let codex = parse_log_line("codex", r#"{"logged_at":"2026-09-26T12:00:00Z","event":{"hook_event_name":"PreToolUse","session_id":"a","tool_name":"Shell"}}"#).unwrap();
        assert_eq!(codex.status_key(), "codex:session:a");
        assert_eq!(codex.tool_name.as_deref(), Some("Shell"));
        let grok = parse_log_line("claude", r#"{"logged_at":"2026-09-26T12:00:00Z","hookEventName":"SessionStart","sessionId":"b","workspaceRoot":"/repo"}"#).unwrap();
        assert_eq!(grok.provider, "grok");
        assert_eq!(grok.cwd.as_deref(), Some("/repo"));
    }

    #[test]
    fn cursor_hook_normalization_preserves_session_and_tool_fields() {
        let payload = normalize_cursor_payload(
            "beforeShellExecution",
            &json!({"conversationId":"s1","workspace_roots":["/repo"],"command":"pwd"}),
        );
        assert_eq!(payload["hook_event_name"], "PreToolUse");
        assert_eq!(payload["session_id"], "s1");
        assert_eq!(payload["cwd"], "/repo");
        assert_eq!(payload["tool_input"]["command"], "pwd");
        assert_eq!(payload["agent_origin"], "Cursor");
    }

    #[test]
    fn grok_hook_is_routed_from_claude() {
        assert_eq!(
            infer_hook_provider("claude", &json!({"hookEventName":"Stop"})),
            "grok"
        );
    }
}
