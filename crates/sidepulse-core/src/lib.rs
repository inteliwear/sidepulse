//! Platform-independent SidePulse event and status model.
//!
//! This crate is intentionally free of GUI and operating-system APIs. During
//! migration it is exercised by `sidepulse-next`; the Python release remains
//! the installed implementation until the delivery gates are met.

mod audit;
mod model;
mod monitor;
mod origin;
mod protocol;
mod provider;

pub use audit::status_audit_record;
pub use model::{AgentMode, AgentStatus, AggregateStatus, HookEvent, MonitorSnapshot};
pub use monitor::{Monitor, MonitoringPolicy, mode_for_event};
pub use origin::{
    AgentOrigin, ProcessInfo, origin_from_environment, origin_from_processes,
    origin_from_terminal_environment, origin_label_from_payload,
};
pub use protocol::{ClientRequest, PROTOCOL_VERSION, RequestKind, ServerMessage, ServerPayload};
pub use provider::{
    canonical_event_name, format_hook_payload, infer_hook_provider, normalize_cursor_payload,
    parse_log_line,
};
