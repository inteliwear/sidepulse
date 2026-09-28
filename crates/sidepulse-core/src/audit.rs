use chrono::{DateTime, SecondsFormat, Utc};
use serde_json::{Value, json};

use crate::{AgentStatus, HookEvent};
use std::io::{self, BufRead, Write};

pub const AUDIT_COLUMNS: [&str; 14] = [
    "audited_at",
    "logged_at",
    "provider",
    "hook_event",
    "status",
    "status_label",
    "origin",
    "display_name",
    "session_id",
    "agent_id",
    "cwd",
    "tool_name",
    "message",
    "raw_preview",
];

/// Streaming exports preserve the legacy fourteen-column debug log schema.
pub fn export_status_audit(
    mut source: impl BufRead,
    mut destination: impl Write,
    format: crate::DiagnosticFormat,
) -> io::Result<usize> {
    use crate::DiagnosticFormat;
    match format {
        DiagnosticFormat::Csv => writeln!(destination, "{}\r", AUDIT_COLUMNS.join(","))?,
        DiagnosticFormat::Html => {
            writeln!(
                destination,
                "<!doctype html><meta charset=\"utf-8\"><title>SidePulse Agent Debug Log</title><style>body{{font:14px system-ui;margin:24px}}table{{border-collapse:collapse;width:100%}}th,td{{border-bottom:1px solid #ddd;padding:8px;text-align:left;vertical-align:top;overflow-wrap:anywhere}}th{{position:sticky;top:0;background:white}}.raw{{font:12px monospace}}</style><h1>SidePulse Agent Debug Log</h1><table><thead><tr>"
            )?;
            for column in AUDIT_COLUMNS {
                write!(destination, "<th>{column}</th>")?;
            }
            writeln!(destination, "</tr></thead><tbody>")?;
        }
    }
    let mut events = 0;
    let mut line = Vec::new();
    loop {
        line.clear();
        let length = std::io::Read::take(&mut source, 1_048_577).read_until(b'\n', &mut line)?;
        if length == 0 {
            break;
        }
        if length > 1_048_576 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "debug log contains an oversized event",
            ));
        }
        let Ok(record) = serde_json::from_slice::<Value>(&line) else {
            continue;
        };
        if !record.is_object() {
            continue;
        }
        let cells = AUDIT_COLUMNS.map(|key| match record.get(key) {
            Some(Value::String(value)) => value.clone(),
            Some(Value::Null) => "None".into(),
            Some(Value::Bool(true)) => "True".into(),
            Some(Value::Bool(false)) => "False".into(),
            Some(value) => value.to_string(),
            None => String::new(),
        });
        match format {
            DiagnosticFormat::Csv => {
                for (index, cell) in cells.iter().enumerate() {
                    if index > 0 {
                        write!(destination, ",")?;
                    }
                    if cell.contains([',', '"', '\r', '\n']) {
                        write!(destination, "\"{}\"", cell.replace('"', "\"\""))?;
                    } else {
                        write!(destination, "{cell}")?;
                    }
                }
                write!(destination, "\r\n")?;
            }
            DiagnosticFormat::Html => {
                write!(destination, "<tr>")?;
                for (column, cell) in AUDIT_COLUMNS.iter().zip(cells) {
                    let escaped = cell
                        .replace('&', "&amp;")
                        .replace('<', "&lt;")
                        .replace('>', "&gt;")
                        .replace('"', "&quot;")
                        .replace('\'', "&#x27;");
                    write!(
                        destination,
                        "<td{}>{escaped}</td>",
                        if *column == "raw_preview" {
                            " class=\"raw\""
                        } else {
                            ""
                        }
                    )?;
                }
                writeln!(destination, "</tr>")?;
            }
        }
        events += 1;
    }
    if format == DiagnosticFormat::Html {
        writeln!(destination, "</tbody></table><p>{events} events</p>")?;
    }
    Ok(events)
}

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
    fn debug_exports_preserve_columns_quotes_unicode_and_escape_html() {
        let record = json!({"provider":"claude","message":"a, \"雪\"\nnext","raw_preview":"<script>alert('x')</script> & data"});
        let input = format!("bad JSON\n[]\n{}\n", record);
        let mut csv = Vec::new();
        assert_eq!(
            export_status_audit(input.as_bytes(), &mut csv, crate::DiagnosticFormat::Csv).unwrap(),
            1
        );
        let csv = String::from_utf8(csv).unwrap();
        assert!(csv.starts_with(&format!("{}\r\n", AUDIT_COLUMNS.join(","))));
        assert!(csv.contains("\"a, \"\"雪\"\"\nnext\""));
        let mut html = Vec::new();
        assert_eq!(
            export_status_audit(input.as_bytes(), &mut html, crate::DiagnosticFormat::Html)
                .unwrap(),
            1
        );
        let html = String::from_utf8(html).unwrap();
        assert!(!html.contains("<script>"));
        assert!(html.contains("&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt; &amp; data"));
        assert!(html.contains("<p>1 events</p>"));
    }

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
