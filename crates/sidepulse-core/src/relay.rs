//! Portable validation and annotation of SidePulse relay events.

use serde_json::Value;

#[derive(Debug, Clone, PartialEq)]
pub struct RelayEvent {
    pub provider: String,
    pub line: Value,
    pub event_id: String,
    pub source_name: String,
}

pub fn parse_relay_message(text: &str) -> Option<RelayEvent> {
    let message: Value = serde_json::from_str(text).ok()?;
    if message.get("v")?.as_u64()? != 1 || message.get("type")?.as_str()? != "agent_event" {
        return None;
    }
    let provider = message.get("provider")?.as_str()?;
    if !["codex", "claude", "grok", "cursor", "junie"].contains(&provider) {
        return None;
    }
    let event_id = message.get("event_id")?.as_str()?.to_owned();
    let mut line = message.get("line")?.as_object()?.clone();
    let source_name = message
        .get("source")
        .and_then(|source| source.get("name"))
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|name| !name.is_empty())
        .unwrap_or("Remote computer")
        .chars()
        .take(80)
        .collect::<String>();
    let raw = if provider == "codex" {
        line.get_mut("event")?.as_object_mut()?
    } else {
        &mut line
    };
    raw.insert(
        "sidepulse_relay_source".into(),
        Value::String(source_name.clone()),
    );
    let origin = raw
        .get("agent_origin")
        .or_else(|| raw.get("agentOrigin"))
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    let origin = if origin.is_empty() {
        source_name.clone()
    } else {
        format!("{origin} · {source_name}")
    };
    raw.insert("agent_origin".into(), Value::String(origin));
    let identity = ["agent_id", "agentId", "session_id", "sessionId"]
        .into_iter()
        .find_map(|key| {
            raw.get(key).filter(|value| {
                !value.is_null() && value.as_str().is_none_or(|text| !text.is_empty())
            })
        })
        .map(|value| {
            value
                .as_str()
                .map(str::to_owned)
                .unwrap_or_else(|| value.to_string())
        })
        .unwrap_or_else(|| "default".into());
    raw.insert(
        "agent_id".into(),
        Value::String(format!("relay:{source_name}:{identity}")),
    );
    Some(RelayEvent {
        provider: provider.to_owned(),
        line: Value::Object(line),
        event_id,
        source_name,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn remote_claude_event_is_namespaced_and_keeps_origin() {
        let message = json!({
            "v": 1, "type": "agent_event", "event_id": "e1",
            "provider": "claude", "source": {"name": "Laptop"},
            "line": {"hook_event_name": "Stop", "session_id": "a", "agent_origin": "VS Code"}
        });
        let event = parse_relay_message(&message.to_string()).unwrap();
        assert_eq!(event.event_id, "e1");
        assert_eq!(event.line["agent_id"], "relay:Laptop:a");
        assert_eq!(event.line["agent_origin"], "VS Code · Laptop");
        assert_eq!(event.line["sidepulse_relay_source"], "Laptop");
    }

    #[test]
    fn remote_codex_event_annotates_nested_payload() {
        let message = json!({
            "v": 1, "type": "agent_event", "event_id": "e2",
            "provider": "codex", "source": {"name": "Desktop"},
            "line": {"logged_at": "2026-09-27T00:00:00Z", "event": {"hook_event_name": "Stop", "session_id": "b"}}
        });
        let event = parse_relay_message(&message.to_string()).unwrap();
        assert_eq!(event.line["event"]["agent_id"], "relay:Desktop:b");
        assert_eq!(event.line["event"]["agent_origin"], "Desktop");
        assert!(parse_relay_message(&message.to_string().replace("\"v\":1", "\"v\":2")).is_none());
    }
}
