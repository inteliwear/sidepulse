use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::{MonitorSnapshot, PowerSnapshot};

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
    SelectDevice { root: String },
    SetBrightness { brightness: u8 },
    SetDisplayMode { mode: String },
    SetSleepPolicy { policy: String },
    SetTranscriptMonitoring { provider: String, enabled: bool },
    Subscribe,
    IngestHook { provider: String, line: Value },
    IngestRelay { message: Value },
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
            && mode != "agent"
            && mode != "battery"
        {
            return Err("invalid display mode");
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
