use chrono::{DateTime, SecondsFormat, Utc};
use serde_json::{Value, json};

use crate::{AgentStatus, HookEvent};

/// The existing JSONL audit schema consumed by the Python CLI and exports.
pub fn status_audit_record(
    event: &HookEvent,
    status: Option<&AgentStatus>,
    audited_at: DateTime<Utc>,
) -> Value {
    let status_mode = status
        .and_then(|status| serde_json::to_value(status.mode).ok())
        .and_then(|mode| mode.as_str().map(str::to_owned))
        .unwrap_or_default();
    let raw_message = ["message", "last_assistant_message", "prompt"]
        .iter()
        .find_map(|key| event.raw.get(*key).and_then(Value::as_str))
        .unwrap_or("");
    let message = event.message.as_deref().unwrap_or(raw_message);
    let raw_preview = serde_json::to_string(&event.raw).unwrap_or_default();
    json!({
        "audited_at": audited_at.format("%Y-%m-%dT%H:%M:%SZ").to_string(),
        "logged_at": event.logged_at.to_rfc3339_opts(SecondsFormat::AutoSi, false),
        "provider": event.provider,
        "hook_event": event.event_name,
        "status": status_mode,
        "status_label": status.map_or("", |status| status.mode.label()),
        "origin": status.and_then(|status| status.origin.as_deref())
            .or(event.origin.as_deref()).unwrap_or(""),
        "display_name": status.map_or("", |status| status.display_name.as_str()),
        "session_id": event.session_id.as_deref().unwrap_or(""),
        "agent_id": event.status_key(),
        "cwd": event.cwd.as_deref().unwrap_or(""),
        "tool_name": event.tool_name.as_deref().unwrap_or(""),
        "message": truncate_preview(message, 240),
        "raw_preview": truncate_preview(&raw_preview, 2000),
    })
}

fn truncate_preview(text: &str, limit: usize) -> String {
    let normalized = text.split_whitespace().collect::<Vec<_>>().join(" ");
    if normalized.chars().count() <= limit {
        return normalized;
    }
    let mut preview: String = normalized.chars().take(limit - 3).collect();
    preview = preview.trim_end().to_owned();
    preview.push_str("...");
    preview
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{Monitor, parse_log_line};

    #[test]
    fn audit_record_uses_legacy_fields_and_preview_limits() {
        let event = parse_log_line(
            "claude",
            r#"{"logged_at":"2026-09-26T12:00:00Z","hook_event_name":"Stop","session_id":"s1","message":"done"}"#,
        )
        .unwrap();
        let mut monitor = Monitor::default();
        let status = monitor.ingest(&event).unwrap();
        let row = status_audit_record(&event, Some(status), event.logged_at);
        assert_eq!(row["provider"], "claude");
        assert_eq!(row["status"], "completed");
        assert_eq!(row["status_label"], "Completed");
        assert_eq!(row["agent_id"], "claude:session:s1");
        assert_eq!(row["message"], "done");
    }
}
