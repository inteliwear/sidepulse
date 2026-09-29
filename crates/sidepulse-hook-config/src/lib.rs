//! Pure provider config plans plus an explicit, backed-up apply step.

use std::fs;
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{Map, Value, json};
use tempfile::NamedTempFile;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Action {
    Install,
    Uninstall,
}

pub fn provider_config_path(provider: &str) -> Option<&'static Path> {
    Some(Path::new(match provider {
        "codex" => ".codex/config.toml",
        "claude" => ".claude/settings.json",
        "grok" => ".grok/hooks/sidepulse.json",
        "cursor" => ".cursor/hooks.json",
        "junie" => ".junie/config.json",
        _ => return None,
    }))
}

#[derive(Debug, Clone)]
pub struct HookPlan {
    pub provider: String,
    pub config_path: PathBuf,
    pub log_path: PathBuf,
    pub changed: bool,
    pub updated: String,
    original: Option<Vec<u8>>,
}

#[derive(Debug, Clone)]
pub struct ApplyResult {
    pub changed: bool,
    pub backup_path: Option<PathBuf>,
}

const CLAUDE: &[&str] = &[
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
];
const GROK: &[&str] = &[
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
];
const CURSOR: &[&str] = &[
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
];
const JUNIE: &[&str] = &[
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "Stop",
    "StopFailure",
    "SessionEnd",
];
const CODEX: &[&str] = &[
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
];
const MANAGED_START: &str = "# >>> agent-monitor hooks >>>";
const MANAGED_END: &str = "# <<< agent-monitor hooks <<<";

pub fn plan_codex_hooks(
    config_path: &Path,
    log_path: &Path,
    hook_executable: &Path,
    action: Action,
) -> io::Result<HookPlan> {
    let original = match fs::read(config_path) {
        Ok(bytes) => Some(bytes),
        Err(error) if error.kind() == io::ErrorKind::NotFound => None,
        Err(error) => return Err(error),
    };
    let text = match &original {
        Some(bytes) => std::str::from_utf8(bytes).map_err(io::Error::other)?,
        None => "",
    };
    if !text.trim().is_empty() {
        toml::from_str::<toml::Value>(text).map_err(io::Error::other)?;
    }
    if action == Action::Uninstall
        && !text.contains(MANAGED_START)
        && !text.contains(MANAGED_END)
        && !text.lines().any(is_managed_command)
    {
        return Ok(HookPlan {
            provider: "codex".into(),
            config_path: config_path.to_path_buf(),
            log_path: log_path.to_path_buf(),
            changed: false,
            updated: text.into(),
            original,
        });
    }
    let cleaned = remove_codex_managed_blocks(text);
    let mut updated = if action == Action::Install {
        ensure_codex_hooks_feature(&cleaned)
    } else {
        cleaned
    };
    if action == Action::Install {
        updated = updated.trim_end().to_owned();
        if !updated.is_empty() {
            updated.push_str("\n\n");
        }
        updated.push_str(MANAGED_START);
        updated.push('\n');
        for event in CODEX {
            let command = hook_command("codex", event, hook_executable, log_path);
            updated.push_str(&format!(
                "[[hooks.{event}]]\nmatcher = \"*\"\n[[hooks.{event}.hooks]]\ntype = \"command\"\ncommand = {}\n",
                toml::Value::String(command)
            ));
            if *event == "Interrupt" {
                updated.push_str("timeout = 3\n");
            }
            updated.push('\n');
        }
        updated.push_str(MANAGED_END);
        updated.push('\n');
    } else if !updated.is_empty() {
        updated = format!("{}\n", updated.trim_end());
    }
    if !updated.trim().is_empty() {
        toml::from_str::<toml::Value>(&updated).map_err(io::Error::other)?;
    }
    let changed = original.as_deref() != Some(updated.as_bytes())
        && !(original.is_none() && updated.is_empty());
    Ok(HookPlan {
        provider: "codex".into(),
        config_path: config_path.to_path_buf(),
        log_path: log_path.to_path_buf(),
        changed,
        updated,
        original,
    })
}

