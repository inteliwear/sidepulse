use std::collections::{HashMap, HashSet};
use std::path::Path;
use std::path::PathBuf;
use std::sync::{LazyLock, Mutex};
use std::time::SystemTime;

use chrono::{DateTime, Utc};
use regex::Regex;
use serde_json::Value;

use crate::{AgentMode, AgentStatus, AggregateStatus, HookEvent, MonitorSnapshot};

#[derive(Debug, Clone, Copy)]
pub struct MonitoringPolicy {
    pub stale_after_seconds: f64,
    pub tool_running_timeout_seconds: f64,
    pub completed_visible_seconds: f64,
    pub idle_visible_seconds: f64,
    pub post_tool_working_visible_seconds: f64,
}

impl Default for MonitoringPolicy {
    fn default() -> Self {
        Self {
            stale_after_seconds: 3600.0,
            tool_running_timeout_seconds: 0.0,
            completed_visible_seconds: 20.0 * 60.0,
            idle_visible_seconds: 0.0,
            post_tool_working_visible_seconds: 2.0 * 60.0,
        }
    }
}

#[derive(Debug, Default)]
pub struct Monitor {
    statuses: HashMap<String, AgentStatus>,
    pending_permissions: HashMap<String, HashSet<String>>,
    metadata_by_session: HashMap<String, StatusMetadata>,
    metadata_by_status: HashMap<String, StatusMetadata>,
    policy: MonitoringPolicy,
}

#[derive(Debug, Default, Clone)]
struct StatusMetadata {
    cwd: Option<String>,
    title: Option<String>,
    origin: Option<String>,
}

#[derive(Debug)]
struct CodexIndexCache {
    path: PathBuf,
    modified: Option<SystemTime>,
    size: u64,
    titles: HashMap<String, String>,
}

static CODEX_INDEX: LazyLock<Mutex<Option<CodexIndexCache>>> = LazyLock::new(|| Mutex::new(None));

impl Monitor {
    pub fn new(policy: MonitoringPolicy) -> Self {
        Self {
            statuses: HashMap::new(),
            pending_permissions: HashMap::new(),
            metadata_by_session: HashMap::new(),
            metadata_by_status: HashMap::new(),
            policy,
        }
    }

    pub fn restore_statuses(&mut self, statuses: impl IntoIterator<Item = AgentStatus>) -> usize {
        let mut restored = 0;
        for status in statuses {
            let key = status.agent_id.clone();
            if self
                .statuses
                .get(&key)
                .is_none_or(|previous| previous.updated_at <= status.updated_at)
            {
                self.metadata_by_status.insert(
                    key.clone(),
                    StatusMetadata {
                        cwd: status.cwd.clone(),
                        title: None,
                        origin: status.origin.clone(),
                    },
                );
                self.statuses.insert(key, status);
                restored += 1;
            }
        }
        restored
    }

    pub fn stored_statuses(&self) -> Vec<AgentStatus> {
        let mut statuses = self.statuses.values().cloned().collect::<Vec<_>>();
        statuses.sort_by(|left, right| left.agent_id.cmp(&right.agent_id));
        statuses
    }

    pub fn ingest(&mut self, event: &HookEvent) -> Option<&AgentStatus> {
        let metadata = self.metadata_for_record(event);
        let mut status = status_from_event(event, &metadata)?;
        let key = status.agent_id.clone();
        if metadata.title.is_none()
            && event.cwd.is_none()
            && let Some(previous) = self.statuses.get(&key)
            && previous.session_id == status.session_id
        {
            status.display_name = previous.display_name.clone();
        }
        if self
            .statuses
            .get(&key)
            .is_some_and(|previous| previous.updated_at > status.updated_at)
        {
            return self.statuses.get(&key);
        }
        self.track_pending_permissions(event);
        if let Some(previous) = self.statuses.get(&key) {
            if previous.mode == AgentMode::Completed && status.event_name == "Notification" {
                return self.statuses.get(&key);
            }
            if previous.mode == AgentMode::WaitingForInput
                && previous.event_name == "PermissionRequest"
                && status.event_name != "PermissionRequest"
                && self
                    .pending_permissions
                    .get(&key)
                    .is_some_and(|items| !items.is_empty())
            {
                return self.statuses.get(&key);
            }
        }
        self.statuses.insert(key.clone(), status);
        self.statuses.get(&key)
    }

