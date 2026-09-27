use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AgentMode {
    IdleReady,
    Working,
    ToolRunning,
    WaitingForInput,
    LongTaskProgress,
    BlockedError,
    Completed,
    Unknown,
}

impl AgentMode {
    pub fn priority(self) -> u8 {
        match self {
            Self::BlockedError => 1,
            Self::WaitingForInput => 2,
            Self::ToolRunning => 3,
            Self::LongTaskProgress => 4,
            Self::Working => 5,
            Self::Completed => 6,
            Self::IdleReady => 7,
            Self::Unknown => 99,
        }
    }

    pub fn label(self) -> &'static str {
        match self {
            Self::IdleReady => "Idle / Ready",
            Self::Working => "Working",
            Self::ToolRunning => "Tool Running",
            Self::WaitingForInput => "Waiting for Input",
            Self::LongTaskProgress => "Long Task Progress",
            Self::BlockedError => "Blocked / Error",
            Self::Completed => "Completed",
            Self::Unknown => "Unknown",
        }
    }

    pub fn counts_active(self) -> bool {
        !matches!(self, Self::Completed | Self::IdleReady)
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct HookEvent {
    pub provider: String,
    pub logged_at: DateTime<Utc>,
    pub event_name: String,
    pub raw: Value,
    pub session_id: Option<String>,
    pub turn_id: Option<String>,
    pub agent_id: Option<String>,
    pub cwd: Option<String>,
    pub tool_name: Option<String>,
    pub message: Option<String>,
    pub origin: Option<String>,
}

impl HookEvent {
    pub fn status_key(&self) -> String {
        if let Some(id) = &self.agent_id {
            format!("{}:agent:{id}", self.provider)
        } else if let Some(id) = &self.session_id {
            format!("{}:session:{id}", self.provider)
        } else {
            format!("{}:unknown", self.provider)
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AgentStatus {
    pub provider: String,
    pub agent_id: String,
    pub display_name: String,
    pub mode: AgentMode,
    pub updated_at: DateTime<Utc>,
    pub event_name: String,
    pub session_id: Option<String>,
    pub cwd: Option<String>,
    pub tool_name: Option<String>,
    pub message: Option<String>,
    pub origin: Option<String>,
    #[serde(default)]
    pub stale: bool,
}

impl AgentStatus {
    pub fn age_seconds(&self, now: DateTime<Utc>) -> f64 {
        ((now - self.updated_at).num_milliseconds() as f64 / 1000.0).max(0.0)
    }

    pub fn legacy_json(&self, now: DateTime<Utc>) -> Value {
        serde_json::json!({
            "provider": self.provider,
            "agent_id": self.agent_id,
            "display_name": self.display_name,
            "mode": self.mode,
            "mode_label": self.mode.label(),
            "priority": self.mode.priority(),
            "updated_at": self.updated_at.to_rfc3339(),
            "age_seconds": (self.age_seconds(now) * 1000.0).round() / 1000.0,
            "event_name": self.event_name,
            "session_id": self.session_id,
            "cwd": self.cwd,
            "tool_name": self.tool_name,
            "message": self.message,
            "origin": self.origin,
            "stale": self.stale,
        })
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AggregateStatus {
    pub mode: AgentMode,
    pub active_count: usize,
    pub stale_count: usize,
    pub representative: Option<AgentStatus>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct MonitorSnapshot {
    pub aggregate: AggregateStatus,
    pub statuses: Vec<AgentStatus>,
    pub stale_statuses: Vec<AgentStatus>,
    pub collected_at: DateTime<Utc>,
}
