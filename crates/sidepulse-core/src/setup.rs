//! Setup and diagnostics data shared by service clients.
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct HookSetupStatus {
    pub configured: bool,
    pub stage_dir: Option<String>,
    #[serde(default)]
    pub startup_directory: Option<String>,
    pub home: String,
    pub providers: Vec<ProviderHookStatus>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ProviderHookStatus {
    pub provider: String,
    pub config_path: String,
    pub log_path: String,
    pub installed: bool,
    pub native: bool,
    pub error: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DiagnosticFormat {
    Csv,
    Html,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DiagnosticsStatus {
    pub settings_path: Option<String>,
    pub audit_path: Option<String>,
    #[serde(default)]
    pub audit_paths: Vec<String>,
    pub audit_bytes: u64,
    pub history_path: Option<String>,
    pub export_directory: Option<String>,
}