    fn metadata_for_record(&mut self, event: &HookEvent) -> StatusMetadata {
        let session_metadata = event.session_id.as_ref().map(|session_id| {
            let key = format!("{}:session:{session_id}", event.provider);
            let metadata = self.metadata_by_session.entry(key).or_default();
            update_metadata(metadata, event);
            metadata.clone()
        });
        let status_metadata = self
            .metadata_by_status
            .entry(event.status_key())
            .or_default();
        update_metadata(status_metadata, event);
        if let Some(session_metadata) = session_metadata {
            StatusMetadata {
                cwd: status_metadata.cwd.clone().or(session_metadata.cwd),
                title: status_metadata.title.clone().or(session_metadata.title),
                origin: status_metadata.origin.clone().or(session_metadata.origin),
            }
        } else {
            status_metadata.clone()
        }
    }

    fn track_pending_permissions(&mut self, event: &HookEvent) {
        let key = event.status_key();
        let signature = permission_signature(event);
        match event.event_name.as_str() {
            "PermissionRequest" => {
                if let Some(signature) = signature {
                    self.pending_permissions
                        .entry(key)
                        .or_default()
                        .insert(signature);
                }
            }
            "PostToolUse" => {
                if let Some(signature) = signature
                    && let Some(pending) = self.pending_permissions.get_mut(&key)
                {
                    pending.remove(&signature);
                    if pending.is_empty() {
                        self.pending_permissions.remove(&key);
                    }
                }
            }
            "Stop" | "Interrupt" | "SessionEnd" | "UserPromptSubmit" => {
                self.pending_permissions.remove(&key);
            }
            _ => {}
        }
    }

    pub fn snapshot(&self, now: DateTime<Utc>) -> MonitorSnapshot {
        self.snapshot_with_policy(now, self.policy)
    }

    pub fn snapshot_with_policy(
        &self,
        now: DateTime<Utc>,
        policy: MonitoringPolicy,
    ) -> MonitorSnapshot {
        let mut fresh = Vec::new();
        let mut stale = Vec::new();
        for original in self.statuses.values() {
            let mut status = original.clone();
            if status.mode == AgentMode::Working
                && status.event_name == "PostToolUse"
                && policy.post_tool_working_visible_seconds >= 0.0
                && status.age_seconds(now) > policy.post_tool_working_visible_seconds
            {
                status.mode = AgentMode::Completed;
            }
            let age = status.age_seconds(now);
            let expired = match status.mode {
                AgentMode::Completed if policy.completed_visible_seconds >= 0.0 => {
                    age > policy.completed_visible_seconds
                }
                AgentMode::IdleReady if policy.idle_visible_seconds >= 0.0 => {
                    age > policy.idle_visible_seconds
                }
                AgentMode::ToolRunning if policy.tool_running_timeout_seconds > 0.0 => {
                    age > policy.stale_after_seconds || age > policy.tool_running_timeout_seconds
                }
                _ => age > policy.stale_after_seconds,
            };
            status.stale = expired;
            if expired {
                stale.push(status);
            } else {
                fresh.push(status);
            }
        }
        if fresh.iter().any(|status| status.mode.counts_active()) {
            let inactive: Vec<_> = fresh
                .extract_if(.., |status| !status.mode.counts_active())
                .collect();
            stale.extend(inactive.into_iter().map(|mut status| {
                status.stale = true;
                status
            }));
        }
        let sort = |left: &AgentStatus, right: &AgentStatus| {
            left.mode
                .priority()
                .cmp(&right.mode.priority())
                .then_with(|| right.updated_at.cmp(&left.updated_at))
                .then_with(|| left.agent_id.cmp(&right.agent_id))
        };
        fresh.sort_by(sort);
        stale.sort_by(sort);
        let representative = fresh.first().cloned();
        let aggregate = AggregateStatus {
            mode: representative
                .as_ref()
                .map_or(AgentMode::IdleReady, |status| status.mode),
            active_count: fresh
                .iter()
                .filter(|status| status.mode.counts_active())
                .count(),
            stale_count: stale.len(),
            representative,
        };
        MonitorSnapshot {
            aggregate,
            statuses: fresh,
            stale_statuses: stale,
            collected_at: now,
        }
    }
}

