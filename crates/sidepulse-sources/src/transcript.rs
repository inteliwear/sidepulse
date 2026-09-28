//! Optional Codex and Claude transcript recovery. These sources are opt-in,
//! matching the Python settings defaults, and produce the same hook event model.

use std::collections::HashMap;
use std::fs::{self, File};
use std::io;
use std::path::{Path, PathBuf};
use std::time::SystemTime;

use chrono::{DateTime, NaiveDate, NaiveDateTime, Utc};
use serde_json::{Value, json};
use sidepulse_core::{HookEvent, parse_log_line};

use crate::SourceSpec;

const MAX_LINES: usize = 500;

pub fn is_transcript_provider(provider: &str) -> bool {
    matches!(provider, "codex-transcripts" | "claude-transcripts")
}

pub fn load_transcript_events(source: &SourceSpec) -> io::Result<Vec<HookEvent>> {
    let limit = match source.provider.as_str() {
        "codex-transcripts" => 12,
        "claude-transcripts" => 24,
        _ => {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "unknown transcript source",
            ));
        }
    };
    let mut events = Vec::new();
    for path in recent_files(&source.path, limit)? {
        events.extend(load_file(&source.provider, &path)?);
    }
    events.sort_by_key(|event| event.logged_at);
    Ok(events)
}

pub(crate) struct TranscriptCursor {
    source: SourceSpec,
    signatures: HashMap<PathBuf, (u64, Option<SystemTime>)>,
}

impl TranscriptCursor {
    pub(crate) fn source(&self) -> &SourceSpec {
        &self.source
    }

    pub(crate) fn new(source: SourceSpec) -> io::Result<Self> {
        let mut cursor = Self {
            source,
            signatures: HashMap::new(),
        };
        cursor.signatures = cursor.current_signatures()?;
        Ok(cursor)
    }

    pub(crate) fn poll(&mut self, events: &mut Vec<HookEvent>) -> io::Result<()> {
        let current = self.current_signatures()?;
        for (path, signature) in &current {
            if self.signatures.get(path) != Some(signature) {
                events.extend(load_file(&self.source.provider, path)?);
            }
        }
        self.signatures = current;
        Ok(())
    }

    fn current_signatures(&self) -> io::Result<HashMap<PathBuf, (u64, Option<SystemTime>)>> {
        let limit = if self.source.provider == "codex-transcripts" {
            12
        } else {
            24
        };
        recent_files(&self.source.path, limit)?
            .into_iter()
            .map(|path| {
                let metadata = fs::metadata(&path)?;
                Ok((path, (metadata.len(), metadata.modified().ok())))
            })
            .collect()
    }
}

fn recent_files(root: &Path, limit: usize) -> io::Result<Vec<PathBuf>> {
    let mut stack = vec![root.to_path_buf()];
    let mut found = Vec::new();
    while let Some(dir) = stack.pop() {
        let entries = match fs::read_dir(&dir) {
            Ok(entries) => entries,
            Err(error) if error.kind() == io::ErrorKind::NotFound => continue,
            Err(error) => return Err(error),
        };
        for entry in entries {
            let entry = entry?;
            let kind = entry.file_type()?;
            if kind.is_dir() {
                stack.push(entry.path());
            } else if kind.is_file() && entry.path().extension().is_some_and(|ext| ext == "jsonl") {
                let metadata = entry.metadata()?;
                found.push((
                    entry.path(),
                    metadata.modified().unwrap_or(SystemTime::UNIX_EPOCH),
                ));
            }
        }
    }
    found.sort_by_key(|(_, modified)| std::cmp::Reverse(*modified));
    Ok(found
        .into_iter()
        .take(limit)
        .map(|(path, _)| path)
        .collect())
}

