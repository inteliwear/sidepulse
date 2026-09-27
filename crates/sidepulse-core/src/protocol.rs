use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::MonitorSnapshot;

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
    SetBrightness { brightness: u8 },
    SetDisplayMode { mode: String },
    Subscribe,
    IngestHook { provider: String, line: Value },
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
    StateChanged {
        state: MonitorSnapshot,
    },
    Ack,
    Error {
        code: String,
        message: String,
    },
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
        if let RequestKind::SetDisplayMode { mode } = &self.kind
            && mode != "agent"
            && mode != "battery"
        {
            return Err("invalid display mode");
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

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
    }
}