pub fn mode_for_event(event: &HookEvent) -> Option<AgentMode> {
    if event.event_name == "Interrupt" {
        return Some(AgentMode::IdleReady);
    }
    let raw = &event.raw;
    for key in ["sidepulse_status", "sidepulse_mode"] {
        if let Some(mode) = raw.get(key).and_then(Value::as_str).and_then(explicit_mode) {
            return Some(mode);
        }
    }
    let marker_message = raw
        .get("last_assistant_message")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .or_else(|| raw.get("message").and_then(Value::as_str));
    if let Some(mode) = marker_message.and_then(mode_marker) {
        return Some(mode);
    }
    match event.event_name.as_str() {
        "PostToolUseFailure" | "PermissionDenied" | "StopFailure" => Some(AgentMode::BlockedError),
        "PermissionRequest" => Some(AgentMode::WaitingForInput),
        "Notification" => {
            let kind = raw
                .get("notification_type")
                .and_then(Value::as_str)
                .unwrap_or("")
                .trim()
                .to_lowercase();
            let message = raw
                .get("message")
                .and_then(Value::as_str)
                .unwrap_or("")
                .trim()
                .to_lowercase();
            let text = format!("{kind} {message}");
            if [
                "turn complete",
                "turn completed",
                "task complete",
                "task completed",
                "completed successfully",
                "work complete",
                "work completed",
            ]
            .iter()
            .any(|phrase| text.contains(phrase))
                || (kind == "idle_prompt"
                    && ["done", "complete", "completed"].contains(&message.as_str()))
            {
                Some(AgentMode::Completed)
            } else if [
                "waiting for your input",
                "waiting for input",
                "needs your input",
                "needs input",
                "permission",
                "approval",
                "confirm",
            ]
            .iter()
            .any(|phrase| text.contains(phrase))
            {
                Some(AgentMode::WaitingForInput)
            } else {
                Some(AgentMode::Working)
            }
        }
        "PreToolUse" => Some(AgentMode::ToolRunning),
        "PostToolUse" => Some(if tool_response_failed(raw.get("tool_response")) {
            AgentMode::BlockedError
        } else {
            AgentMode::Working
        }),
        "UserPromptSubmit" | "PreCompact" | "PostCompact" | "SubagentStart" => {
            Some(AgentMode::Working)
        }
        "Stop" | "SubagentStop" => Some(
            if raw
                .get("last_assistant_message")
                .and_then(Value::as_str)
                .is_some_and(asks_question)
            {
                AgentMode::WaitingForInput
            } else {
                AgentMode::Completed
            },
        ),
        "SessionEnd" => Some(AgentMode::Completed),
        "SessionStart" => Some(AgentMode::IdleReady),
        _ => None,
    }
}

fn update_metadata(metadata: &mut StatusMetadata, event: &HookEvent) {
    if let Some(cwd) = event.cwd.as_ref().filter(|cwd| !cwd.is_empty()) {
        metadata.cwd = Some(cwd.clone());
    }
    if let Some(title) = codex_session_title(event) {
        metadata.title = Some(title);
    } else if metadata.title.is_none() && event.event_name == "UserPromptSubmit" {
        metadata.title = event
            .raw
            .get("prompt")
            .and_then(Value::as_str)
            .and_then(summarize_prompt);
    }
    if let Some(origin) = &event.origin {
        metadata.origin = Some(origin.clone());
    }
}

