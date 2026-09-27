//! Shared presentation state for platform tray and settings adapters.
//! This crate reads service snapshots; it never opens hooks, monitors logs,
//! writes device output, or imports a GUI toolkit.

use sidepulse_core::{AgentMode, AgentStatus, MonitorSnapshot};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StatusIcon {
    Idle,
    Working,
    Tool,
    Waiting,
    LongTask,
    Error,
    Completed,
    Unknown,
}

impl From<AgentMode> for StatusIcon {
    fn from(mode: AgentMode) -> Self {
        match mode {
            AgentMode::IdleReady => Self::Idle,
            AgentMode::Working => Self::Working,
            AgentMode::ToolRunning => Self::Tool,
            AgentMode::WaitingForInput => Self::Waiting,
            AgentMode::LongTaskProgress => Self::LongTask,
            AgentMode::BlockedError => Self::Error,
            AgentMode::Completed => Self::Completed,
            AgentMode::Unknown => Self::Unknown,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AgentRow {
    pub id: String,
    pub title: String,
    pub subtitle: String,
    pub icon: StatusIcon,
    pub stale: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TrayState {
    pub icon: StatusIcon,
    pub title: String,
    pub tooltip: String,
    pub active_count: usize,
    pub rows: Vec<AgentRow>,
    pub stale_rows: Vec<AgentRow>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct BrightnessChoice {
    pub label: &'static str,
    pub value: u8,
}

pub const BRIGHTNESS_CHOICES: [BrightnessChoice; 5] = [
    BrightnessChoice {
        label: "Off",
        value: 0,
    },
    BrightnessChoice {
        label: "25%",
        value: 64,
    },
    BrightnessChoice {
        label: "50%",
        value: 128,
    },
    BrightnessChoice {
        label: "75%",
        value: 192,
    },
    BrightnessChoice {
        label: "100%",
        value: 255,
    },
];

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DisplayChoice {
    pub label: &'static str,
    pub value: &'static str,
}

pub const DISPLAY_CHOICES: [DisplayChoice; 2] = [
    DisplayChoice {
        label: "Agent status",
        value: "agent",
    },
    DisplayChoice {
        label: "Battery level",
        value: "battery",
    },
];

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SleepChoice {
    pub label: &'static str,
    pub value: &'static str,
}

pub const SLEEP_CHOICES: [SleepChoice; 3] = [
    SleepChoice {
        label: "Never",
        value: "never",
    },
    SleepChoice {
        label: "While agents work",
        value: "agents",
    },
    SleepChoice {
        label: "Always",
        value: "always",
    },
];

pub fn brightness_label(current: Option<u8>) -> String {
    current.map_or_else(
        || "Device brightness unavailable".into(),
        |value| {
            format!(
                "Device brightness: {}%",
                ((u16::from(value) * 100 + 127) / 255)
            )
        },
    )
}

impl TrayState {
    pub fn from_snapshot(snapshot: &MonitorSnapshot) -> Self {
        let aggregate = &snapshot.aggregate;
        let mode = aggregate.mode;
        let title = match aggregate.active_count {
            0 => "SidePulse".to_owned(),
            1 => mode.label().to_owned(),
            count => format!("{} ({count})", mode.label()),
        };
        let tooltip = if aggregate.active_count == 0 {
            format!("SidePulse: {}", mode.label())
        } else {
            format!(
                "SidePulse: {} · {} active {}",
                mode.label(),
                aggregate.active_count,
                if aggregate.active_count == 1 {
                    "agent"
                } else {
                    "agents"
                }
            )
        };
        Self {
            icon: mode.into(),
            title,
            tooltip,
            active_count: aggregate.active_count,
            rows: snapshot.statuses.iter().map(agent_row).collect(),
            stale_rows: snapshot.stale_statuses.iter().map(agent_row).collect(),
        }
    }
}

fn agent_row(status: &AgentStatus) -> AgentRow {
    let mut parts = vec![status.mode.label().to_owned()];
    if let Some(origin) = status.origin.as_deref().filter(|origin| !origin.is_empty()) {
        parts.push(origin.to_owned());
    }
    if let Some(tool) = status.tool_name.as_deref().filter(|tool| !tool.is_empty()) {
        parts.push(tool.to_owned());
    }
    AgentRow {
        id: status.agent_id.clone(),
        title: status.display_name.clone(),
        subtitle: parts.join(" · "),
        icon: status.mode.into(),
        stale: status.stale,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::Utc;
    use sidepulse_core::{Monitor, parse_log_line};

    #[test]
    fn one_service_snapshot_drives_tray_and_agent_rows() {
        let mut monitor = Monitor::default();
        let event = parse_log_line(
            "claude",
            r#"{"hook_event_name":"PermissionRequest","session_id":"s1","logged_at":"2026-09-27T12:00:00Z","agent_origin":"Claude Code CLI"}"#,
        )
        .unwrap();
        monitor.ingest(&event);
        let snapshot = monitor.snapshot(event.logged_at);
        let state = TrayState::from_snapshot(&snapshot);
        assert_eq!(state.icon, StatusIcon::Waiting);
        assert_eq!(state.active_count, 1);
        assert_eq!(state.rows.len(), 1);
        assert_eq!(state.rows[0].id, "claude:session:s1");
        assert!(state.rows[0].subtitle.contains("Claude Code CLI"));
        assert!(state.stale_rows.is_empty());
        assert!(state.tooltip.contains("1 active agent"));

        let empty = Monitor::default().snapshot(Utc::now());
        assert_eq!(TrayState::from_snapshot(&empty).title, "SidePulse");
    }
}