// Python accepts naive ISO timestamps as UTC and uses the current time when
// a transcript omits or corrupts a timestamp. Keep those rows recoverable.
fn transcript_timestamp(value: Option<&Value>, fallback: DateTime<Utc>) -> DateTime<Utc> {
    let Some(text) = value
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|s| !s.is_empty())
    else {
        return fallback;
    };
    if let Ok(time) = DateTime::parse_from_rfc3339(text) {
        return time.with_timezone(&Utc);
    }
    for format in [
        "%Y-%m-%dT%H:%M:%S%.f",
        "%Y-%m-%d %H:%M:%S%.f",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M",
    ] {
        if let Ok(time) = NaiveDateTime::parse_from_str(text, format) {
            return time.and_utc();
        }
    }
    NaiveDate::parse_from_str(text, "%Y-%m-%d")
        .ok()
        .and_then(|date| date.and_hms_opt(0, 0, 0))
        .map_or(fallback, |time| time.and_utc())
}

fn load_file(provider: &str, path: &Path) -> io::Result<Vec<HookEvent>> {
    let Some(session_id) = session_id(path) else {
        return Ok(Vec::new());
    };
    let lines = crate::read_recent_lines(&mut File::open(path)?, MAX_LINES)?;
    let mut events = Vec::new();
    let mut cwd: Option<String> = None;
    let mut turn_id: Option<String> = None;
    for line in lines {
        let Ok(row) = serde_json::from_str::<Value>(&line) else {
            continue;
        };
        let Some(object) = row.as_object() else {
            continue;
        };
        let timestamp = transcript_timestamp(object.get("timestamp"), Utc::now());
        let event = if provider == "codex-transcripts" {
            let Some(payload) = object.get("payload").and_then(Value::as_object) else {
                continue;
            };
            if object.get("type").and_then(Value::as_str) == Some("turn_context") {
                cwd = string(payload.get("cwd")).or(cwd);
                turn_id = string(payload.get("turn_id")).or(turn_id);
                continue;
            }
            codex_event(
                &Value::Object(payload.clone()),
                &session_id,
                turn_id.as_deref(),
                cwd.as_deref(),
                timestamp,
                path,
            )
        } else {
            cwd = string(object.get("cwd")).or(cwd);
            claude_event(&row, &session_id, timestamp, path)
        };
        if let Some(event) = event {
            events.push(event);
        }
    }
    if provider == "claude-transcripts"
        && let Some(last) = events.last()
        && matches!(
            last.event_name.as_str(),
            "UserPromptSubmit"
                | "PreToolUse"
                | "PostToolUse"
                | "PreCompact"
                | "PostCompact"
                | "SubagentStart"
        )
        && let Ok(modified) = fs::metadata(path).and_then(|metadata| metadata.modified())
    {
        let modified: DateTime<Utc> = modified.into();
        if (modified - last.logged_at).num_seconds() > 30 {
            let last_cwd = last.cwd.clone().or(cwd);
            if let Some(event) = event(
                "claude",
                "Notification",
                &session_id,
                None,
                last_cwd.as_deref(),
                modified,
                path,
                json!({
                    "notification_type": "transcript_mtime",
                    "message": "Claude transcript file changed after the last embedded event."
                }),
            ) {
                events.push(event);
            }
        }
    }
    Ok(events)
}

fn codex_event(
    payload: &Value,
    session: &str,
    turn: Option<&str>,
    cwd: Option<&str>,
    timestamp: DateTime<Utc>,
    path: &Path,
) -> Option<HookEvent> {
    let kind = payload.get("type")?.as_str()?;
    match kind {
        "message" => match payload.get("role")?.as_str()? {
            "user" => event(
                "codex",
                "UserPromptSubmit",
                session,
                turn,
                cwd,
                timestamp,
                path,
                json!({"prompt": content_text(payload.get("content"))}),
            ),
            "assistant" => event(
                "codex",
                "Stop",
                session,
                turn,
                cwd,
                timestamp,
                path,
                json!({"last_assistant_message": content_text(payload.get("content"))}),
            ),
            _ => None,
        },
        "function_call" => event(
            "codex",
            "PreToolUse",
            session,
            turn,
            cwd,
            timestamp,
            path,
            json!({
                "tool_name": payload.get("name"), "tool_input": payload.get("arguments"), "tool_use_id": payload.get("call_id")
            }),
        ),
        "function_call_output" => event(
            "codex",
            "PostToolUse",
            session,
            turn,
            cwd,
            timestamp,
            path,
            json!({
                "tool_response": payload.get("output"), "tool_use_id": payload.get("call_id")
            }),
        ),
        "task_complete" => event(
            "codex",
            "Stop",
            session,
            turn,
            cwd,
            timestamp,
            path,
            json!({
                "last_assistant_message": payload.get("last_agent_message").and_then(Value::as_str).unwrap_or("")
            }),
        ),
        _ => None,
    }
}