fn codex_session_title(event: &HookEvent) -> Option<String> {
    if event.provider != "codex" {
        return None;
    }
    let session_id = event.session_id.as_ref()?;
    let home = std::env::var_os(if cfg!(windows) { "USERPROFILE" } else { "HOME" })?;
    let path = PathBuf::from(home).join(".codex/session_index.jsonl");
    let metadata = std::fs::metadata(&path).ok()?;
    let modified = metadata.modified().ok();
    let mut cache = CODEX_INDEX.lock().ok()?;
    let needs_reload = cache.as_ref().is_none_or(|cached| {
        cached.path != path || cached.modified != modified || cached.size != metadata.len()
    });
    if needs_reload {
        *cache = Some(CodexIndexCache {
            path: path.clone(),
            modified,
            size: metadata.len(),
            titles: read_codex_session_titles(&path),
        });
    }
    cache.as_ref()?.titles.get(session_id).cloned()
}

fn read_codex_session_titles(path: &Path) -> HashMap<String, String> {
    let Ok(content) = std::fs::read_to_string(path) else {
        return HashMap::new();
    };
    let mut titles = HashMap::new();
    let lines = content.lines().collect::<Vec<_>>();
    for line in &lines[lines.len().saturating_sub(5000)..] {
        let Ok(row) = serde_json::from_str::<Value>(line) else {
            continue;
        };
        if let (Some(id), Some(title)) = (
            row.get("id").and_then(Value::as_str),
            row.get("thread_name").and_then(Value::as_str),
        ) {
            let title = title.trim();
            if !id.is_empty() && !title.is_empty() {
                titles.insert(id.to_owned(), truncate_text(title, 72));
            }
        }
    }
    titles
}

fn status_from_event(event: &HookEvent, metadata: &StatusMetadata) -> Option<AgentStatus> {
    let mode = mode_for_event(event)?;
    if event.provider == "codex" {
        let text = [
            event.raw.get("prompt").and_then(Value::as_str),
            event.raw.get("message").and_then(Value::as_str),
            event
                .raw
                .get("last_assistant_message")
                .and_then(Value::as_str),
        ]
        .into_iter()
        .flatten()
        .collect::<Vec<_>>()
        .join(" ")
        .to_lowercase();
        if [
            "generate 0 to 3 hyperpersonalized suggestions",
            "you are an expert at upholding safety and compliance standards",
        ]
        .iter()
        .any(|prompt| text.contains(prompt))
        {
            return None;
        }
    }
    let provider_label = match event.provider.as_str() {
        "codex" => "Codex",
        "claude" => "Claude",
        "grok" => "Grok",
        "cursor" => "Cursor",
        "junie" => "Junie",
        _ => &event.provider,
    };
    let display_name = if let Some(id) = &event.agent_id {
        let short = short_id(id);
        display_name_from_parts(
            metadata.cwd.as_deref().or(event.cwd.as_deref()),
            metadata.title.as_deref(),
            &format!("agent {short}"),
            &format!("{provider_label} agent {short}"),
        )
    } else if let Some(id) = &event.session_id {
        let short = short_id(id);
        display_name_from_parts(
            metadata.cwd.as_deref().or(event.cwd.as_deref()),
            metadata.title.as_deref(),
            &short,
            &format!("{provider_label} session {short}"),
        )
    } else {
        provider_label.to_owned()
    };
    Some(AgentStatus {
        provider: event.provider.clone(),
        agent_id: event.status_key(),
        display_name,
        mode,
        updated_at: event.logged_at,
        event_name: event.event_name.clone(),
        session_id: event.session_id.clone(),
        cwd: event.cwd.clone(),
        tool_name: event.tool_name.clone(),
        message: event.message.clone(),
        origin: event.origin.clone().or_else(|| metadata.origin.clone()),
        stale: false,
    })
}

fn short_id(value: &str) -> String {
    value.chars().take(8).collect()
}

