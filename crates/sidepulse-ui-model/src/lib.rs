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
    pub can_open: bool,
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

#[derive(Debug, Clone, PartialEq)]
pub struct TrayControls {
    pub visible: bool,
    pub brightness: Option<u8>,
    pub display_mode: Option<String>,
    pub codex_transcripts: bool,
    pub claude_transcripts: bool,
    pub sleep_policy: Option<String>,
    pub battery_power_preview: bool,
    pub virtual_display_enabled: bool,
    pub recent_session_retention_seconds: f64,
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
            visible: settings
                .get("show_menu_bar_icon")
                .and_then(|value| value.as_bool())
                .unwrap_or(true),
            virtual_display_enabled: settings
                .get("virtual_status_device_enabled")
                .and_then(|value| value.as_bool())
                .unwrap_or(false),
            recent_session_retention_seconds: legacy_nonnegative_setting(
                settings,
                "agent_list",
                "recent_session_retention_seconds",
                48.0 * 3600.0,
            ),
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
            battery_power_preview: settings
                .get("battery_monitoring")
                .and_then(|value| value.get("show_on_power_change"))
                .and_then(|value| value.as_bool())
                .unwrap_or(true),
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

#[derive(Debug, Clone, PartialEq)]
pub struct SettingsView {
    pub controls: TrayControls,
    pub full_charge_watts: Option<f64>,
    pub power_change_preview_seconds: f64,
    pub idle_timeout_seconds: f64,
    pub sleep_min_battery_percent: f64,
    pub virtual_display_enabled: bool,
    pub virtual_display_brightness: u8,
    pub virtual_display_mode: String,
    pub session_terminal: String,
    pub custom_terminal_path: String,
    pub session_open_preferences: Vec<(String, sidepulse_core::SessionAction)>,
}

impl SettingsView {
    pub fn from_service_payload(payload: &ServerPayload) -> Option<Self> {
        let controls = TrayControls::from_service_payload(payload)?;
        let ServerPayload::Settings { settings, .. } = payload else {
            return None;
        };
        let battery = settings.get("battery_monitoring");
        let virtual_device = settings
            .get("devices")
            .and_then(|devices| devices.as_array())
            .and_then(|devices| {
                devices.iter().find(|device| {
                    device.get("id").and_then(|id| id.as_str()) == Some("virtual:status-bar")
                        || device.get("path").and_then(|path| path.as_str())
                            == Some("virtual:status-bar")
                })
            });
        Some(Self {
            session_terminal: settings
                .get("session_terminal_app")
                .and_then(|value| value.as_str())
                .unwrap_or("terminal")
                .into(),
            custom_terminal_path: settings
                .get("custom_terminal_path")
                .and_then(|value| value.as_str())
                .unwrap_or("")
                .into(),
            session_open_preferences: ["codex", "claude", "grok"]
                .into_iter()
                .map(|provider| {
                    (
                        provider.into(),
                        sidepulse_core::saved_session_action(settings, provider, None).unwrap_or(
                            match provider {
                                "claude" => sidepulse_core::SessionAction::Vscode,
                                "codex" => sidepulse_core::SessionAction::App,
                                _ => sidepulse_core::SessionAction::Terminal,
                            },
                        ),
                    )
                })
                .collect(),
            virtual_display_enabled: settings
                .get("virtual_status_device_enabled")
                .and_then(|value| value.as_bool())
                .unwrap_or(false),
            virtual_display_brightness: virtual_device
                .and_then(|device| device.get("brightness"))
                .and_then(|value| value.as_u64())
                .and_then(|value| u8::try_from(value).ok())
                .unwrap_or(255),
            virtual_display_mode: virtual_device
                .and_then(|device| device.get("led_display"))
                .and_then(|value| value.as_str())
                .or_else(|| settings.get("led_display").and_then(|value| value.as_str()))
                .unwrap_or("agent")
                .into(),
            idle_timeout_seconds: legacy_nonnegative_setting(
                settings,
                "agent_list",
                "idle_timeout_seconds",
                3600.0,
            ),
            sleep_min_battery_percent: legacy_nonnegative_setting(
                settings,
                "sleep_prevention",
                "min_battery_percent",
                20.0,
            )
            .min(100.0),
            controls,
            full_charge_watts: battery
                .and_then(|value| value.get("full_charge_watts"))
                .and_then(|value| value.as_f64())
                .filter(|watts| watts.is_finite() && *watts > 0.0),
            power_change_preview_seconds: battery
                .and_then(|value| value.get("power_change_preview_seconds"))
                .and_then(|value| value.as_f64())
                .filter(|seconds| seconds.is_finite())
                .unwrap_or(7.0)
                .max(0.0),
        })
    }
}

fn legacy_nonnegative_setting(
    settings: &serde_json::Value,
    group: &str,
    key: &str,
    default: f64,
) -> f64 {
    let legacy_key = if group == "sleep_prevention" {
        "sleep_prevention_min_battery_percent"
    } else {
        key
    };
    settings
        .get(group)
        .and_then(|value| value.get(key))
        .or_else(|| settings.get(legacy_key))
        .and_then(|value| value.as_f64())
        .filter(|value| value.is_finite())
        .unwrap_or(default)
        .max(0.0)
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

pub const DISPLAY_CHOICES: [DisplayChoice; 3] = [
    DisplayChoice {
        label: "Agent status",
        value: "agent",
    },
    DisplayChoice {
        label: "Battery level",
        value: "battery",
    },
    DisplayChoice {
        label: "Manual output",
        value: "custom",
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
        Self::from_snapshot_with_retention(snapshot, 48.0 * 3600.0)
    }

    pub fn from_snapshot_with_retention(
        snapshot: &MonitorSnapshot,
        retention_seconds: f64,
    ) -> Self {
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
            stale_rows: snapshot
                .stale_statuses
                .iter()
                .filter(|status| {
                    status.mode == AgentMode::Completed
                        && status.age_seconds(snapshot.collected_at) <= retention_seconds
                })
                .map(agent_row)
                .collect(),
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
        can_open: !sidepulse_core::session_open_options(status, "").is_empty(),
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
        assert!(controls.visible);
        assert_eq!(controls.brightness, Some(128));
        assert_eq!(controls.display_mode.as_deref(), Some("battery"));
        assert!(controls.codex_transcripts);
        assert!(!controls.claude_transcripts);
        assert_eq!(controls.sleep_policy.as_deref(), Some("always"));

        let disconnected_device = ServerPayload::Settings {
            settings: json!({
                "show_menu_bar_icon": false,
                "transcript_monitoring": {"codex": true},
            }),
            active_device: None,
            brightness: Some(128),
            display_mode: Some("battery".into()),
        };
        let controls = TrayControls::from_service_payload(&disconnected_device).unwrap();
        assert!(!controls.visible);
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

    #[test]
    fn recent_sessions_include_only_completed_sessions_within_saved_retention() {
        let now = Utc::now();
        let mut monitor = Monitor::default();
        for (session, event, age) in [
            ("recent", "Stop", 7200),
            ("old", "Stop", 3 * 86400),
            ("stale-tool", "PreToolUse", 7200),
        ] {
            monitor.ingest(
                &parse_log_line(
                    "claude",
                    &json!({
                        "session_id": session,
                        "hook_event_name": event,
                        "logged_at": (now - chrono::Duration::seconds(age)).to_rfc3339(),
                    })
                    .to_string(),
                )
                .unwrap(),
            );
        }
        let snapshot = monitor.snapshot(now);
        let defaults = TrayState::from_snapshot(&snapshot);
        assert_eq!(defaults.stale_rows.len(), 1);
        assert!(defaults.stale_rows[0].id.ends_with(":recent"));
        assert!(
            TrayState::from_snapshot_with_retention(&snapshot, 3600.0)
                .stale_rows
                .is_empty()
        );
    }
}