fn claude_event(
    row: &Value,
    session: &str,
    timestamp: DateTime<Utc>,
    path: &Path,
) -> Option<HookEvent> {
    let cwd = row.get("cwd").and_then(Value::as_str);
    let content = row
        .get("message")
        .and_then(|message| message.get("content"));
    match row.get("type")?.as_str()? {
        "user" => {
            if row.get("isMeta") == Some(&Value::Bool(true)) {
                return None;
            }
            if let Some(items) = content.and_then(Value::as_array)
                && items
                    .iter()
                    .any(|item| item.get("type").and_then(Value::as_str) == Some("tool_result"))
            {
                let failed = items.iter().any(|item| {
                    item.get("type").and_then(Value::as_str) == Some("tool_result")
                        && (item.get("is_error") == Some(&Value::Bool(true))
                            || response_failed(item.get("content")))
                }) || response_failed(row.get("toolUseResult"));
                return event(
                    "claude",
                    if failed {
                        "PostToolUseFailure"
                    } else {
                        "PostToolUse"
                    },
                    session,
                    None,
                    cwd,
                    timestamp,
                    path,
                    json!({
                        "tool_response": row.get("toolUseResult").or(content), "tool_use_id": row.get("sourceToolAssistantUUID")
                    }),
                );
            }
            if row
                .get("toolUseResult")
                .is_some_and(|value| !value.is_null())
            {
                return event(
                    "claude",
                    if response_failed(row.get("toolUseResult")) {
                        "PostToolUseFailure"
                    } else {
                        "PostToolUse"
                    },
                    session,
                    None,
                    cwd,
                    timestamp,
                    path,
                    json!({"tool_response": row.get("toolUseResult")}),
                );
            }
            let prompt = content_text(content);
            if prompt.is_empty() || prompt.trim().starts_with("<task-notification>") {
                return None;
            }
            event(
                "claude",
                "UserPromptSubmit",
                session,
                None,
                cwd,
                timestamp,
                path,
                json!({"prompt": prompt}),
            )
        }
        "assistant" => {
            let message = row.get("message")?;
            if let Some(tool) = content.and_then(Value::as_array).and_then(|items| {
                items
                    .iter()
                    .find(|item| item.get("type").and_then(Value::as_str) == Some("tool_use"))
            }) {
                return event(
                    "claude",
                    "PreToolUse",
                    session,
                    None,
                    cwd,
                    timestamp,
                    path,
                    json!({
                        "tool_name": tool.get("name"), "tool_input": tool.get("input"), "tool_use_id": tool.get("id")
                    }),
                );
            }
            if message.get("stop_reason").and_then(Value::as_str) == Some("end_turn") {
                return event(
                    "claude",
                    "Stop",
                    session,
                    None,
                    cwd,
                    timestamp,
                    path,
                    json!({"last_assistant_message": content_text(content)}),
                );
            }
            None
        }
        _ => None,
    }
}