fn summarize_prompt(value: &str) -> Option<String> {
    static MY_REQUEST: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"(?is)##\s+My request for [^:\n]+:\s*(.*)").unwrap());
    static CODE_BLOCK: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?s)```.*?```").unwrap());
    static INLINE_CODE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"`([^`]+)`").unwrap());
    static PATH: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r#"(?:~|/Users|/var|/private|/tmp)/[^\s,;)'"`]+"#).unwrap());
    static TAG: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"<[^>]+>").unwrap());
    static MARKDOWN: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"#+\s*").unwrap());
    static SPACE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\s+").unwrap());

    let mut text = value.trim();
    if text.is_empty() || text.starts_with("<task-notification>") {
        return None;
    }
    if let Some(captures) = MY_REQUEST.captures(text) {
        text = captures.get(1)?.as_str();
    }
    let text = CODE_BLOCK.replace_all(text, " ");
    let text = INLINE_CODE.replace_all(&text, "$1");
    let text = PATH.replace_all(&text, "...");
    let text = TAG.replace_all(&text, " ");
    let text = MARKDOWN.replace_all(&text, " ");
    let text = SPACE.replace_all(&text, " ");
    let text = text.trim_matches(|ch: char| ch.is_whitespace() || "-:".contains(ch));
    if text.is_empty() {
        None
    } else {
        Some(truncate_text(text, 72))
    }
}

fn display_name_from_parts(
    cwd: Option<&str>,
    title: Option<&str>,
    short_id: &str,
    fallback: &str,
) -> String {
    let project = cwd.and_then(project_name);
    let name = match (project.as_deref(), title) {
        (Some(project), Some(title))
            if normalized_name_part(project) == normalized_name_part(title) =>
        {
            format!("{title} ({short_id})")
        }
        (Some(project), Some(title)) => format!("{project}: {title} ({short_id})"),
        (None, Some(title)) => format!("{title} ({short_id})"),
        (Some(project), None) => format!("{project} ({short_id})"),
        (None, None) => return fallback.to_owned(),
    };
    truncate_text(&name, 96)
}

fn normalized_name_part(value: &str) -> String {
    value
        .replace(['_', '-'], " ")
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .to_lowercase()
}

fn project_name(cwd: &str) -> Option<String> {
    let path = Path::new(cwd);
    for candidate in path.ancestors() {
        if candidate.join(".git").exists() {
            return candidate
                .file_name()
                .map(|name| name.to_string_lossy().into_owned())
                .or_else(|| Some(candidate.display().to_string()));
        }
    }
    path.file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .or_else(|| Some(cwd.to_owned()))
}

fn truncate_text(value: &str, max_len: usize) -> String {
    if value.chars().count() <= max_len {
        return value.to_owned();
    }
    let mut trimmed = value.chars().take(max_len - 1).collect::<String>();
    trimmed = trimmed.trim_end().to_owned();
    let boundary = trimmed.rfind([' ', ',', ';']);
    if let Some(boundary) = boundary
        && trimmed[..boundary].chars().count() >= max_len / 2
    {
        trimmed.truncate(boundary);
        trimmed = trimmed.trim_end().to_owned();
    }
    format!("{trimmed}...")
}

fn explicit_mode(value: &str) -> Option<AgentMode> {
    static SEPARATORS: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[^a-z0-9]+").unwrap());
    let lower = value.trim().to_lowercase();
    let normalized = SEPARATORS.replace_all(&lower, "_");
    match normalized.trim_matches('_') {
        "ask" | "question" | "waiting" | "waiting_for_input" | "input" => {
            Some(AgentMode::WaitingForInput)
        }
        "blocked" | "error" | "blocked_error" => Some(AgentMode::BlockedError),
        "working" => Some(AgentMode::Working),
        "tool_running" => Some(AgentMode::ToolRunning),
        "progress" | "long_task_progress" => Some(AgentMode::LongTaskProgress),
        "done" | "complete" | "completed" => Some(AgentMode::Completed),
        "idle" | "ready" | "idle_ready" => Some(AgentMode::IdleReady),
        _ => None,
    }
}