fn remove_codex_managed_blocks(text: &str) -> String {
    let lines = text.lines().collect::<Vec<_>>();
    let mut kept = Vec::new();
    let mut index = 0;
    while index < lines.len() {
        let line = lines[index].trim();
        if line == MANAGED_START || line == MANAGED_END {
            index += 1;
            continue;
        }
        let event = CODEX
            .iter()
            .copied()
            .find(|event| line == format!("[[hooks.{event}]]"));
        if let Some(event) = event {
            let start = index;
            index += 1;
            while index < lines.len() {
                let next = lines[index].trim();
                if next.starts_with('[')
                    && next.ends_with(']')
                    && next != format!("[[hooks.{event}.hooks]]")
                {
                    break;
                }
                index += 1;
            }
            let block = &lines[start..index];
            if !block.iter().any(|line| is_managed_command(line)) {
                kept.extend(block.iter().copied().filter(|line| {
                    let line = line.trim();
                    line != MANAGED_START && line != MANAGED_END
                }));
            }
        } else {
            kept.push(lines[index]);
            index += 1;
        }
    }
    kept.join("\n")
}

fn ensure_codex_hooks_feature(text: &str) -> String {
    let mut lines = text.lines().map(str::to_owned).collect::<Vec<_>>();
    if let Some(start) = lines.iter().position(|line| line.trim() == "[features]") {
        let end = (start + 1..lines.len())
            .find(|index| lines[*index].trim().starts_with('['))
            .unwrap_or(lines.len());
        if let Some(index) =
            (start + 1..end).find(|index| lines[*index].trim().starts_with("hooks ="))
        {
            lines[index] = "hooks = true".into();
        } else {
            lines.insert(end, "hooks = true".into());
        }
    } else {
        lines.push("[features]".into());
        lines.push("hooks = true".into());
    }
    lines.join("\n")
}

/// Remove duplicated legacy handlers before registering the current Grok hooks.
pub fn plan_grok_legacy_removal(home: &Path, log: &Path, hook: &Path) -> io::Result<Vec<HookPlan>> {
    let mut plans = Vec::new();
    for name in ["sidepulse-agent-monitor.json", "sidepulse-cli.json"] {
        let config = home.join(".grok/hooks").join(name);
        match fs::metadata(&config) {
            Err(error) if error.kind() == io::ErrorKind::NotFound => continue,
            Err(error) => return Err(error),
            Ok(metadata) if !metadata.is_file() || metadata.len() > 1024 * 1024 => {
                return Err(io::Error::other(
                    "legacy Grok configuration must be a file smaller than 1 MiB",
                ));
            }
            Ok(_) => {}
        }
        let mut plan = plan_json_hooks("grok", &config, log, hook, Action::Uninstall)?;
        plan.provider = format!("grok legacy {name}");
        plans.push(plan);
    }
    Ok(plans)
}

pub fn plan_json_hooks(
    provider: &str,
    config_path: &Path,
    log_path: &Path,
    hook_executable: &Path,
    action: Action,
) -> io::Result<HookPlan> {
    let events = match provider {
        "claude" => CLAUDE,
        "grok" => GROK,
        "cursor" => CURSOR,
        "junie" => JUNIE,
        _ => {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "unsupported JSON hook provider",
            ));
        }
    };
    let original = match fs::read(config_path) {
        Ok(bytes) => Some(bytes),
        Err(error) if error.kind() == io::ErrorKind::NotFound => None,
        Err(error) => return Err(error),
    };
    let mut document: Value = match &original {
        Some(bytes) => serde_json::from_slice(bytes).map_err(io::Error::other)?,
        None => json!({}),
    };
    if action == Action::Uninstall && !has_managed_command(document.get("hooks")) {
        return Ok(HookPlan {
            provider: provider.into(),
            config_path: config_path.to_path_buf(),
            log_path: log_path.to_path_buf(),
            changed: false,
            updated: original
                .as_ref()
                .map(|bytes| String::from_utf8_lossy(bytes).into_owned())
                .unwrap_or_default(),
            original,
        });
    }
    let object = document.as_object_mut().ok_or_else(|| {
        io::Error::new(
            io::ErrorKind::InvalidData,
            "provider config must be a JSON object",
        )
    })?;
    if action == Action::Install && provider == "cursor" {
        object.entry("version").or_insert(json!(1));
    }
    let mut hooks = match object.remove("hooks") {
        Some(Value::Object(hooks)) => hooks,
        Some(_) => {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "hooks must be a JSON object",
            ));
        }
        None => Map::new(),
    };
    for event in events {
        let mut entries = match hooks.remove(*event) {
            Some(Value::Array(entries)) => entries,
            Some(other) => {
                hooks.insert((*event).into(), other);
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    format!("{event} hooks must be an array"),
                ));
            }
            None => Vec::new(),
        };
        entries = if provider == "cursor" {
            clean_cursor_entries(entries)
        } else {
            clean_nested_entries(entries)
        };
        if action == Action::Install {
            let command = hook_command(provider, event, hook_executable, log_path);
            entries.push(if provider == "cursor" {
                json!({"command": command})
            } else {
                let mut entry = json!({"hooks": [{"type": "command", "command": command}]});
                if provider == "claude"
                    || (provider == "grok"
                        && [
                            "PreToolUse",
                            "PostToolUse",
                            "PostToolUseFailure",
                            "PermissionDenied",
                            "Notification",
                        ]
                        .contains(event))
                {
                    entry["matcher"] = json!("*");
                }
                entry
            });
        }
        if !entries.is_empty() {
            hooks.insert((*event).into(), Value::Array(entries));
        }
    }
    if !hooks.is_empty() {
        object.insert("hooks".into(), Value::Object(hooks));
    }
    let mut updated = serde_json::to_string_pretty(&document).map_err(io::Error::other)?;
    updated.push('\n');
    let changed = match &original {
        Some(bytes) => {
            serde_json::from_slice::<Value>(bytes).map_err(io::Error::other)? != document
        }
        None => document != json!({}),
    };
    Ok(HookPlan {
        provider: provider.into(),
        config_path: config_path.to_path_buf(),
        log_path: log_path.to_path_buf(),
        changed,
        updated,
        original,
    })
}

