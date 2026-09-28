//! Session targets and preference precedence, independent of OS activation.

use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::AgentStatus;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SessionAction {
    App,
    Terminal,
    Vscode,
}

impl SessionAction {
    pub const ALL: [Self; 3] = [Self::App, Self::Terminal, Self::Vscode];
    pub fn key(self) -> &'static str {
        match self {
            Self::App => "app",
            Self::Terminal => "terminal",
            Self::Vscode => "vscode",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum SessionTarget {
    Url {
        url: String,
    },
    Terminal {
        executable: String,
        args: Vec<String>,
        cwd: String,
    },
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SessionOpenOption {
    pub action: SessionAction,
    pub label: String,
    pub target: SessionTarget,
}

fn percent_encode(value: &str) -> String {
    let mut result = String::new();
    for byte in value.bytes() {
        if byte.is_ascii_alphanumeric() || b"-._~".contains(&byte) {
            result.push(char::from(byte));
        } else {
            use std::fmt::Write;
            let _ = write!(result, "%{byte:02X}");
        }
    }
    result
}

pub fn session_open_options(status: &AgentStatus, home: &str) -> Vec<SessionOpenOption> {
    let provider = status.provider.to_lowercase();
    let session = status
        .session_id
        .as_deref()
        .filter(|session| !session.is_empty());
    let mut options = Vec::new();
    if provider == "codex"
        && let Some(session) = session
    {
        options.push(SessionOpenOption {
            action: SessionAction::App,
            label: "Open in Codex".into(),
            target: SessionTarget::Url {
                url: format!("codex://threads/{}", percent_encode(session)),
            },
        });
    } else if provider == "claude" {
        options.push(SessionOpenOption {
            action: SessionAction::App,
            label: "Open Claude App".into(),
            target: SessionTarget::Url {
                url: "claude://".into(),
            },
        });
    }
    if let Some(session) = session {
        let args = match provider.as_str() {
            "codex" => Some(vec!["resume".into(), session.into()]),
            "claude" | "grok" => Some(vec!["--resume".into(), session.into()]),
            "junie" => Some(vec![format!("--session-id={session}"), "--resume".into()]),
            _ => None,
        };
        if let Some(args) = args {
            options.push(SessionOpenOption {
                action: SessionAction::Terminal,
                label: "Resume in Terminal".into(),
                target: SessionTarget::Terminal {
                    executable: provider.clone(),
                    args,
                    cwd: status
                        .cwd
                        .as_deref()
                        .filter(|cwd| !cwd.is_empty())
                        .unwrap_or(home)
                        .into(),
                },
            });
        }
        if provider == "claude" {
            options.push(SessionOpenOption {
                action: SessionAction::Vscode,
                label: "Open in VS Code".into(),
                target: SessionTarget::Url {
                    url: format!(
                        "vscode://anthropic.claude-code/open?session={}",
                        percent_encode(session)
                    ),
                },
            });
        }
    }
    options
}

pub fn normalized_session_origin(origin: &str) -> String {
    let mut result = String::new();
    for character in origin.to_lowercase().chars() {
        if character.is_ascii_alphanumeric() {
            result.push(character);
        } else if !result.is_empty() && !result.ends_with('_') {
            result.push('_');
        }
    }
    let result = result.trim_matches('_');
    if result.is_empty() {
        "unknown".into()
    } else {
        result.into()
    }
}

pub fn saved_session_action(
    settings: &Value,
    provider: &str,
    origin: Option<&str>,
) -> Option<SessionAction> {
    let provider = provider.to_lowercase();
    let preferences = settings.get("session_open_preferences");
    let action = |key: &str| {
        preferences
            .and_then(|value| value.get(key))
            .and_then(|value| serde_json::from_value(value.clone()).ok())
    };
    if let Some(origin) = origin.filter(|origin| !origin.is_empty()) {
        let key = normalized_session_origin(origin);
        if let Some(selected) =
            action(&format!("origin:{provider}:{key}")).or_else(|| action(&format!("origin:{key}")))
        {
            return Some(selected);
        }
    }
    if provider == "grok" {
        return Some(
            settings
                .get("grok_session_open_action")
                .and_then(|value| serde_json::from_value(value.clone()).ok())
                .unwrap_or(SessionAction::Terminal),
        );
    }
    action(&provider)
}

pub fn preferred_session_action(
    status: &AgentStatus,
    settings: &Value,
    options: &[SessionOpenOption],
) -> Option<SessionAction> {
    if let Some(action) = saved_session_action(settings, &status.provider, status.origin.as_deref())
        && options.iter().any(|option| option.action == action)
    {
        return Some(action);
    }
    let origin = status
        .origin
        .as_deref()
        .unwrap_or("")
        .to_lowercase()
        .replace('-', " ")
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ");
    let order = if ["vscode", "vs code", "visual studio code"]
        .iter()
        .any(|part| origin.contains(part))
    {
        [
            SessionAction::Vscode,
            SessionAction::App,
            SessionAction::Terminal,
        ]
    } else if ["cli", "terminal", "command line"]
        .iter()
        .any(|part| origin.contains(part))
    {
        [
            SessionAction::Terminal,
            SessionAction::App,
            SessionAction::Vscode,
        ]
    } else if ["app", "ui", "transcript"]
        .iter()
        .any(|part| origin.contains(part))
    {
        [
            SessionAction::App,
            SessionAction::Vscode,
            SessionAction::Terminal,
        ]
    } else if origin.contains("cursor") || origin.contains("windsurf") {
        [
            SessionAction::App,
            SessionAction::Terminal,
            SessionAction::Vscode,
        ]
    } else if status.provider.eq_ignore_ascii_case("claude") {
        [
            SessionAction::Vscode,
            SessionAction::App,
            SessionAction::Terminal,
        ]
    } else {
        [
            SessionAction::App,
            SessionAction::Terminal,
            SessionAction::Vscode,
        ]
    };
    order
        .into_iter()
        .find(|action| options.iter().any(|option| option.action == *action))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{Monitor, parse_log_line};
    use serde_json::json;

    #[test]
    fn session_targets_escape_urls_and_keep_terminal_arguments_separate() {
        let mut monitor = Monitor::default();
        let event = parse_log_line("claude", &json!({"hook_event_name":"PreToolUse","session_id":"a&b '雪'","cwd":"/tmp/project's folder"}).to_string()).unwrap();
        let status = monitor.ingest(&event).unwrap();
        let options = session_open_options(status, "/home/test");
        assert_eq!(options.len(), 3);
        let SessionTarget::Terminal { args, cwd, .. } = &options[1].target else {
            panic!();
        };
        assert_eq!(args, &["--resume", "a&b '雪'"]);
        assert_eq!(cwd, "/tmp/project's folder");
        assert_eq!(
            options[2].target,
            SessionTarget::Url {
                url: "vscode://anthropic.claude-code/open?session=a%26b%20%27%E9%9B%AA%27".into()
            }
        );
        assert_eq!(
            preferred_session_action(status, &json!({}), &options),
            Some(SessionAction::Vscode)
        );
    }

    #[test]
    fn origin_preferences_precede_provider_settings_and_unavailable_choices_fall_back() {
        let mut monitor = Monitor::default();
        let event = parse_log_line(
            "codex",
            &json!({"hook_event_name":"UserPromptSubmit","session_id":"s","origin":"Codex CLI"})
                .to_string(),
        )
        .unwrap();
        let mut status = monitor.ingest(&event).unwrap().clone();
        status.origin = Some("Codex CLI".into());
        let options = session_open_options(&status, "/home/test");
        let settings =
            json!({"session_open_preferences":{"codex":"app","origin:codex:codex_cli":"terminal"}});
        assert_eq!(
            preferred_session_action(&status, &settings, &options),
            Some(SessionAction::Terminal)
        );
        assert_eq!(
            preferred_session_action(
                &status,
                &json!({"session_open_preferences":{"codex":"vscode"}}),
                &options
            ),
            Some(SessionAction::Terminal)
        );
    }
}