#[expect(
    clippy::too_many_arguments,
    reason = "each event carries the transcript session context"
)]
fn event(
    provider: &str,
    name: &str,
    session: &str,
    turn: Option<&str>,
    cwd: Option<&str>,
    timestamp: DateTime<Utc>,
    path: &Path,
    fields: Value,
) -> Option<HookEvent> {
    let mut raw = fields.as_object()?.clone();
    raw.insert("hook_event_name".into(), json!(name));
    raw.insert("session_id".into(), json!(session));
    raw.insert("turn_id".into(), json!(turn));
    raw.insert("cwd".into(), json!(cwd));
    raw.insert("transcript_path".into(), json!(path.to_string_lossy()));
    raw.insert("source".into(), json!(format!("{provider}-transcripts")));
    let line = if provider == "codex" {
        json!({"logged_at": timestamp.to_rfc3339(), "event": raw})
    } else {
        raw.insert("logged_at".into(), json!(timestamp.to_rfc3339()));
        Value::Object(raw)
    };
    let mut parsed = parse_log_line(provider, &line.to_string())?;
    if parsed.message.is_none() {
        parsed.message = parsed
            .raw
            .get("prompt")
            .and_then(Value::as_str)
            .map(str::to_owned);
    }
    Some(parsed)
}

fn content_text(content: Option<&Value>) -> String {
    match content {
        Some(Value::String(text)) => text.clone(),
        Some(Value::Array(items)) => items
            .iter()
            .filter_map(|item| item.get("text").and_then(Value::as_str))
            .collect::<Vec<_>>()
            .join("\n"),
        _ => String::new(),
    }
}

fn response_failed(response: Option<&Value>) -> bool {
    match response {
        Some(Value::Object(object)) => {
            object.get("interrupted") == Some(&Value::Bool(true))
                || object.get("success") == Some(&Value::Bool(false))
                || object
                    .get("exit_code")
                    .is_some_and(|code| !code.is_null() && code != 0)
        }
        Some(Value::String(text)) => {
            let text = text.to_ascii_lowercase();
            text.contains("exit code: 1") || text.contains("traceback")
        }
        _ => false,
    }
}

fn string(value: Option<&Value>) -> Option<String> {
    value
        .and_then(Value::as_str)
        .filter(|text| !text.is_empty())
        .map(str::to_owned)
}

fn session_id(path: &Path) -> Option<String> {
    let name = path.file_name()?.to_string_lossy();
    for start in 0..=name.len().saturating_sub(36) {
        let Some(candidate) = name.get(start..start + 36) else {
            continue;
        };
        if candidate.chars().enumerate().all(|(index, character)| {
            if [8, 13, 18, 23].contains(&index) {
                character == '-'
            } else {
                character.is_ascii_hexdigit()
            }
        }) {
            return Some(candidate.to_owned());
        }
    }
    None
}

#[cfg(test)]
mod tests {
    #[test]
    fn python_timestamp_fallback_and_naive_utc_rows_remain_recoverable() {
        let expected = DateTime::parse_from_rfc3339("2026-09-27T12:00:00Z")
            .unwrap()
            .with_timezone(&Utc);
        for value in [
            json!("2026-09-27T12:00:00"),
            json!("2026-09-27 12:00:00"),
            json!("2026-09-27T12:00"),
            json!(" 2026-09-27T14:00:00+02:00 "),
        ] {
            assert_eq!(transcript_timestamp(Some(&value), Utc::now()), expected);
        }
        for value in [json!("broken"), json!(""), Value::Null, json!(123)] {
            assert_eq!(transcript_timestamp(Some(&value), expected), expected);
        }
        assert_eq!(transcript_timestamp(None, expected), expected);
        assert_eq!(
            transcript_timestamp(Some(&json!("2026-09-27")), expected),
            DateTime::parse_from_rfc3339("2026-09-27T00:00:00Z").unwrap()
        );
    }

    use super::*;
    use tempfile::tempdir;