fn mode_marker(message: &str) -> Option<AgentMode> {
    static CODE_BLOCK: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?s)```.*?```").unwrap());
    static MARKERS: LazyLock<[Regex; 3]> = LazyLock::new(|| {
        [
            r"(?im)^\s*<!--\s*(?:sidepulse|agent[-_ ]monitor)\s*:\s*([a-z0-9_ -]+)\s*-->\s*$",
            r"(?im)^\s*<!--\s*(?:sidepulse|agent[-_ ]monitor)\s+(?:status|mode)\s*:\s*([a-z0-9_ -]+)\s*-->\s*$",
            r"(?im)^\s*\[(?:sidepulse|agent[-_ ]monitor)\s+(?:status|mode)\s*:\s*([a-z0-9_ -]+)\]\s*$",
        ].map(|pattern| Regex::new(pattern).unwrap())
    });
    let text = CODE_BLOCK.replace_all(message, "");
    for marker in MARKERS.iter() {
        for captures in marker.captures_iter(&text) {
            if let Some(mode) = explicit_mode(&captures[1]) {
                return Some(mode);
            }
        }
    }
    None
}

fn tool_response_failed(response: Option<&Value>) -> bool {
    let Some(response) = response else {
        return false;
    };
    if let Some(object) = response.as_object() {
        return object.get("interrupted") == Some(&Value::Bool(true))
            || object.get("success") == Some(&Value::Bool(false))
            || object
                .get("exit_code")
                .is_some_and(|value| !value.is_null() && value != 0 && value != false);
    }
    response.as_str().is_some_and(|value| {
        let text = value.to_lowercase();
        text.contains("exit code: 1") || text.contains("traceback")
    })
}

fn asks_question(message: &str) -> bool {
    static CODE_BLOCK: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?s)```.*?```").unwrap());
    static INLINE_CODE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"`[^`\n]*`").unwrap());
    static EMBEDDED_REQUEST: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(
            r"(?:^|[.!?]\s+)(?:want me to|need me to|should i|should we|do you want me to)\b",
        )
        .unwrap()
    });
    let without_blocks = CODE_BLOCK.replace_all(message, "");
    let plain = INLINE_CODE.replace_all(&without_blocks, "");
    let lines: Vec<_> = plain
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty())
        .collect();
    for line in lines.iter().rev().take(8) {
        let lower = line.to_lowercase();
        if ["* cogitated ", "* recap:", "※ recap:", "recap:"]
            .iter()
            .any(|prefix| lower.starts_with(prefix))
        {
            continue;
        }
        if line.ends_with(':')
            || [
                "anything else",
                "any other",
                "all good",
                "need anything else",
                "want anything else",
                "anything you want",
                "anything you'd like",
                "anything else you want",
                "anything else you'd like",
            ]
            .iter()
            .any(|prefix| lower.starts_with(prefix))
        {
            continue;
        }
        if EMBEDDED_REQUEST.is_match(&lower) {
            return true;
        }
        let required = [
            "which ",
            "what ",
            "where ",
            "when ",
            "who ",
            "why ",
            "how ",
            "can you ",
            "could you ",
            "please confirm",
            "please choose",
            "choose ",
            "need me to ",
            "want me to ",
            "should i ",
            "should we ",
            "do you want me to ",
        ];
        let imperative = &required[9..];
        if (line.ends_with('?') && required.iter().any(|prefix| lower.starts_with(prefix)))
            || imperative.iter().any(|prefix| lower.starts_with(prefix))
        {
            return true;
        }
    }
    false
}

