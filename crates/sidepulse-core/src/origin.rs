use std::collections::HashMap;

use serde_json::Value;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AgentOrigin {
    pub kind: String,
    pub label: String,
    pub source: String,
    pub confidence: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProcessInfo {
    pub pid: u32,
    pub ppid: Option<u32>,
    pub comm: String,
    pub command: String,
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
    None
}

pub fn origin_from_terminal_environment(
    provider: &str,
    env: &HashMap<String, String>,
) -> Option<AgentOrigin> {
    env.get("TERM_PROGRAM")
        .filter(|term| !term.trim().is_empty() && !term.eq_ignore_ascii_case("vscode"))
        .map(|_| AgentOrigin::surface(provider, "cli", "env:TERM_PROGRAM"))
}

pub fn origin_from_processes(provider: &str, processes: &[ProcessInfo]) -> Option<AgentOrigin> {
    let haystack = processes
        .iter()
        .map(|info| format!("{}\n{}", info.comm, info.command))
        .collect::<Vec<_>>()
        .join("\n")
        .to_ascii_lowercase();
    for (tokens, surface, source) in [
        (
            &["visual studio code.app", "code helper", "vscode"][..],
            "vscode",
            "process:Visual Studio Code",
        ),
        (
            &["cursor.app", "cursor helper"][..],
            "cursor",
            "process:Cursor",
        ),
        (
            &["windsurf.app", "windsurf helper"][..],
            "windsurf",
            "process:Windsurf",
        ),
    ] {
        if tokens.iter().any(|token| haystack.contains(token)) {
            return Some(AgentOrigin::surface(provider, surface, source));
        }
    }
    for (process, surface, source) in [
        ("code.exe", "vscode", "process:Visual Studio Code"),
        ("cursor.exe", "cursor", "process:Cursor"),
        ("windsurf.exe", "windsurf", "process:Windsurf"),
    ] {
        if processes
            .iter()
            .any(|info| info.comm.eq_ignore_ascii_case(process))
        {
            return Some(AgentOrigin::surface(provider, surface, source));
        }
    }
    let app = match provider {
        "codex" if haystack.contains("codex.app") || haystack.contains("chatgpt.app") => {
            Some("process:Codex.app")
        }
        "claude" if haystack.contains("claude.app") => Some("process:Claude.app"),
        "grok" if haystack.contains("grok.app") => Some("process:Grok.app"),
        "junie"
            if [
                "intellij idea.app",
                "pycharm.app",
                "webstorm.app",
                "goland.app",
                "phpstorm.app",
                "rubymine.app",
                "rustrover.app",
                "rider.app",
                "clion.app",
            ]
            .iter()
            .any(|token| haystack.contains(token))
                || processes.iter().any(|info| {
                    [
                        "idea64.exe",
                        "pycharm64.exe",
                        "webstorm64.exe",
                        "goland64.exe",
                        "phpstorm64.exe",
                        "rubymine64.exe",
                        "rustrover64.exe",
                        "rider64.exe",
                        "clion64.exe",
                    ]
                    .iter()
                    .any(|name| info.comm.eq_ignore_ascii_case(name))
                }) =>
        {
            return Some(AgentOrigin::surface(
                provider,
                "ide",
                "process:JetBrains IDE",
            ));
        }
        _ => None,
    };
    if let Some(source) = app {
        return Some(AgentOrigin::surface(provider, "app", source));
    }
    for info in processes {
        let basename = std::path::Path::new(&info.comm)
            .file_name()
            .and_then(|name| name.to_str())
            .unwrap_or("")
            .to_ascii_lowercase();
        let command_basename = info
            .command
            .split_whitespace()
            .next()
            .and_then(|name| std::path::Path::new(name).file_name())
            .and_then(|name| name.to_str())
            .unwrap_or("")
            .to_ascii_lowercase();
        let is_cli = [
            basename.trim_end_matches(".exe"),
            command_basename.trim_end_matches(".exe"),
        ]
        .iter()
        .any(|name| match provider {
            "codex" => *name == "codex",
            "claude" => ["claude", "claude-code"].contains(name),
            "grok" => *name == "grok",
            "junie" => *name == "junie",
            _ => false,
        });
        if is_cli {
            return Some(AgentOrigin::surface(
                provider,
                "cli",
                &format!("process:{provider}"),
            ));
        }
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

    #[test]
    fn process_origin_prefers_editor_before_cli() {
        let processes = vec![
            ProcessInfo {
                pid: 10,
                ppid: Some(20),
                comm: "/usr/bin/codex".into(),
                command: "codex exec".into(),
            },
            ProcessInfo {
                pid: 20,
                ppid: None,
                comm: "Code Helper".into(),
                command: "/Applications/Visual Studio Code.app/Contents/MacOS/Code Helper".into(),
            },
        ];
        let origin = origin_from_processes("codex", &processes).unwrap();
        assert_eq!(origin.label, "Codex in VS Code");
        assert_eq!(origin.source, "process:Visual Studio Code");
        let origin = origin_from_processes(
            "claude",
            &[ProcessInfo {
                pid: 1,
                ppid: None,
                comm: "/usr/bin/claude".into(),
                command: "claude".into(),
            }],
        )
        .unwrap();
        assert_eq!(origin.label, "Claude Code CLI");
        let windows = [ProcessInfo {
            pid: 3,
            ppid: None,
            comm: "Cursor.exe".into(),
            command: "Cursor.exe".into(),
        }];
        assert_eq!(
            origin_from_processes("codex", &windows).unwrap().label,
            "Codex in Cursor"
        );
        let windows_cli = [ProcessInfo {
            pid: 4,
            ppid: None,
            comm: "codex.exe".into(),
            command: "codex.exe".into(),
        }];
        assert_eq!(
            origin_from_processes("codex", &windows_cli).unwrap().label,
            "Codex CLI"
        );
    }
}
