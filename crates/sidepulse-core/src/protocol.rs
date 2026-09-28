use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::{AgentMode, MonitorSnapshot, PowerSnapshot};

/// IPC data model shared by the service, CLI, and platform UI adapters.
/// Each JSON message occupies one line and is limited to 1 MiB on the wire.
pub const PROTOCOL_VERSION: u16 = 1;

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ClientRequest {
    pub version: u16,
    pub request_id: u64,
    #[serde(flatten)]
    pub kind: RequestKind,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "command", rename_all = "snake_case")]
pub enum RequestKind {
    Snapshot,
    Settings,
    Power,
    Devices,
    Animations,
    VirtualDisplay,
    SetVirtualDisplay {
        patch: VirtualDisplaySettingsPatch,
    },
    SetAgentAnimation {
        mode: AgentMode,
        style: String,
        custom_program: Option<String>,
    },
    SelectDevice {
        root: String,
    },
    SetBrightness {
        brightness: u8,
    },
    SetDisplayMode {
        mode: String,
    },
    SetBatterySettings {
        patch: BatterySettingsPatch,
    },
    SetAgentListSettings {
        patch: AgentListSettingsPatch,
    },
    SetSleepSettings {
        patch: SleepSettingsPatch,
    },
    SetSleepPolicy {
        policy: String,
    },
    SetTranscriptMonitoring {
        provider: String,
        enabled: bool,
    },
    Subscribe,
    IngestHook {
        provider: String,
        line: Value,
    },
    IngestRelay {
        message: Value,
    },
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ServerMessage {
    pub version: u16,
    /// Unsolicited subscription updates have no request ID.
    pub request_id: Option<u64>,
    #[serde(flatten)]
    pub payload: ServerPayload,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ServerPayload {
    Snapshot {
        state: MonitorSnapshot,
    },
    Settings {
        settings: Value,
        active_device: Option<String>,
        brightness: Option<u8>,
        display_mode: Option<String>,
    },
    Power {
        snapshot: PowerSnapshot,
    },
    Devices {
        devices: Vec<DeviceInfo>,
        active_device: Option<String>,
    },
    Animations {
        choices: Vec<AnimationChoice>,
        states: Vec<AgentAnimationState>,
    },
    VirtualDisplay {
        frame: VirtualDisplayFrame,
    },
    StateChanged {
        state: MonitorSnapshot,
    },
    Ack,
    Error {
        code: String,
        message: String,
    },
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DeviceInfo {
    pub root: String,
    pub target: String,
    pub reason: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub label: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AnimationChoice {
    pub id: String,
    pub name: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AgentAnimationState {
    pub mode: AgentMode,
    pub style: String,
    pub program: String,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct VirtualDisplaySettingsPatch {
    pub enabled: Option<bool>,
    pub brightness: Option<u8>,
    pub display: Option<String>,
}

impl VirtualDisplaySettingsPatch {
    pub fn validate(&self) -> Result<(), &'static str> {
        if self
            .display
            .as_deref()
            .is_some_and(|display| !matches!(display, "agent" | "battery" | "custom"))
        {
            Err("invalid virtual display mode")
        } else {
            Ok(())
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct VirtualDisplayFrame {
    pub enabled: bool,
    pub display: String,
    pub pixels: Vec<[u8; 3]>,
}

#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum ChargerBaseline {
    Auto,
    Watts { watts: f64 },
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct BatterySettingsPatch {
    pub display: Option<String>,
    pub full_charge_watts: Option<ChargerBaseline>,
    pub show_on_power_change: Option<bool>,
    pub power_change_preview_seconds: Option<f64>,
}

impl BatterySettingsPatch {
    pub fn validate(&self) -> Result<(), &'static str> {
        if self
            .display
            .as_deref()
            .is_some_and(|mode| !matches!(mode, "agent" | "battery" | "custom"))
        {
            return Err("invalid display mode");
        }
        if let Some(ChargerBaseline::Watts { watts }) = self.full_charge_watts
            && (!watts.is_finite() || watts <= 0.0)
        {
            return Err("invalid charger wattage");
        }
        if self
            .power_change_preview_seconds
            .is_some_and(|seconds| !seconds.is_finite() || seconds < 0.0)
        {
            return Err("invalid power-change preview duration");
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct AgentListSettingsPatch {
    pub idle_timeout_seconds: Option<f64>,
    pub recent_session_retention_seconds: Option<f64>,
}

impl AgentListSettingsPatch {
    pub fn validate(&self) -> Result<(), &'static str> {
        if [
            self.idle_timeout_seconds,
            self.recent_session_retention_seconds,
        ]
        .into_iter()
        .flatten()
        .any(|seconds| !seconds.is_finite() || seconds < 0.0)
        {
            Err("invalid agent-list duration")
        } else {
            Ok(())
        }
    }
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct SleepSettingsPatch {
    pub policy: Option<String>,
    pub min_battery_percent: Option<f64>,
}

impl SleepSettingsPatch {
    pub fn validate(&self) -> Result<(), &'static str> {
        if self
            .policy
            .as_deref()
            .is_some_and(|policy| !matches!(policy, "never" | "agents" | "always"))
        {
            return Err("invalid sleep policy");
        }
        if self
            .min_battery_percent
            .is_some_and(|percent| !percent.is_finite() || !(0.0..=100.0).contains(&percent))
        {
            return Err("invalid sleep battery safeguard");
        }
        Ok(())
    }
}

impl ClientRequest {
    pub fn validate(&self) -> Result<(), &'static str> {
        if self.version != PROTOCOL_VERSION {
            return Err("unsupported protocol version");
        }
        if let RequestKind::IngestHook { provider, line } = &self.kind
            && (provider.is_empty() || !line.is_object())
        {
            return Err("invalid hook event");
        }
        if let RequestKind::IngestRelay { message } = &self.kind
            && !message.is_object()
        {
            return Err("invalid relay event");
        }
        if let RequestKind::SetDisplayMode { mode } = &self.kind
            && !matches!(mode.as_str(), "agent" | "battery" | "custom")
        {
            return Err("invalid display mode");
        }
        if let RequestKind::SetBatterySettings { patch } = &self.kind {
            patch.validate()?;
        }
        if let RequestKind::SetVirtualDisplay { patch } = &self.kind {
            patch.validate()?;
        }
        if let RequestKind::SetAgentListSettings { patch } = &self.kind {
            patch.validate()?;
        }
        if let RequestKind::SetSleepSettings { patch } = &self.kind {
            patch.validate()?;
        }
        if let RequestKind::SetSleepPolicy { policy } = &self.kind
            && !matches!(policy.as_str(), "never" | "agents" | "always")
        {
            return Err("invalid sleep policy");
        }
        if let RequestKind::SetTranscriptMonitoring { provider, .. } = &self.kind
            && !matches!(provider.as_str(), "codex" | "claude")
        {
            return Err("invalid transcript provider");
        }
        if let RequestKind::SelectDevice { root } = &self.kind
            && root.is_empty()
        {
            return Err("invalid device root");
        }
        if let RequestKind::SetAgentAnimation {
            style,
            custom_program,
            ..
        } = &self.kind
            && (style.is_empty()
                || style.len() > 128
                || custom_program
                    .as_ref()
                    .is_some_and(|program| program.len() > 65536))
        {
            return Err("invalid animation setting");
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accepts_device_payloads_without_optional_volume_label() {
        let device: DeviceInfo = serde_json::from_str(
            r#"{"root":"D:\\","target":"D:\\LEDS.LED","reason":"contains LEDS.LED"}"#,
        )
        .unwrap();
        assert_eq!(device.label, None);
    }

    #[test]
    fn round_trips_versioned_hook_request() {
        let input = r#"{"version":1,"request_id":5,"command":"ingest_hook","provider":"codex","line":{"logged_at":"2026-09-26T12:00:00Z","event":{"hook_event_name":"Stop"}}}"#;
        let request: ClientRequest = serde_json::from_str(input).unwrap();
        assert_eq!(request.validate(), Ok(()));
        assert_eq!(
            serde_json::from_str::<ClientRequest>(&serde_json::to_string(&request).unwrap())
                .unwrap(),
            request
        );
    }

    #[test]
    fn rejects_unsupported_version_and_non_object_hook() {
        let request = ClientRequest {
            version: 2,
            request_id: 0,
            kind: RequestKind::Snapshot,
        };
        assert!(request.validate().is_err());
        let request = ClientRequest {
            version: PROTOCOL_VERSION,
            request_id: 0,
            kind: RequestKind::IngestHook {
                provider: "claude".into(),
                line: Value::Null,
            },
        };
        assert!(request.validate().is_err());
    }

    #[test]
    fn accepts_only_supported_display_modes() {
        let mut request = ClientRequest {
            version: PROTOCOL_VERSION,
            request_id: 1,
            kind: RequestKind::SetDisplayMode {
                mode: "battery".into(),
            },
        };
        assert_eq!(request.validate(), Ok(()));
        request.kind = RequestKind::SetDisplayMode {
            mode: "custom".into(),
        };
        assert_eq!(request.validate(), Ok(()));
        request.kind = RequestKind::SetDisplayMode {
            mode: "invalid".into(),
        };
        assert_eq!(request.validate(), Err("invalid display mode"));
        request.kind = RequestKind::SetSleepPolicy {
            policy: "agents".into(),
        };
        assert_eq!(request.validate(), Ok(()));
        request.kind = RequestKind::SetSleepPolicy {
            policy: "automatic".into(),
        };
        assert_eq!(request.validate(), Err("invalid sleep policy"));
        request.kind = RequestKind::SetTranscriptMonitoring {
            provider: "codex".into(),
            enabled: true,
        };
        assert_eq!(request.validate(), Ok(()));
        request.kind = RequestKind::SetTranscriptMonitoring {
            provider: "cursor".into(),
            enabled: true,
        };
        assert_eq!(request.validate(), Err("invalid transcript provider"));
    }
}
