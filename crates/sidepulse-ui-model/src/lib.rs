//! Shared presentation state for platform tray and settings adapters.
//! This crate reads service snapshots; it never opens hooks, monitors logs,
//! writes device output, or imports a GUI toolkit.

use serde::{Deserialize, Serialize};
use sidepulse_core::{
    AgentMode, AgentStatus, DeviceInfo, MonitorSnapshot, PhoneLinkSummary, ServerPayload,
};

pub fn device_display_name(device: &DeviceInfo) -> String {
    let name = device
        .label
        .as_deref()
        .filter(|label| !label.is_empty())
        .map_or_else(
            || {
                std::path::Path::new(&device.root).file_name().map_or_else(
                    || device.root.clone(),
                    |name| name.to_string_lossy().into_owned(),
                )
            },
            str::to_owned,
        );
    let normalized = normalized_device_name(&name);
    if normalized.contains("sidepulsedot") || normalized.contains("pulsedot") {
        "SidePulse Dot".into()
    } else if normalized.contains("sidepulsepro") {
        "SidePulse Pro".into()
    } else if name.is_empty() {
        "SidePulse Device".into()
    } else {
        name
    }
}

fn normalized_device_name(name: &str) -> String {
    name.to_lowercase()
        .chars()
        .filter(|ch| ch.is_alphanumeric())
        .collect()
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
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

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AgentRow {
    pub id: String,
    pub provider: String,
    pub origin: Option<String>,
    pub title: String,
    pub subtitle: String,
    pub icon: StatusIcon,
    pub stale: bool,
    pub can_open: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TrayState {
    pub icon: StatusIcon,
    pub title: String,
    pub tooltip: String,
    pub active_count: usize,
    pub rows: Vec<AgentRow>,
    pub stale_rows: Vec<AgentRow>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TrayControls {
    pub visible: bool,
    pub brightness: Option<u8>,
    pub display_mode: Option<String>,
    pub default_display: String,
    pub codex_transcripts: bool,
    pub claude_transcripts: bool,
    pub sleep_policy: Option<String>,
    pub battery_power_preview: bool,
    pub virtual_display_enabled: bool,
    pub recent_session_retention_seconds: f64,
    pub saved_devices: Vec<TrayDevice>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TrayDevice {
    pub id: String,
    pub name: String,
    pub path: String,
    pub connected: bool,
    pub display: String,
    pub brightness: u8,
    pub linked_phone: bool,
    pub virtual_device: bool,
    pub phone_id: Option<String>,
    pub phone_server: Option<String>,
}

pub fn tray_devices(
    connected: &[DeviceInfo],
    links: &[PhoneLinkSummary],
    controls: &TrayControls,
) -> Vec<TrayDevice> {
    let mut devices = controls
        .saved_devices
        .iter()
        .filter(|device| !device.linked_phone)
        .cloned()
        .collect::<Vec<_>>();
    for device in connected {
        let saved = devices.iter_mut().find(|saved| saved.path == device.root);
        if let Some(saved) = saved {
            saved.connected = true;
            saved.name = device_display_name(device);
        } else {
            devices.push(TrayDevice {
                id: device.root.clone(),
                name: device_display_name(device),
                path: device.root.clone(),
                connected: true,
                display: controls.default_display.clone(),
                brightness: 255,
                linked_phone: false,
                virtual_device: false,
                phone_id: None,
                phone_server: None,
            });
        }
    }
    devices.retain(|device| !device.virtual_device || controls.virtual_display_enabled);
    if controls.virtual_display_enabled && !devices.iter().any(|device| device.virtual_device) {
        devices.push(TrayDevice {
            id: "virtual:status-bar".into(),
            name: "SidePulse Notch".into(),
            path: "virtual:status-bar".into(),
            connected: true,
            display: "agent".into(),
            brightness: 255,
            linked_phone: false,
            virtual_device: true,
            phone_id: None,
            phone_server: None,
        });
    }
    for link in links {
        devices.push(TrayDevice {
            id: format!("ios/{}", link.id),
            name: link.name.clone(),
            path: format!("ios/{}", link.id),
            connected: true,
            display: link.display.clone(),
            brightness: 255,
            linked_phone: true,
            virtual_device: false,
            phone_id: Some(link.id.clone()),
            phone_server: Some(link.server.clone()),
        });
    }
    devices.sort_by(|a, b| {
        (!a.connected, a.name.to_lowercase(), &a.path).cmp(&(
            !b.connected,
            b.name.to_lowercase(),
            &b.path,
        ))
    });
    let mut counts = std::collections::HashMap::new();
    for device in &devices {
        *counts.entry(device.name.clone()).or_insert(0_usize) += 1;
    }
    for device in &mut devices {
        if counts.get(&device.name).copied().unwrap_or(0) > 1 {
            let root_name = std::path::Path::new(&device.path)
                .file_name()
                .map_or(device.path.as_str().into(), |name| name.to_string_lossy());
            let suffix = if normalized_device_name(&root_name)
                .starts_with(&normalized_device_name(&device.name))
            {
                root_name
                    .chars()
                    .skip(device.name.chars().count())
                    .collect::<String>()
                    .trim()
                    .to_owned()
            } else {
                root_name.trim().to_owned()
            };
            if !suffix.is_empty() {
                device.name = format!("{} {suffix}", device.name);
            }
        }
    }
    devices
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
            saved_devices: settings
                .get("devices")
                .and_then(|value| value.as_array())
                .map_or_else(Vec::new, |devices| {
                    devices
                        .iter()
                        .filter_map(|device| {
                            let path = device.get("path")?.as_str()?.to_owned();
                            let id = device
                                .get("id")
                                .and_then(|value| value.as_str())
                                .unwrap_or(&path)
                                .to_owned();
                            let virtual_device = id == "virtual:status-bar";
                            let linked_phone = id.starts_with("ios/");
                            Some(TrayDevice {
                                name: device
                                    .get("name")
                                    .and_then(|value| value.as_str())
                                    .unwrap_or(&id)
                                    .to_owned(),
                                id,
                                path,
                                connected: virtual_device || linked_phone,
                                display: device
                                    .get("led_display")
                                    .and_then(|value| value.as_str())
                                    .or_else(|| {
                                        settings.get("led_display").and_then(|value| value.as_str())
                                    })
                                    .unwrap_or("agent")
                                    .to_owned(),
                                brightness: device
                                    .get("brightness")
                                    .and_then(|value| value.as_u64())
                                    .and_then(|value| u8::try_from(value).ok())
                                    .unwrap_or(255),
                                linked_phone,
                                virtual_device,
                                phone_id: None,
                                phone_server: None,
                            })
                        })
                        .collect()
                }),
            brightness: active_device.as_ref().and(*brightness),
            display_mode: active_device.as_ref().and(display_mode.clone()),
            default_display: settings
                .get("led_display")
                .and_then(|value| value.as_str())
                .unwrap_or("agent")
                .into(),
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

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
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
        label: "Agent Status",
        value: "agent",
    },
    DisplayChoice {
        label: "Battery Level",
        value: "battery",
    },
    DisplayChoice {
        label: "Manual",
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
        label: "When Agents Work",
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
        let mut statuses = snapshot
            .statuses
            .iter()
            .chain(snapshot.stale_statuses.iter().filter(|status| {
                status.mode == AgentMode::Completed
                    && status.age_seconds(snapshot.collected_at) <= retention_seconds
            }))
            .collect::<Vec<_>>();
        statuses.sort_by(|left, right| {
            (
                left.mode.priority(),
                left.agent_id.contains(":agent:"),
                std::cmp::Reverse(left.updated_at),
            )
                .cmp(&(
                    right.mode.priority(),
                    right.agent_id.contains(":agent:"),
                    std::cmp::Reverse(right.updated_at),
                ))
        });
        let mut seen = std::collections::HashSet::new();
        statuses.retain(|status| {
            status
                .session_id
                .as_ref()
                .is_none_or(|id| seen.insert((status.provider.to_lowercase(), id.clone())))
        });
        statuses.truncate(10);
        let mut titles = std::collections::HashMap::new();
        for status in &statuses {
            *titles
                .entry((
                    status.provider.to_lowercase(),
                    normalize_menu_part(&python_session_title(status, false)),
                ))
                .or_insert(0_usize) += 1;
        }
        let mut rows = Vec::new();
        let mut stale_rows = Vec::new();
        for status in statuses {
            let collision = titles
                .get(&(
                    status.provider.to_lowercase(),
                    normalize_menu_part(&python_session_title(status, false)),
                ))
                .copied()
                .unwrap_or(0)
                > 1;
            let row = agent_row(status, collision);
            if status.stale {
                stale_rows.push(row.clone());
            }
            rows.push(row);
        }
        Self {
            icon: mode.into(),
            title,
            tooltip,
            active_count: aggregate.active_count,
            rows,
            stale_rows,
        }
    }
}

fn agent_row(status: &AgentStatus, disambiguate: bool) -> AgentRow {
    let mut parts = vec![status.mode.label().to_owned()];
    if let Some(origin) = status.origin.as_deref().filter(|origin| !origin.is_empty()) {
        parts.push(origin.to_owned());
    }
    if let Some(tool) = status.tool_name.as_deref().filter(|tool| !tool.is_empty()) {
        parts.push(tool.to_owned());
    }
    AgentRow {
        id: status.agent_id.clone(),
        provider: status.provider.clone(),
        origin: status.origin.clone(),
        title: python_session_title(status, disambiguate),
        subtitle: parts.join(" · "),
        icon: status.mode.into(),
        stale: status.stale,
        can_open: !sidepulse_core::session_open_options(status, "").is_empty(),
    }
}

fn normalize_menu_part(text: &str) -> String {
    text.replace(['_', '-'], " ")
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .to_lowercase()
}

fn python_session_title(status: &AgentStatus, disambiguate: bool) -> String {
    let mut title = status.display_name.trim().to_owned();
    if let Some(id) = status.session_id.as_deref() {
        let suffix = format!(" ({})", id.chars().take(8).collect::<String>());
        if title.ends_with(&suffix) {
            title.truncate(title.len() - suffix.len());
        }
    }
    if title.ends_with(')')
        && let Some((prefix, suffix)) = title.rsplit_once(" (")
    {
        let token = &suffix[..suffix.len() - 1];
        if (6..=12).contains(&token.len())
            && token
                .chars()
                .all(|ch| ch.is_ascii_alphanumeric() || ch == '-')
        {
            title = prefix.trim().to_owned();
        }
    }
    let mut project = status.cwd.as_deref().and_then(|cwd| {
        let path = std::path::Path::new(cwd);
        path.ancestors()
            .find(|candidate| candidate.join(".git").exists())
            .unwrap_or(path)
            .file_name()
            .map(|name| name.to_string_lossy().into_owned())
    });
    if let Some(project) = project.as_deref() {
        if let Some(rest) = title.strip_prefix(&format!("{project}: ")) {
            title = rest.to_owned();
        }
    } else if let Some((maybe_project, rest)) = title.split_once(": ") {
        project = Some(maybe_project.to_owned());
        title = rest.to_owned();
    }
    if title.is_empty() {
        title = status.display_name.clone();
    }
    if disambiguate && let Some(id) = &status.session_id {
        title = format!("{title} ({})", id.chars().take(8).collect::<String>());
    }
    if let Some(project) =
        project.filter(|project| normalize_menu_part(project) != normalize_menu_part(&title))
    {
        format!("{title}  {project}")
    } else {
        title
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

    #[test]
    fn tray_sessions_match_python_order_coalescing_and_collision_titles() {
        let now = Utc::now();
        let status = |id: &str, mode: AgentMode, updated_at| AgentStatus {
            provider: "claude".into(),
            agent_id: format!("claude:session:{id}"),
            display_name: "project: Fix build".into(),
            mode,
            updated_at,
            event_name: "Stop".into(),
            session_id: Some(id.into()),
            cwd: Some("/tmp/project".into()),
            tool_name: None,
            message: None,
            origin: None,
            stale: false,
        };
        let older = now - chrono::Duration::seconds(30);
        let mut snapshot = MonitorSnapshot {
            aggregate: sidepulse_core::AggregateStatus {
                mode: AgentMode::WaitingForInput,
                active_count: 2,
                stale_count: 0,
                representative: None,
            },
            statuses: vec![
                status("aaaaaaaa111", AgentMode::Working, older),
                status("bbbbbbbb222", AgentMode::WaitingForInput, now),
                status("aaaaaaaa111", AgentMode::Completed, now),
            ],
            stale_statuses: Vec::new(),
            collected_at: now,
        };
        let state = TrayState::from_snapshot(&snapshot);
        assert_eq!(state.rows.len(), 2);
        assert_eq!(state.rows[0].title, "Fix build (bbbbbbbb)  project");
        assert_eq!(state.rows[1].title, "Fix build (aaaaaaaa)  project");
        snapshot.statuses.clear();
        let empty = TrayState::from_snapshot(&snapshot);
        assert!(empty.rows.is_empty());
    }

    #[test]
    fn tray_devices_include_saved_disconnected_phone_and_virtual_entries() {
        let payload = ServerPayload::Settings {
            settings: json!({
                "virtual_status_device_enabled": true,
                "devices": [
                    {"id":"one","name":"SidePulse Dot","path":"/tmp/one","led_display":"battery","brightness":64},
                    {"id":"two","name":"Old Dot","path":"/tmp/two","led_display":"custom","brightness":128},
                    {"id":"virtual:status-bar","name":"SidePulse Notch","path":"virtual:status-bar","led_display":"agent","brightness":255}
                ]
            }),
            active_device: None,
            brightness: None,
            display_mode: None,
        };
        let controls = TrayControls::from_service_payload(&payload).unwrap();
        let connected = [DeviceInfo {
            root: "/tmp/one".into(),
            target: "/tmp/one/LEDS.LED".into(),
            reason: "test".into(),
            label: Some("SidePulse Dot".into()),
        }];
        let links = [PhoneLinkSummary {
            id: "abc123".into(),
            name: "Peter's iPhone".into(),
            server: "https://example.test".into(),
            linked_at: "".into(),
            display: "custom".into(),
            last_sent_at: None,
            delivery_error: None,
        }];
        let devices = tray_devices(&connected, &links, &controls);
        assert_eq!(devices.len(), 4);
        assert!(devices[0].connected);
        assert_eq!(
            devices
                .iter()
                .find(|device| device.path == "/tmp/one")
                .unwrap()
                .brightness,
            64
        );
        assert!(
            !devices
                .iter()
                .find(|device| device.path == "/tmp/two")
                .unwrap()
                .connected
        );
        assert_eq!(
            devices
                .iter()
                .find(|device| device.linked_phone)
                .unwrap()
                .phone_id
                .as_deref(),
            Some("abc123")
        );
        assert!(devices.iter().any(|device| device.virtual_device));
    }

    #[test]
    fn duplicate_device_names_use_the_python_mount_suffix() {
        let controls = TrayControls::from_service_payload(&ServerPayload::Settings {
            settings: json!({}),
            active_device: None,
            brightness: None,
            display_mode: None,
        })
        .unwrap();
        let connected = ["SidePulse Dot", "SidePulse Dot 2"].map(|name| DeviceInfo {
            root: format!("/Volumes/{name}"),
            target: format!("/Volumes/{name}/LEDS.LED"),
            reason: "test".into(),
            label: None,
        });
        let devices = tray_devices(&connected, &[], &controls);
        assert_eq!(devices[0].name, "SidePulse Dot");
        assert_eq!(devices[1].name, "SidePulse Dot 2");
    }
}