impl HookPlan {
    /// Apply only if the config still matches the content used to build this plan.
    pub fn apply(&self) -> io::Result<ApplyResult> {
        if !self.changed {
            return Ok(ApplyResult {
                changed: false,
                backup_path: None,
            });
        }
        match fs::symlink_metadata(&self.config_path) {
            Ok(metadata) if metadata.file_type().is_symlink() => {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "refusing to replace a provider config symlink",
                ));
            }
            Ok(_) => {}
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(error) => return Err(error),
        }
        let current = match fs::read(&self.config_path) {
            Ok(bytes) => Some(bytes),
            Err(error) if error.kind() == io::ErrorKind::NotFound => None,
            Err(error) => return Err(error),
        };
        if current != self.original {
            return Err(io::Error::new(
                io::ErrorKind::AlreadyExists,
                "provider config changed after planning",
            ));
        }
        let parent = self.config_path.parent().unwrap_or_else(|| Path::new("."));
        fs::create_dir_all(parent)?;
        let backup_path = if let Some(bytes) = &current {
            let stamp = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map_err(io::Error::other)?
                .as_nanos();
            let name = self
                .config_path
                .file_name()
                .unwrap_or_default()
                .to_string_lossy();
            let path = parent.join(format!("{name}.bak.{stamp}"));
            let mut backup = fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&path)?;
            backup.write_all(bytes)?;
            backup.sync_all()?;
            Some(path)
        } else {
            None
        };
        let mut temporary = NamedTempFile::new_in(parent)?;
        if let Ok(metadata) = fs::metadata(&self.config_path) {
            temporary
                .as_file()
                .set_permissions(metadata.permissions())?;
        }
        temporary.write_all(self.updated.as_bytes())?;
        temporary.flush()?;
        temporary.as_file().sync_all()?;
        temporary
            .persist(&self.config_path)
            .map_err(|error| error.error)?;
        Ok(ApplyResult {
            changed: true,
            backup_path,
        })
    }

    /// Restore only a file still matching this plan's own write. Backups made
    /// by `apply` remain available even after a successful rollback.
    fn rollback(&self) -> io::Result<()> {
        if !self.changed {
            return Ok(());
        }
        let current = fs::read(&self.config_path)?;
        if current != self.updated.as_bytes() {
            return Err(io::Error::new(
                io::ErrorKind::AlreadyExists,
                "provider config changed after this plan was applied",
            ));
        }
        match &self.original {
            Some(original) => {
                let parent = self.config_path.parent().unwrap_or_else(|| Path::new("."));
                let mut temporary = NamedTempFile::new_in(parent)?;
                if let Ok(metadata) = fs::metadata(&self.config_path) {
                    temporary
                        .as_file()
                        .set_permissions(metadata.permissions())?;
                }
                temporary.write_all(original)?;
                temporary.flush()?;
                temporary.as_file().sync_all()?;
                temporary
                    .persist(&self.config_path)
                    .map_err(|error| error.error)?;
            }
            None => fs::remove_file(&self.config_path)?,
        }
        Ok(())
    }
}

