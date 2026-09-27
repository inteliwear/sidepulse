//! Read configured hook commands as data to find existing SidePulse log paths.

use std::fs;
use std::path::{Path, PathBuf};

use serde_json::Value;

pub fn discover_log_path(provider: &str, home: &Path) -> Option<PathBuf> {
    let relative = match provider {
        "codex" => ".codex/config.toml",
        "claude" => ".claude/settings.json",
        "grok" => ".grok/hooks/sidepulse.json",
        "cursor" => ".cursor/hooks.json",
        "junie" => ".junie/config.json",
        _ => return None,
    };
    let text = fs::read_to_string(home.join(relative)).ok()?;
    let document: Value = if provider == "codex" {
        let parsed: toml::Value = toml::from_str(&text).ok()?;
        serde_json::to_value(parsed).ok()?
    } else {
        serde_json::from_str(&text).ok()?
    };
    let hooks = document.get("hooks")?;
    let mut commands = Vec::new();
    collect_commands(hooks, &mut commands);
    commands
        .into_iter()
        .filter(|command| {
            command.contains("sidepulse")
                || command.contains("agent-monitor hook-log")
                || command.contains("hook_entry.py")
        })
        .flat_map(|command| paths_from_command(command, home))
        .next()
}

fn collect_commands<'a>(value: &'a Value, output: &mut Vec<&'a str>) {
    match value {
        Value::Object(object) => {
            if let Some(command) = object.get("command").and_then(Value::as_str) {
                output.push(command);
            }
            for (key, child) in object {
                if key != "command" {
                    collect_commands(child, output);
                }
            }
        }
        Value::Array(values) => {
            for child in values {
                collect_commands(child, output);
            }
        }
        _ => {}
    }
}

fn paths_from_command(command: &str, home: &Path) -> Vec<PathBuf> {
    let parts = split_shell_words(command);
    let mut paths = Vec::new();
    for (index, part) in parts.iter().enumerate() {
        let path = if part == "--log" || part == ">>" {
            parts.get(index + 1).map(String::as_str)
        } else if let Some(path) = part.strip_prefix(">>") {
            (!path.is_empty()).then_some(path)
        } else {
            part.strip_prefix("--log=")
        };
        if let Some(path) = path {
            let expanded = if path == "~" {
                home.to_path_buf()
            } else if let Some(suffix) = path.strip_prefix("~/") {
                home.join(suffix)
            } else {
                PathBuf::from(path)
            };
            paths.push(expanded);
        }
    }
    paths
}

fn split_shell_words(command: &str) -> Vec<String> {
    let mut words = Vec::new();
    let mut current = String::new();
    let mut quote = None;
    let mut started = false;
    let mut chars = command.chars().peekable();
    while let Some(ch) = chars.next() {
        if ch == '\\' && quote != Some('\'') {
            if chars.peek().is_some_and(|next| {
                *next == '"' || *next == '\'' || *next == '\\' || next.is_whitespace()
            }) {
                current.push(chars.next().unwrap());
            } else {
                current.push('\\');
            }
            started = true;
        } else if let Some(delimiter) = quote {
            if ch == delimiter {
                quote = None;
            } else {
                current.push(ch);
            }
        } else if ch == '\'' || ch == '"' {
            quote = Some(ch);
            started = true;
        } else if ch.is_whitespace() {
            if started {
                words.push(std::mem::take(&mut current));
                started = false;
            }
        } else {
            current.push(ch);
            started = true;
        }
    }
    if started {
        words.push(current);
    }
    words
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn extracts_quoted_log_and_redirect_paths() {
        let home = Path::new("/users/demo");
        assert_eq!(
            paths_from_command("sidepulse hook-log --log '~/state/agent log.jsonl'", home),
            vec![home.join("state/agent log.jsonl")]
        );
        assert_eq!(
            paths_from_command("python hook_entry.py >> \"/tmp/agent log.jsonl\"", home),
            vec![PathBuf::from("/tmp/agent log.jsonl")]
        );
        assert_eq!(
            paths_from_command("sidepulse hook-log --log=~/agent.jsonl", home),
            vec![home.join("agent.jsonl")]
        );
        assert_eq!(
            paths_from_command(
                r#"sidepulse hook-log --log "C:\Users\demo\agent log.jsonl""#,
                home
            ),
            vec![PathBuf::from(r"C:\Users\demo\agent log.jsonl")]
        );
        assert_eq!(
            paths_from_command("python hook_entry.py >>'/tmp/agent.jsonl'", home),
            vec![PathBuf::from("/tmp/agent.jsonl")]
        );
    }
}
