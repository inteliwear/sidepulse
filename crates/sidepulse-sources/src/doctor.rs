//! Read-only provider hook diagnostics for the CLI and installer.

use std::collections::BTreeSet;
use std::fs;
use std::path::{Path, PathBuf};

use serde_json::Value;

use crate::discovery::{collect_commands, paths_from_command};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProviderConfig {
    pub provider: String,
    pub config_path: PathBuf,
    pub exists: bool,
    pub hooks_enabled: bool,
    pub hook_events: Vec<String>,
    pub log_paths: Vec<PathBuf>,
}

const PROVIDERS: [(&str, &str); 5] = [
    ("codex", ".codex/config.toml"),
    ("claude", ".claude/settings.json"),
    ("grok", ".grok/hooks/sidepulse.json"),
    ("cursor", ".cursor/hooks.json"),
    ("junie", ".junie/config.json"),
];

pub fn detect_provider_configs(home: &Path) -> Vec<ProviderConfig> {
    PROVIDERS
        .into_iter()
        .map(|(provider, relative)| detect_one(provider, home.join(relative), home))
        .collect()
}

fn detect_one(provider: &str, config_path: PathBuf, home: &Path) -> ProviderConfig {
    let exists = config_path.exists();
    let mut report = ProviderConfig {
        provider: provider.to_owned(),
        config_path,
        exists,
        hooks_enabled: false,
        hook_events: Vec::new(),
        log_paths: Vec::new(),
    };
    let Ok(text) = fs::read_to_string(&report.config_path) else {
        return report;
    };
    let document: Value = if provider == "codex" {
        let Ok(parsed) = toml::from_str::<toml::Value>(&text) else {
            return report;
        };
        let Ok(value) = serde_json::to_value(parsed) else {
            return report;
        };
        value
    } else {
        let Ok(value) = serde_json::from_str(&text) else {
            return report;
        };
        value
    };
    if provider == "codex" {
        report.hooks_enabled = document
            .get("features")
            .and_then(|features| features.get("hooks"))
            .and_then(Value::as_bool)
            .unwrap_or(false);
    }
    let Some(hooks) = document.get("hooks").and_then(Value::as_object) else {
        return report;
    };
    let mut events = BTreeSet::new();
    for (event, entries) in hooks {
        let canonical = if provider == "grok" {
            sidepulse_core::canonical_event_name(event).unwrap_or(event)
        } else {
            event
        };
        if !allowed_events(provider).contains(&canonical) {
            continue;
        }
        let Some(entries) = entries.as_array() else {
            continue;
        };
        let relevant: Vec<_> = if matches!(provider, "cursor" | "junie") {
            entries
                .iter()
                .filter(|entry| {
                    let mut commands = Vec::new();
                    collect_commands(entry, &mut commands);
                    commands.iter().any(|command| is_sidepulse_command(command))
                })
                .collect()
        } else {
            entries.iter().collect()
        };
        if relevant.is_empty() {
            continue;
        }
        events.insert(canonical.to_owned());
        for entry in relevant {
            let mut commands = Vec::new();
            collect_commands(entry, &mut commands);
            for command in commands {
                for path in paths_from_command(command, home) {
                    if !report.log_paths.contains(&path) {
                        report.log_paths.push(path);
                    }
                }
            }
        }
    }
    report.hook_events = events.into_iter().collect();
    if provider != "codex" {
        report.hooks_enabled = !report.hook_events.is_empty();
    }
    report
}

fn is_sidepulse_command(command: &str) -> bool {
    command.contains("sidepulse-next-hook")
        || command.contains("sidepulse.cursor_hook")
        || command.contains("sidepulse hook-log")
        || command.contains("agent-monitor hook-log")
        || command.contains("hook_entry.py")
}

fn allowed_events(provider: &str) -> &'static [&'static str] {
    match provider {
        "codex" => &[
            "SessionStart",
            "UserPromptSubmit",
            "PreToolUse",
            "PostToolUse",
            "PermissionRequest",
            "PreCompact",
            "PostCompact",
            "SubagentStart",
            "SubagentStop",
            "Stop",
            "Interrupt",
        ],
        "claude" => &[
            "SessionStart",
            "UserPromptSubmit",
            "PreToolUse",
            "PostToolUse",
            "PostToolUseFailure",
            "PermissionRequest",
            "Notification",
            "PreCompact",
            "PostCompact",
            "SubagentStop",
            "Stop",
            "SessionEnd",
        ],
        "grok" => &[
            "SessionStart",
            "UserPromptSubmit",
            "PreToolUse",
            "PostToolUse",
            "PostToolUseFailure",
            "PermissionDenied",
            "Notification",
            "PreCompact",
            "PostCompact",
            "SubagentStart",
            "SubagentStop",
            "Stop",
            "StopFailure",
            "SessionEnd",
        ],
        "cursor" => &[
            "sessionStart",
            "sessionEnd",
            "beforeSubmitPrompt",
            "preToolUse",
            "beforeShellExecution",
            "afterShellExecution",
            "afterFileEdit",
            "postToolUse",
            "postToolUseFailure",
            "stop",
        ],
        "junie" => &[
            "SessionStart",
            "UserPromptSubmit",
            "PreToolUse",
            "Stop",
            "StopFailure",
            "SessionEnd",
        ],
        _ => &[],
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reports_present_missing_and_managed_hooks() {
        let home = tempfile::tempdir().unwrap();
        fs::create_dir_all(home.path().join(".codex")).unwrap();
        fs::write(home.path().join(".codex/config.toml"), "[features]\nhooks = true\n[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ncommand = 'sidepulse-next-hook --log /tmp/codex.jsonl'\n").unwrap();
        fs::create_dir_all(home.path().join(".cursor")).unwrap();
        fs::write(home.path().join(".cursor/hooks.json"), r#"{"hooks":{"stop":[{"command":"unrelated --log /tmp/other"},{"command":"sidepulse-next-hook --log /tmp/cursor.jsonl"}]}}"#).unwrap();
        let reports = detect_provider_configs(home.path());
        assert_eq!(reports.len(), 5);
        assert_eq!(reports[0].hook_events, ["Stop"]);
        assert_eq!(reports[0].log_paths, [PathBuf::from("/tmp/codex.jsonl")]);
        assert!(reports[0].hooks_enabled);
        assert!(!reports[1].exists);
        assert_eq!(reports[3].hook_events, ["stop"]);
        assert_eq!(reports[3].log_paths, [PathBuf::from("/tmp/cursor.jsonl")]);
    }
}