/// Apply a prebuilt provider set as one operation. A later failure restores
/// earlier files, unless another process changed one in the meantime.
pub fn apply_plans_atomically(plans: &[HookPlan]) -> io::Result<Vec<ApplyResult>> {
    let mut results = Vec::with_capacity(plans.len());
    for (index, plan) in plans.iter().enumerate() {
        match plan.apply() {
            Ok(result) => results.push(result),
            Err(error) => {
                let mut rollback_errors = Vec::new();
                for previous in plans[..index].iter().rev() {
                    if let Err(rollback_error) = previous.rollback() {
                        rollback_errors.push(format!("{}: {rollback_error}", previous.provider));
                    }
                }
                if rollback_errors.is_empty() {
                    return Err(error);
                }
                return Err(io::Error::other(format!(
                    "{error}; rollback failed: {}",
                    rollback_errors.join("; ")
                )));
            }
        }
    }
    Ok(results)
}

/// Apply provider plans, then move Grok hook backups out of Grok's live hook
/// directory. A failed relocation restores the moved files and config plans.
pub fn apply_plans_with_grok_backup_relocation(
    plans: &[HookPlan],
    grok_hooks_dir: &Path,
) -> io::Result<Vec<ApplyResult>> {
    let mut results = apply_plans_atomically(plans)?;
    let backup_dir = grok_hooks_dir
        .parent()
        .unwrap_or_else(|| Path::new("."))
        .join("sidepulse-hook-backups");
    let mut moved: Vec<(PathBuf, PathBuf)> = Vec::new();
    let relocation = (|| -> io::Result<()> {
        let entries = match fs::read_dir(grok_hooks_dir) {
            Ok(entries) => entries,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(()),
            Err(error) => return Err(error),
        };
        let mut sources = entries
            .filter_map(Result::ok)
            .map(|entry| entry.path())
            .filter(|path| {
                path.file_name().is_some_and(|name| {
                    let name = name.to_string_lossy();
                    name.starts_with("sidepulse") && name.contains(".json.bak.")
                }) && fs::symlink_metadata(path).is_ok_and(|metadata| metadata.is_file())
            })
            .collect::<Vec<_>>();
        sources.sort();
        if sources.is_empty() {
            return Ok(());
        }
        fs::create_dir_all(&backup_dir)?;
        for (index, source) in sources.iter().enumerate() {
            let name = source.file_name().unwrap().to_string_lossy();
            let mut destination = backup_dir.join(name.as_ref());
            if fs::symlink_metadata(&destination).is_ok() {
                let stamp = SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .map_err(io::Error::other)?
                    .as_nanos();
                destination = backup_dir.join(format!("{name}.{stamp}.{index}"));
            }
            fs::rename(source, &destination)?;
            moved.push((source.clone(), destination));
        }
        Ok(())
    })();
    if let Err(error) = relocation {
        let mut rollback_errors = Vec::new();
        for (source, destination) in moved.iter().rev() {
            if let Err(rollback_error) = fs::rename(destination, source) {
                rollback_errors.push(format!("{}: {rollback_error}", source.display()));
            }
        }
        for plan in plans.iter().rev() {
            if let Err(rollback_error) = plan.rollback() {
                rollback_errors.push(format!("{}: {rollback_error}", plan.provider));
            }
        }
        if rollback_errors.is_empty() {
            return Err(error);
        }
        return Err(io::Error::other(format!(
            "{error}; rollback failed: {}",
            rollback_errors.join("; ")
        )));
    }
    for result in &mut results {
        if let Some(path) = &result.backup_path
            && let Some((_, destination)) = moved.iter().find(|(source, _)| source == path)
        {
            result.backup_path = Some(destination.clone());
        }
    }
    Ok(results)
}

fn clean_cursor_entries(entries: Vec<Value>) -> Vec<Value> {
    entries
        .into_iter()
        .filter(|entry| {
            !entry
                .get("command")
                .and_then(Value::as_str)
                .is_some_and(is_managed_command)
        })
        .collect()
}