fn permission_signature(event: &HookEvent) -> Option<String> {
    let input = event.raw.get("tool_input")?.as_object()?;
    let command = input.get("command")?.as_str()?;
    if command.is_empty() {
        return None;
    }
    Some(format!(
        "{}\0{command}",
        event.tool_name.as_deref().unwrap_or("")
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::parse_log_line;
    use chrono::TimeZone;

    fn event(name: &str) -> HookEvent {
        parse_log_line("codex", &format!(r#"{{"logged_at":"2026-09-26T12:00:00Z","event":{{"hook_event_name":"{name}","session_id":"abc"}}}}"#)).unwrap()
    }

    #[test]
    fn maps_core_lifecycle_events() {
        assert_eq!(
            mode_for_event(&event("SessionStart")),
            Some(AgentMode::IdleReady)
        );
        assert_eq!(
            mode_for_event(&event("PreToolUse")),
            Some(AgentMode::ToolRunning)
        );
        assert_eq!(
            mode_for_event(&event("PostToolUse")),
            Some(AgentMode::Working)
        );
        assert_eq!(
            mode_for_event(&event("PermissionRequest")),
            Some(AgentMode::WaitingForInput)
        );
        assert_eq!(
            mode_for_event(&event("Interrupt")),
            Some(AgentMode::IdleReady)
        );
    }

    #[test]
    fn settles_post_tool_status_then_expires_completion() {
        let start = Utc.with_ymd_and_hms(2026, 9, 26, 12, 0, 0).unwrap();
        let mut monitor = Monitor::default();
        monitor.ingest(&event("PostToolUse"));
        assert_eq!(monitor.snapshot(start).aggregate.mode, AgentMode::Working);
        assert_eq!(
            monitor
                .snapshot(start + chrono::Duration::seconds(121))
                .aggregate
                .mode,
            AgentMode::Completed
        );
        let expired = monitor.snapshot(start + chrono::Duration::minutes(21));
        assert_eq!(expired.aggregate.mode, AgentMode::IdleReady);
        assert_eq!(expired.aggregate.stale_count, 1);
    }

    #[test]
    fn permission_wait_persists_until_matching_tool_result() {
        let parse = |name: &str, command: &str| {
            parse_log_line("claude", &format!(r#"{{"logged_at":"2026-09-26T12:00:00Z","hook_event_name":"{name}","session_id":"a","tool_name":"Shell","tool_input":{{"command":"{command}"}}}}"#)).unwrap()
        };
        let mut monitor = Monitor::default();
        monitor.ingest(&parse("PermissionRequest", "date"));
        monitor.ingest(&parse("PreToolUse", "date"));
        let now = Utc.with_ymd_and_hms(2026, 9, 26, 12, 0, 0).unwrap();
        assert_eq!(
            monitor.snapshot(now).aggregate.mode,
            AgentMode::WaitingForInput
        );
        monitor.ingest(&parse("PostToolUse", "date"));
        assert_eq!(monitor.snapshot(now).aggregate.mode, AgentMode::Working);
    }

    #[test]
    fn final_message_distinguishes_requests_and_casual_closing() {
        assert!(asks_question("Please choose a region."));
        assert!(asks_question("The build passed. Should I deploy?"));
        assert!(!asks_question("All done. Anything else?"));
        assert!(!asks_question(
            "Use `Please choose a region` in the document."
        ));
        assert!(!asks_question("```text\nPlease choose a region\n```"));
    }

    #[test]
    fn explicit_marker_ignores_fenced_examples() {
        assert_eq!(mode_marker("```text\n<!-- sidepulse:ask -->\n```"), None);
        assert_eq!(
            mode_marker("Ready.\n<!-- sidepulse:ask -->"),
            Some(AgentMode::WaitingForInput)
        );
        assert_eq!(
            mode_marker("[sidepulse status: done]"),
            Some(AgentMode::Completed)
        );
        assert_eq!(mode_marker("[sidepulse: ask]"), None);
        assert_eq!(
            mode_marker("[sidepulse status: done]\n<!-- sidepulse: ask -->"),
            Some(AgentMode::WaitingForInput)
        );
        assert_eq!(
            explicit_mode("waiting---for input"),
            Some(AgentMode::WaitingForInput)
        );
    }

    #[test]
    fn final_status_uses_assistant_message_and_marker_precedence() {
        let mut stopped = event("Stop");
        stopped.raw = serde_json::json!({
            "message": "Complete.",
            "last_assistant_message": "Please choose a region."
        });
        assert_eq!(mode_for_event(&stopped), Some(AgentMode::WaitingForInput));
        stopped.raw = serde_json::json!({
            "message": "<!-- sidepulse: ask -->",
            "last_assistant_message": "Complete."
        });
        assert_eq!(mode_for_event(&stopped), Some(AgentMode::Completed));

        let mut notification = event("Notification");
        notification.raw =
            serde_json::json!({"notification_type":" idle_prompt ","message":" done "});
        assert_eq!(mode_for_event(&notification), Some(AgentMode::Completed));
    }

    #[test]
    fn keeps_project_and_prompt_title_across_events_without_context() {
        let directory = tempfile::tempdir().unwrap();
        let project = directory.path().join("sample-project");
        let nested = project.join("src");
        std::fs::create_dir_all(&nested).unwrap();
        std::fs::create_dir(project.join(".git")).unwrap();
        let first = serde_json::json!({
            "logged_at": "2026-09-26T12:00:00Z",
            "hook_event_name": "UserPromptSubmit",
            "session_id": "abcdefgh-1234",
            "cwd": nested,
            "prompt": "Fix the parser in `/tmp/project/src`"
        });
        let second = serde_json::json!({
            "logged_at": "2026-09-26T12:00:01Z",
            "hook_event_name": "PreToolUse",
            "session_id": "abcdefgh-1234",
            "tool_name": "Shell"
        });
        let mut monitor = Monitor::default();
        monitor.ingest(&parse_log_line("claude", &first.to_string()).unwrap());
        monitor.ingest(&parse_log_line("claude", &second.to_string()).unwrap());
        let status = monitor.stored_statuses().remove(0);
        assert_eq!(
            status.display_name,
            "sample-project: Fix the parser in ... (abcdefgh)"
        );
        assert_eq!(status.cwd, None);
    }

    #[test]
    fn ignores_notification_prompts_and_shortens_unicode_by_character() {
        assert_eq!(summarize_prompt("<task-notification>finished"), None);
        assert_eq!(
            summarize_prompt("## My request for Codex:\n### Résumé"),
            Some("Résumé".into())
        );
        assert_eq!(short_id("éééééééé9"), "éééééééé");
    }

    #[test]
    fn codex_session_index_uses_latest_title_within_recent_rows() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("session_index.jsonl");
        std::fs::write(
            &path,
            concat!(
                "{\"id\":\"abc\",\"thread_name\":\"Old title\"}\n",
                "invalid JSON\n",
                "{\"id\":\"abc\",\"thread_name\":\"Updated title\"}\n"
            ),
        )
        .unwrap();
        assert_eq!(
            read_codex_session_titles(&path)
                .get("abc")
                .map(String::as_str),
            Some("Updated title")
        );
    }

    #[test]
    fn restored_status_keeps_its_name_when_next_event_has_no_context() {
        let mut monitor = Monitor::default();
        monitor.restore_statuses([AgentStatus {
            provider: "claude".into(),
            agent_id: "claude:session:abc".into(),
            display_name: "project: Saved title (abc)".into(),
            mode: AgentMode::Working,
            updated_at: Utc.with_ymd_and_hms(2026, 9, 26, 11, 0, 0).unwrap(),
            event_name: "UserPromptSubmit".into(),
            session_id: Some("abc".into()),
            cwd: Some("/tmp/project".into()),
            tool_name: None,
            message: None,
            origin: None,
            stale: false,
        }]);
        let next = parse_log_line(
            "claude",
            r#"{"logged_at":"2026-09-26T12:00:00Z","hook_event_name":"PreToolUse","session_id":"abc"}"#,
        )
        .unwrap();
        monitor.ingest(&next);
        assert_eq!(
            monitor.stored_statuses()[0].display_name,
            "project: Saved title (abc)"
        );
    }
}
