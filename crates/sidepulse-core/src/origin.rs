use std::collections::HashMap;

use serde_json::Value;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AgentOrigin {
    pub kind: String,
    pub label: String,
    pub source: String,
    pub confidence: String,
}

impl AgentOrigin {
    fn surface(provider: &str, surface: &str, source: &str) -> Self {
        let label = match (provider, surface) {
            ("codex", "app") => "Codex UI",
            ("codex", "cli") => "Codex CLI",
            ("codex", "vscode") => "Codex in VS Code",
            ("codex", "cursor") => "Codex in Cursor",
            ("codex", "windsurf") => "Codex in Windsurf",
            ("codex", "transcript") => "Codex Transcript",
            ("claude", "app") => "Claude App",
            ("claude", "cli") => "Claude Code CLI",
            ("claude", "vscode") => "Claude in VS Code",
            ("claude", "cursor") => "Claude in Cursor",
            ("claude", "windsurf") => "Claude in Windsurf",
            ("claude", "transcript") => "Claude Transcript",
            ("grok", "app") => "Grok App",
            ("grok", "cli") => "Grok CLI",
            ("grok", "vscode") => "Grok in VS Code",
            ("grok", "cursor") => "Grok in Cursor",
            ("grok", "windsurf") => "Grok in Windsurf",
            ("grok", "transcript") => "Grok Transcript",
            ("junie", "cli") => "Junie CLI",
            ("junie", "ide") => "Junie in JetBrains IDE",
            ("junie", "transcript") => "Junie Transcript",
            _ => provider_label(provider),
        };
        Self {
            kind: format!("{provider}_{surface}"),
            label: label.into(),
            source: source.into(),
            confidence: "inferred".into(),
        }
    }

    pub fn fallback(provider: &str) -> Self {
        Self {
            kind: format!("{provider}_unknown"),
            label: provider_label(provider).into(),
            source: "fallback:provider".into(),
            confidence: "unknown".into(),
        }
    }
}

pub fn origin_from_environment(
    provider: &str,
    env: &HashMap<String, String>,
) -> Option<AgentOrigin> {
    if let Some(label) = env
        .get("SIDEPULSE_AGENT_ORIGIN")
        .and_then(|value| clean_label(value))
    {
        let kind = env
            .get("SIDEPULSE_AGENT_ORIGIN_KIND")
            .and_then(|value| clean_label(value))
            .unwrap_or_else(|| normalize_kind(&label));
        return Some(AgentOrigin {
            kind,
            label,
            source: "env:SIDEPULSE_AGENT_ORIGIN".into(),
            confidence: "explicit".into(),
        });
    }
    let term = env.get("TERM_PROGRAM").map_or("", String::as_str);
    if term.eq_ignore_ascii_case("vscode") || env.keys().any(|key| key.starts_with("VSCODE_")) {
        return Some(AgentOrigin::surface(provider, "vscode", "env:VSCODE"));
    }
    if let Some(bundle) = env.get("__CFBundleIdentifier") {
        let bundle = bundle.to_ascii_lowercase();
        let surface = match provider {
            "codex"
                if ["openai", "chatgpt", "codex"]
                    .iter()
                    .any(|part| bundle.contains(part)) =>
            {
                Some("app")
            }
            "claude" if bundle.contains("anthropic") => Some("app"),
            "grok" if bundle.contains("grok") => Some("app"),
            "junie" if bundle.contains("jetbrains") => Some("ide"),
            _ => None,
        };
        if let Some(surface) = surface {
            return Some(AgentOrigin::surface(
                provider,
                surface,
                "env:__CFBundleIdentifier",
            ));
        }
    }
    if !term.trim().is_empty() {
        return Some(AgentOrigin::surface(provider, "cli", "env:TERM_PROGRAM"));
    }
    None
}

pub fn origin_label_from_payload(provider: &str, raw: &Value) -> Option<String> {
    for key in [
        "agent_origin",
        "agentOrigin",
        "agent_origin_label",
        "origin_label",
    ] {
        if let Some(label) = raw.get(key).and_then(Value::as_str).and_then(clean_label) {
            return Some(label);
        }
    }
    let structured = raw
        .get("sidepulse_origin")
        .or_else(|| raw.get("sidepulseOrigin"));
    if let Some(value) = structured {
        if let Some(label) = value.as_str().and_then(clean_label) {
            return Some(label);
        }
        for key in ["label", "name", "origin"] {
            if let Some(label) = value.get(key).and_then(Value::as_str).and_then(clean_label) {
                return Some(label);
            }
        }
    }
    match raw.get("source").and_then(Value::as_str) {
        Some("codex-transcripts") | Some("claude-transcripts") => {
            Some(AgentOrigin::surface(provider, "transcript", "source:transcript").label)
        }
        _ => None,
    }
}

fn clean_label(value: &str) -> Option<String> {
    let cleaned = value.split_whitespace().collect::<Vec<_>>().join(" ");
    (!cleaned.is_empty()).then_some(cleaned)
}

fn normalize_kind(value: &str) -> String {
    let kind = value
        .trim()
        .to_ascii_lowercase()
        .chars()
        .map(|ch| if ch.is_ascii_alphanumeric() { ch } else { '_' })
        .collect::<String>();
    let kind = kind.trim_matches('_').to_owned();
    if kind.is_empty() {
        "custom".into()
    } else {
        kind
    }
}

fn provider_label(provider: &str) -> &str {
    match provider {
        "codex" => "Codex",
        "claude" => "Claude Code",
        "grok" => "Grok",
        "cursor" => "Cursor",
        "junie" => "Junie",
        _ => provider,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn environment_precedence_and_structured_origin_match_legacy_rules() {
        let env = HashMap::from([
            ("SIDEPULSE_AGENT_ORIGIN".into(), "  My  Editor ".into()),
            ("TERM_PROGRAM".into(), "vscode".into()),
        ]);
        let origin = origin_from_environment("codex", &env).unwrap();
        assert_eq!(origin.label, "My Editor");
        assert_eq!(origin.kind, "my_editor");
        assert_eq!(origin.confidence, "explicit");

        let env = HashMap::from([("VSCODE_GIT_IPC_HANDLE".into(), "x".into())]);
        assert_eq!(
            origin_from_environment("claude", &env).unwrap().label,
            "Claude in VS Code"
        );
        assert_eq!(
            origin_label_from_payload("codex", &json!({"sidepulse_origin":{"label":"Codex UI"}})),
            Some("Codex UI".into())
        );
        assert_eq!(
            origin_label_from_payload("codex", &json!({"source":"codex-transcripts"})),
            Some("Codex Transcript".into())
        );
    }
}