fn clean_nested_entries(entries: Vec<Value>) -> Vec<Value> {
    entries
        .into_iter()
        .filter_map(|mut entry| {
            let Some(object) = entry.as_object_mut() else {
                return Some(entry);
            };
            let Some(Value::Array(hooks)) = object.get_mut("hooks") else {
                return Some(entry);
            };
            hooks.retain(|hook| {
                !hook
                    .get("command")
                    .and_then(Value::as_str)
                    .is_some_and(is_managed_command)
            });
            (!hooks.is_empty()).then_some(entry)
        })
        .collect()
}

fn is_managed_command(command: &str) -> bool {
    [
        "sidepulse-next-hook",
        "sidepulse hook-log",
        "agent-monitor hook-log",
        "hook_entry.py",
        "sidepulse.cursor_hook",
    ]
    .iter()
    .any(|marker| command.contains(marker))
}

fn has_managed_command(value: Option<&Value>) -> bool {
    match value {
        Some(Value::Object(object)) => {
            object
                .get("command")
                .and_then(Value::as_str)
                .is_some_and(is_managed_command)
                || object
                    .values()
                    .any(|child| has_managed_command(Some(child)))
        }
        Some(Value::Array(values)) => values.iter().any(|child| has_managed_command(Some(child))),
        _ => false,
    }
}

fn hook_command(provider: &str, event: &str, executable: &Path, log: &Path) -> String {
    let mut parts = vec![
        quote_argument(&executable.to_string_lossy()),
        "--provider".into(),
        provider.into(),
    ];
    if provider == "cursor" {
        parts.push("--event".into());
        parts.push(event.into());
    }
    parts.push("--log".into());
    parts.push(quote_argument(&log.to_string_lossy()));
    parts.join(" ")
}

#[cfg(unix)]
fn quote_argument(value: &str) -> String {
    format!("'{}'", value.replace('\'', "'\\''"))
}