    #[test]
    fn parses_codex_transcript_and_tails_changes() {
        let dir = tempdir().unwrap();
        let path = dir
            .path()
            .join("rollout-12345678-1234-1234-1234-123456789abc.jsonl");
        fs::write(&path, concat!(
            "{\"timestamp\":\"2026-09-27T12:00:00Z\",\"type\":\"turn_context\",\"payload\":{\"cwd\":\"/repo\",\"turn_id\":\"t1\"}}\n",
            "{\"timestamp\":\"2026-09-27T12:00:01Z\",\"type\":\"response_item\",\"payload\":{\"type\":\"message\",\"role\":\"user\",\"content\":[{\"text\":\"hello\"}]}}\n"
        )).unwrap();
        let source = SourceSpec {
            provider: "codex-transcripts".into(),
            path: dir.path().into(),
        };
        let events = load_transcript_events(&source).unwrap();
        assert_eq!(events.len(), 1);
        assert_eq!(
            events[0].session_id.as_deref(),
            Some("12345678-1234-1234-1234-123456789abc")
        );
        assert_eq!(events[0].turn_id.as_deref(), Some("t1"));
        assert_eq!(events[0].cwd.as_deref(), Some("/repo"));
        let mut cursor = TranscriptCursor::new(source).unwrap();
        let mut initial = Vec::new();
        cursor.poll(&mut initial).unwrap();
        assert!(initial.is_empty());
        use std::io::Write;
        writeln!(fs::OpenOptions::new().append(true).open(&path).unwrap(), "{}", json!({"timestamp":"2026-09-27T12:00:02Z","type":"response_item","payload":{"type":"task_complete","last_agent_message":"done"}})).unwrap();
        let mut updates = Vec::new();
        cursor.poll(&mut updates).unwrap();
        assert_eq!(updates.last().unwrap().event_name, "Stop");
    }

    #[test]
    fn parses_claude_transcript() {
        let dir = tempdir().unwrap();
        let path = dir
            .path()
            .join("12345678-1234-1234-1234-123456789abc.jsonl");
        fs::write(&path, concat!(
            "{\"timestamp\":\"2026-09-27T12:00:00Z\",\"type\":\"user\",\"cwd\":\"/repo\",\"message\":{\"content\":\"hello\"}}\n",
            "{\"timestamp\":\"2026-09-27T12:00:01Z\",\"type\":\"assistant\",\"cwd\":\"/repo\",\"message\":{\"content\":[{\"type\":\"tool_use\",\"name\":\"Bash\",\"id\":\"x\"}]}}\n",
            "{\"timestamp\":\"2026-09-27T12:00:02Z\",\"type\":\"user\",\"cwd\":\"/repo\",\"message\":{\"content\":[{\"type\":\"tool_result\",\"is_error\":true}]}}\n"
        )).unwrap();
        let events = load_transcript_events(&SourceSpec {
            provider: "claude-transcripts".into(),
            path: dir.path().into(),
        })
        .unwrap();
        assert_eq!(
            &events
                .iter()
                .map(|event| event.event_name.as_str())
                .collect::<Vec<_>>()[..3],
            &["UserPromptSubmit", "PreToolUse", "PostToolUseFailure"]
        );
    }
}

#[cfg(test)]
mod captured_parity {
    use super::*;
    #[test]
    fn captured_python_transcript_events_match() {
        let cases: Value =
            serde_json::from_str(include_str!("../resources/parity/python-transcripts.json"))
                .unwrap();
        let directory = tempfile::tempdir().unwrap();
        let path = directory
            .path()
            .join("12345678-1234-1234-1234-123456789abc.jsonl");
        let timestamp = DateTime::parse_from_rfc3339("2026-09-27T12:00:00Z")
            .unwrap()
            .timestamp();
        for case in cases.as_array().unwrap() {
            fs::write(&path, format!("{}\n", case["row"])).unwrap();
            fs::File::options()
                .write(true)
                .open(&path)
                .unwrap()
                .set_times(std::fs::FileTimes::new().set_modified(
                    SystemTime::UNIX_EPOCH + std::time::Duration::from_secs(timestamp as u64),
                ))
                .unwrap();
            let events = load_file(
                &format!("{}-transcripts", case["provider"].as_str().unwrap()),
                &path,
            )
            .unwrap();
            let actual:Vec<_>=events.iter().map(|event|json!({"provider":event.provider,"event_name":event.event_name,"session_id":event.session_id,"turn_id":event.turn_id,"cwd":event.cwd,"tool_name":event.tool_name,"message":event.message})).collect();
            assert_eq!(json!(actual), case["expected"], "{case}");
        }
    }
}
