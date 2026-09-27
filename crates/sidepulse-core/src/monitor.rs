use std::collections::{HashMap, HashSet};
use std::sync::LazyLock;

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
    policy: MonitoringPolicy,
}

impl Monitor {
    pub fn new(policy: MonitoringPolicy) -> Self {
        Self {
            statuses: HashMap::new(),
            pending_permissions: HashMap::new(),
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
        let status = status_from_event(event)?;
        let key = status.agent_id.clone();
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
        let mut fresh = Vec::new();
        let mut stale = Vec::new();
        for original in self.statuses.values() {
            let mut status = original.clone();
            if status.mode == AgentMode::Working
                && status.event_name == "PostToolUse"
                && self.policy.post_tool_working_visible_seconds >= 0.0
                && status.age_seconds(now) > self.policy.post_tool_working_visible_seconds
            {
                status.mode = AgentMode::Completed;
            }
            let age = status.age_seconds(now);
            let expired = match status.mode {
                AgentMode::Completed if self.policy.completed_visible_seconds >= 0.0 => {
                    age > self.policy.completed_visible_seconds
                }
                AgentMode::IdleReady if self.policy.idle_visible_seconds >= 0.0 => {
                    age > self.policy.idle_visible_seconds
                }
                AgentMode::ToolRunning if self.policy.tool_running_timeout_seconds > 0.0 => {
                    age > self.policy.stale_after_seconds
                        || age > self.policy.tool_running_timeout_seconds
                }
                _ => age > self.policy.stale_after_seconds,
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
    for key in ["last_assistant_message", "message"] {
        if let Some(mode) = raw.get(key).and_then(Value::as_str).and_then(mode_marker) {
            return Some(mode);
        }
    }
    match event.event_name.as_str() {
        "PostToolUseFailure" | "PermissionDenied" | "StopFailure" => Some(AgentMode::BlockedError),
        "PermissionRequest" => Some(AgentMode::WaitingForInput),
        "Notification" => {
            let kind = raw
                .get("notification_type")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_lowercase();
            let message = raw
                .get("message")
                .and_then(Value::as_str)
                .unwrap_or("")
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
        "Stop" | "SubagentStop" => Some(if event.message.as_deref().is_some_and(asks_question) {
            AgentMode::WaitingForInput
        } else {
            AgentMode::Completed
        }),
        "SessionEnd" => Some(AgentMode::Completed),
        "SessionStart" => Some(AgentMode::IdleReady),
        _ => None,
    }
}

fn status_from_event(event: &HookEvent) -> Option<AgentStatus> {
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
        format!("{provider_label} agent {}", short_id(id))
    } else if let Some(id) = &event.session_id {
        format!("{provider_label} session {}", short_id(id))
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
        origin: event.origin.clone(),
        stale: false,
    })
}

fn short_id(value: &str) -> &str {
    value.get(..value.len().min(8)).unwrap_or(value)
}

fn explicit_mode(value: &str) -> Option<AgentMode> {
    let normalized = value
        .trim()
        .to_lowercase()
        .chars()
        .map(|ch| if ch.is_ascii_alphanumeric() { ch } else { '_' })
        .collect::<String>();
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
    static MARKER: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"(?i)^(?:sidepulse|agent[-_ ]monitor)(?:\s+(?:status|mode))?\s*:\s*(.+)$")
            .unwrap()
    });
    let mut in_code_block = false;
    for line in message.lines() {
        let line = line.trim();
        if line.starts_with("```") {
            in_code_block = !in_code_block;
            continue;
        }
        if in_code_block {
            continue;
        }
        if let Some(body) = line
            .strip_prefix("<!--")
            .and_then(|text| text.strip_suffix("-->"))
            && let Some(captures) = MARKER.captures(body.trim())
            && let Some(mode) = explicit_mode(&captures[1])
        {
            return Some(mode);
        }
        if let Some(body) = line
            .strip_prefix('[')
            .and_then(|text| text.strip_suffix(']'))
            && let Some(captures) = MARKER.captures(body.trim())
            && let Some(mode) = explicit_mode(&captures[1])
        {
            return Some(mode);
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
    }
}