#[cfg(windows)]
fn quote_argument(value: &str) -> String {
    format!("\"{}\"", value.replace('"', "\\\""))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};

    static NEXT_ID: AtomicUsize = AtomicUsize::new(0);

    fn scratch_dir() -> PathBuf {
        loop {
            let path = std::env::temp_dir().join(format!(
                "sidepulse-hook-config-test-{}-{}-{}",
                std::process::id(),
                SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .unwrap()
                    .as_nanos(),
                NEXT_ID.fetch_add(1, Ordering::Relaxed)
            ));
            match fs::create_dir(&path) {
                Ok(()) => return path,
                Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {}
                Err(error) => panic!("scratch directory: {error}"),
            }
        }
    }

    #[test]
    fn captured_python_configs_preserve_provider_behavior_across_upgrade_and_removal() {
        fn normalize(value: &mut Value) {
            match value {
                Value::Object(fields) => {
                    for (key, value) in fields {
                        if key == "command"
                            && value.as_str().is_some_and(|command| {
                                command.contains("hook_entry.py")
                                    || command.contains("sidepulse-next-hook")
                            })
                        {
                            *value = json!("<managed>");
                        } else {
                            normalize(value);
                        }
                    }
                }
                Value::Array(values) => {
                    for value in values {
                        normalize(value);
                    }
                }
                _ => {}
            }
        }
        fn parse(provider: &str, text: &str) -> Value {
            if provider == "codex" {
                serde_json::to_value(toml::from_str::<toml::Value>(text).unwrap()).unwrap()
            } else {
                serde_json::from_str(text).unwrap()
            }
        }
        let cases: Value =
            serde_json::from_str(include_str!("../resources/parity/python-hook-configs.json"))
                .unwrap();
        for case in cases.as_array().unwrap() {
            let provider = case["provider"].as_str().unwrap();
            let directory = scratch_dir();
            let config = directory.join("config");
            let log = directory.join("events.jsonl");
            let executable = directory.join("sidepulse-next-hook");
            fs::write(&config, case["original"].as_str().unwrap()).unwrap();
            let plan = |action| {
                if provider == "codex" {
                    plan_codex_hooks(&config, &log, &executable, action)
                } else {
                    plan_json_hooks(provider, &config, &log, &executable, action)
                }
                .unwrap()
            };
            let installed = plan(Action::Install);
            let mut actual = parse(provider, &installed.updated);
            normalize(&mut actual);
            assert_eq!(actual, case["installed"], "install {provider}");
            let applied = installed.apply().unwrap();
            assert_eq!(
                fs::read_to_string(applied.backup_path.unwrap()).unwrap(),
                case["original"].as_str().unwrap()
            );
            assert!(!plan(Action::Install).changed, "idempotent {provider}");
            let removed = plan(Action::Uninstall);
            let mut actual = parse(provider, &removed.updated);
            normalize(&mut actual);
            assert_eq!(actual, case["removed"], "remove {provider}");
            removed.apply().unwrap();
            assert!(!plan(Action::Uninstall).changed, "removed {provider}");
            fs::remove_dir_all(directory).unwrap();
        }
    }

    #[test]
    fn install_keeps_unrelated_claude_hooks_and_is_idempotent() {
        let dir = scratch_dir();
        let config = dir.join("settings.json");
        fs::write(&config, r#"{"theme":"dark","hooks":{"PreToolUse":[{"matcher":"*","hooks":[{"type":"command","command":"my-other-hook"},{"type":"command","command":"python hook_entry.py --provider claude --log /old"}]}]}}"#).unwrap();
        let log = dir.join("agent.jsonl");
        let hook = dir.join("sidepulse-next-hook");
        let plan = plan_json_hooks("claude", &config, &log, &hook, Action::Install).unwrap();
        assert!(plan.changed);
        let result = plan.apply().unwrap();
        assert!(result.backup_path.unwrap().exists());
        let data: Value = serde_json::from_slice(&fs::read(&config).unwrap()).unwrap();
        assert_eq!(data["theme"], "dark");
        assert_eq!(data["hooks"]["PreToolUse"].as_array().unwrap().len(), 2);
        assert_eq!(
            data["hooks"]["PreToolUse"][0]["hooks"][0]["command"],
            "my-other-hook"
        );
        assert!(
            data["hooks"]["PreToolUse"][1]["hooks"][0]["command"]
                .as_str()
                .unwrap()
                .contains("sidepulse-next-hook")
        );
        assert!(
            !plan_json_hooks("claude", &config, &log, &hook, Action::Install)
                .unwrap()
                .changed
        );
        let uninstall = plan_json_hooks("claude", &config, &log, &hook, Action::Uninstall).unwrap();
        uninstall.apply().unwrap();
        let data: Value = serde_json::from_slice(&fs::read(&config).unwrap()).unwrap();
        assert_eq!(
            data["hooks"]["PreToolUse"][0]["hooks"][0]["command"],
            "my-other-hook"
        );
        assert!(data["hooks"]["SessionStart"].is_null());
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn cursor_plan_preserves_unknown_entries_and_detects_external_edit() {
        let dir = scratch_dir();
        let config = dir.join("hooks.json");
        fs::write(&config, r#"{"hooks":{"stop":[{"future_format":true}]}}"#).unwrap();
        let plan = plan_json_hooks(
            "cursor",
            &config,
            &dir.join("cursor.jsonl"),
            &dir.join("sidepulse-next-hook"),
            Action::Install,
        )
        .unwrap();
        let data: Value = serde_json::from_str(&plan.updated).unwrap();
        assert_eq!(data["version"], 1);
        assert_eq!(data["hooks"]["stop"][0]["future_format"], true);
        assert!(
            data["hooks"]["stop"][1]["command"]
                .as_str()
                .unwrap()
                .contains("--event stop")
        );
        fs::write(&config, r#"{"hooks":{},"new_setting":true}"#).unwrap();
        assert_eq!(
            plan.apply().unwrap_err().kind(),
            io::ErrorKind::AlreadyExists
        );
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn codex_plan_preserves_other_hooks_and_round_trips() {
        let dir = scratch_dir();
        let config = dir.join("config.toml");
        fs::write(&config, "[features]\nhooks = false\n\n[[hooks.Stop]]\nmatcher = \"*\"\n[[hooks.Stop.hooks]]\ncommand = \"user-hook\"\n").unwrap();
        let log = dir.join("codex.jsonl");
        let executable = dir.join("sidepulse-next-hook");
        let plan = plan_codex_hooks(&config, &log, &executable, Action::Install).unwrap();
        assert!(plan.changed);
        assert!(plan.updated.contains("hooks = true"));
        assert!(plan.updated.contains("command = \"user-hook\""));
        assert!(plan.updated.contains("[[hooks.Interrupt]]"));
        plan.apply().unwrap();
        let repeated = plan_codex_hooks(&config, &log, &executable, Action::Install).unwrap();
        assert_eq!(repeated.updated, fs::read_to_string(&config).unwrap());
        let uninstall = plan_codex_hooks(&config, &log, &executable, Action::Uninstall).unwrap();
        uninstall.apply().unwrap();
        let text = fs::read_to_string(&config).unwrap();
        assert!(text.contains("command = \"user-hook\""));
        assert!(!text.contains("sidepulse-next-hook"));
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn uninstall_without_managed_hooks_preserves_original_bytes() {
        let dir = scratch_dir();
        let json_path = dir.join("settings.json");
        let json_text = "{ \"hooks\": {}, \"theme\": \"dark\" }\n";
        fs::write(&json_path, json_text).unwrap();
        let plan = plan_json_hooks(
            "claude",
            &json_path,
            &dir.join("log"),
            &dir.join("hook"),
            Action::Uninstall,
        )
        .unwrap();
        assert!(!plan.changed);
        assert_eq!(plan.updated, json_text);
        let toml_path = dir.join("config.toml");
        let toml_text = "[features]\nhooks = true\n\n";
        fs::write(&toml_path, toml_text).unwrap();
        let plan = plan_codex_hooks(
            &toml_path,
            &dir.join("log"),
            &dir.join("hook"),
            Action::Uninstall,
        )
        .unwrap();
        assert!(!plan.changed);
        assert_eq!(plan.updated, toml_text);
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn multi_provider_apply_restores_earlier_config_on_conflict() {
        let dir = scratch_dir();
        let first = dir.join("claude.json");
        let second = dir.join("cursor.json");
        let original = b"{\"theme\":\"dark\"}\n";
        fs::write(&first, original).unwrap();
        fs::write(&second, "{}\n").unwrap();
        let plans = [
            plan_json_hooks(
                "claude",
                &first,
                &dir.join("claude.jsonl"),
                &dir.join("sidepulse-next-hook"),
                Action::Install,
            )
            .unwrap(),
            plan_json_hooks(
                "cursor",
                &second,
                &dir.join("cursor.jsonl"),
                &dir.join("sidepulse-next-hook"),
                Action::Install,
            )
            .unwrap(),
        ];
        fs::write(&second, "{\"changed\":true}\n").unwrap();
        assert_eq!(
            apply_plans_atomically(&plans).unwrap_err().kind(),
            io::ErrorKind::AlreadyExists
        );
        assert_eq!(fs::read(&first).unwrap(), original);
        assert_eq!(fs::read_to_string(&second).unwrap(), "{\"changed\":true}\n");
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn batch_moves_grok_backups_out_of_live_hooks_directory() {
        let dir = scratch_dir();
        let hooks = dir.join(".grok/hooks");
        fs::create_dir_all(&hooks).unwrap();
        let backup = hooks.join("sidepulse.json.bak.old");
        fs::write(&backup, b"old hook config").unwrap();
        let config = hooks.join("sidepulse.json");
        fs::write(&config, "{}\n").unwrap();
        let plan = plan_json_hooks(
            "grok",
            &config,
            &dir.join("grok.jsonl"),
            &dir.join("hook"),
            Action::Install,
        )
        .unwrap();
        let results = apply_plans_with_grok_backup_relocation(&[plan], &hooks).unwrap();
        assert!(results[0].changed);
        assert!(!backup.exists());
        assert_eq!(
            fs::read(dir.join(".grok/sidepulse-hook-backups/sidepulse.json.bak.old")).unwrap(),
            b"old hook config"
        );
        assert!(
            results[0]
                .backup_path
                .as_ref()
                .unwrap()
                .starts_with(dir.join(".grok/sidepulse-hook-backups"))
        );
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn failed_grok_backup_move_rolls_back_provider_config() {
        let dir = scratch_dir();
        let hooks = dir.join(".grok/hooks");
        fs::create_dir_all(&hooks).unwrap();
        fs::write(dir.join(".grok/sidepulse-hook-backups"), b"blocked").unwrap();
        let config = hooks.join("sidepulse.json");
        let original = b"{}\n";
        fs::write(&config, original).unwrap();
        let plan = plan_json_hooks(
            "grok",
            &config,
            &dir.join("grok.jsonl"),
            &dir.join("hook"),
            Action::Install,
        )
        .unwrap();
        assert!(apply_plans_with_grok_backup_relocation(&[plan], &hooks).is_err());
        assert_eq!(fs::read(&config).unwrap(), original);
        fs::remove_dir_all(dir).unwrap();
    }
}

#[cfg(not(any(unix, windows)))]
fn quote_argument(value: &str) -> String {
    format!("\"{}\"", value.replace('"', "\\\""))
}
