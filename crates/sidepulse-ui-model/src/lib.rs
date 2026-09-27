//! Shared presentation state for platform tray and settings adapters.
//! This crate reads service snapshots; it never opens hooks, monitors logs,
//! writes device output, or imports a GUI toolkit.

use sidepulse_core::{AgentMode, AgentStatus, DeviceInfo, MonitorSnapshot, ServerPayload};

pub fn device_display_name(device: &DeviceInfo) -> String {
    if let Some(label) = device.label.as_deref().filter(|label| !label.is_empty()) {
        return label.to_owned();
    }
    std::path::Path::new(&device.root).file_name().map_or_else(
        || device.root.clone(),
        |name| name.to_string_lossy().into_owned(),
    )
}

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

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TrayControls {
    pub brightness: Option<u8>,
    pub display_mode: Option<String>,
    pub codex_transcripts: bool,
    pub claude_transcripts: bool,
    pub sleep_policy: Option<String>,
}

impl TrayControls {
    pub fn from_service_payload(payload: &ServerPayload) -> Option<Self> {
        let ServerPayload::Settings {
            settings,
            active_device,
            brightness,
            display_mode,
        } = payload
        else {
            return None;
        };
        let monitoring = settings.get("transcript_monitoring");
        Some(Self {
            brightness: active_device.as_ref().and(*brightness),
            display_mode: active_device.as_ref().and(display_mode.clone()),
            codex_transcripts: monitoring
                .and_then(|value| value.get("codex"))
                .and_then(|value| value.as_bool())
                .unwrap_or(false),
            claude_transcripts: monitoring
                .and_then(|value| value.get("claude"))
                .and_then(|value| value.as_bool())
                .unwrap_or(false),
            sleep_policy: Some(
                settings
                    .get("sleep_prevention_policy")
                    .and_then(|value| value.as_str())
                    .unwrap_or("agents")
                    .to_owned(),
            ),
        })
    }
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
    pub fn disconnected() -> Self {
        Self {
            icon: StatusIcon::Unknown,
            title: "SidePulse".to_owned(),
            tooltip: "SidePulse service unavailable".to_owned(),
            active_count: 0,
            rows: Vec::new(),
            stale_rows: Vec::new(),
        }
    }

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
    use serde_json::json;
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
        let disconnected = TrayState::disconnected();
        assert_eq!(disconnected.title, "SidePulse");
        assert!(disconnected.rows.is_empty());
        assert_eq!(disconnected.icon, StatusIcon::Unknown);
    }

    #[test]
    fn service_settings_map_to_portable_tray_controls() {
        let payload = ServerPayload::Settings {
            settings: json!({
                "transcript_monitoring": {"codex": true, "claude": false},
                "sleep_prevention_policy": "always"
            }),
            active_device: Some("/Volumes/SidePulse".into()),
            brightness: Some(128),
            display_mode: Some("battery".into()),
        };
        let controls = TrayControls::from_service_payload(&payload).unwrap();
        assert_eq!(controls.brightness, Some(128));
        assert_eq!(controls.display_mode.as_deref(), Some("battery"));
        assert!(controls.codex_transcripts);
        assert!(!controls.claude_transcripts);
        assert_eq!(controls.sleep_policy.as_deref(), Some("always"));

        let disconnected_device = ServerPayload::Settings {
            settings: json!({
                "transcript_monitoring": {"codex": true},
            }),
            active_device: None,
            brightness: Some(128),
            display_mode: Some("battery".into()),
        };
        let controls = TrayControls::from_service_payload(&disconnected_device).unwrap();
        assert_eq!(controls.brightness, None);
        assert_eq!(controls.display_mode, None);
    }

    #[test]
    fn windows_volume_label_is_presented_instead_of_drive_letter() {
        let device = DeviceInfo {
            root: "D:\\".into(),
            target: "D:\\LEDS.LED".into(),
            reason: "volume label matches device".into(),
            label: Some("SidePulse Dot".into()),
        };
        assert_eq!(device_display_name(&device), "SidePulse Dot");
    }
}
