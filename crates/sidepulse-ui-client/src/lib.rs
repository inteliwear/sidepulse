//! Renderer-independent Settings client. The service owns application policy.
//! Native and webview hosts can share these typed states, commands, and updates.

use sidepulse_core::{
    AgentAnimationState, AnimationChoice, ClientRequest, DeviceInfo, MonitorSnapshot,
    PROTOCOL_VERSION, RequestKind, ServerMessage, ServerPayload,
};
use sidepulse_installer::startup::{Job, Operation};
use sidepulse_ui_model::{SettingsView, TrayState};
use std::sync::mpsc::{self, Receiver, Sender};
use std::time::Duration;

pub enum WorkerCommand {
    Request(RequestKind),
    Startup {
        job: Job,
        operation: Operation,
        dry_run: bool,
    },
    #[cfg(target_os = "macos")]
    SleepHelper {
        install: bool,
    },
    #[cfg(target_os = "macos")]
    SleepHelperStatus,
    OpenFile {
        path: String,
    },
}

/// Shared presentation data collected from the service, with no renderer types.
pub struct ServiceState {
    pub setup: sidepulse_core::HookSetupStatus,
    pub diagnostics: sidepulse_core::DiagnosticsStatus,
    pub settings: SettingsView,
    pub activity: TrayState,
    pub agents: Vec<sidepulse_core::AgentStatus>,
    pub devices: Vec<DeviceInfo>,
    pub active_device: Option<String>,
    pub animation_choices: Vec<AnimationChoice>,
    pub animation_states: Vec<AgentAnimationState>,
    pub animation_library: sidepulse_core::AnimationLibrary,
    pub history_points: Vec<sidepulse_core::HistoryPoint>,
    pub history_timeframe: u32,
    pub history_sampled: bool,
    pub lid_durations: [f64; 2],
    pub relay: sidepulse_core::RelaySettings,
    pub phones_configured: bool,
    pub phone_output_enabled: bool,
    pub power_control: sidepulse_core::PowerControlStatus,
    pub phones: Vec<sidepulse_core::PhoneLinkSummary>,
    pub phone_pairing: Option<sidepulse_core::PhonePairingView>,
}

pub enum Update {
    Setup(sidepulse_core::HookSetupStatus),
    Managed(Result<String, String>),
    Exported(Result<(String, usize), String>),
    State(Result<Box<ServiceState>, String>),
    Opened(Result<(), String>),
    ProfileExport(Result<String, String>),
    Saved {
        result: Result<(), String>,
        draft: DraftKind,
    },
}

#[derive(Clone, Copy)]
pub enum DraftKind {
    None,
    Battery,
    Monitoring,
    Sleep,
    Animation,
    Terminal,
    Library,
    LidTiming,
    Relay,
    RelayControl,
    Phone,
}

fn request(endpoint: &str, kind: RequestKind) -> Result<ServerPayload, String> {
    let request = ClientRequest {
        version: PROTOCOL_VERSION,
        request_id: 1,
        kind,
    };
    let response: ServerMessage = sidepulse_ipc::request(
        endpoint,
        &request,
        if matches!(request.kind, RequestKind::ExportDiagnostics { .. }) {
            Duration::from_secs(30)
        } else {
            Duration::from_secs(2)
        },
    )
    .map_err(|error| error.to_string())?;
    if response.version != PROTOCOL_VERSION || response.request_id != Some(1) {
        return Err("The service returned an invalid response.".into());
    }
    if let ServerPayload::Error { message, .. } = response.payload {
        Err(message)
    } else {
        Ok(response.payload)
    }
}

/// Read the Settings view using the same validated protocol on every platform.
pub fn fetch_state(endpoint: &str) -> Result<ServiceState, String> {
    let payload = request(endpoint, RequestKind::Settings)?;
    let settings = SettingsView::from_service_payload(&payload)
        .ok_or("The service did not return settings.")?;
    let ServerPayload::Snapshot { state }: ServerPayload =
        request(endpoint, RequestKind::Snapshot)?
    else {
        return Err("The service did not return activity.".into());
    };
    let snapshot: MonitorSnapshot = state;
    let ServerPayload::Devices {
        devices,
        active_device,
    } = request(endpoint, RequestKind::Devices)?
    else {
        return Err("The service did not return devices.".into());
    };
    let activity = TrayState::from_snapshot_with_retention(
        &snapshot,
        settings.controls.recent_session_retention_seconds,
    );
    let ServerPayload::Animations { choices, states } = request(endpoint, RequestKind::Animations)?
    else {
        return Err("The service did not return animations.".into());
    };
    let ServerPayload::History {
        points,
        timeframe_seconds,
        sampled,
    } = request(endpoint, RequestKind::History)?
    else {
        return Err("The service did not return history.".into());
    };
    let ServerPayload::AnimationLibrary { library } =
        request(endpoint, RequestKind::AnimationLibrary)?
    else {
        return Err("The service did not return animation profiles.".into());
    };
    let ServerPayload::RelaySettings { settings: relay } =
        request(endpoint, RequestKind::RelaySettings)?
    else {
        return Err("The service did not return relay settings.".into());
    };
    let ServerPayload::PhoneLinks {
        configured: phones_configured,
        output_enabled: phone_output_enabled,
        links: phones,
        pairing: phone_pairing,
    } = request(endpoint, RequestKind::PhoneLinks)?
    else {
        return Err("The service did not return phone links.".into());
    };
    let ServerPayload::PowerControl {
        status: power_control,
    } = request(endpoint, RequestKind::PowerControl)?
    else {
        return Err("The service did not return power control status.".into());
    };
    let ServerPayload::HookSetup { status: setup } = request(endpoint, RequestKind::HookSetup)?
    else {
        return Err("The service did not return setup status.".into());
    };
    let ServerPayload::Diagnostics {
        status: diagnostics,
    } = request(endpoint, RequestKind::Diagnostics)?
    else {
        return Err("The service did not return diagnostics.".into());
    };
    Ok(ServiceState {
        setup,
        diagnostics,
        power_control,
        phones_configured,
        phone_output_enabled,
        phones,
        phone_pairing,
        settings,
        relay,
        activity,
        agents: snapshot.statuses,
        devices,
        active_device,
        animation_choices: choices,
        animation_states: states,
        animation_library: library,
        history_points: points,
        history_timeframe: timeframe_seconds,
        history_sampled: sampled,
        lid_durations: match payload {
            ServerPayload::Settings { settings, .. } => [
                settings
                    .get("lid_open_animation")
                    .and_then(|value| value.get("duration_seconds"))
                    .and_then(serde_json::Value::as_f64)
                    .unwrap_or(1.0),
                settings
                    .get("lid_closed_animation")
                    .and_then(|value| value.get("duration_seconds"))
                    .and_then(serde_json::Value::as_f64)
                    .unwrap_or(1.3),
            ],
            _ => [1.0, 1.3],
        },
    })
}

/// Poll and execute commands off the renderer thread. Drop both returned channels
/// when the host closes; the worker exits after its bounded pending operation.
pub fn start_worker(endpoint: String) -> (Sender<WorkerCommand>, Receiver<Update>) {
    let (commands, pending) = mpsc::channel();
    let (updates, received) = mpsc::channel();
    std::thread::spawn(move || {
        let mut setup = std::env::current_exe().ok().and_then(|executable| {
            sidepulse_installer::management::context_from_executable(&executable, &endpoint)
                .ok()
                .flatten()
        });
        if let Some(status) = &setup {
            let _ = updates.send(Update::Setup(status.clone()));
        }
        loop {
            let state = fetch_state(&endpoint);
            if let Ok(state) = &state {
                setup = Some(state.setup.clone());
            }
            if updates.send(Update::State(state.map(Box::new))).is_err() {
                break;
            }
            match pending.recv_timeout(Duration::from_secs(1)) {
                Ok(command) => {
                    let kind = match command {
                        WorkerCommand::Request(kind) => kind,
                        command => {
                            let result = (|| -> Result<String, String> {
                                if let WorkerCommand::OpenFile { path } = command {
                                    sidepulse_platform::open_file(std::path::Path::new(&path))
                                        .map_err(|error| error.to_string())?;
                                    return Ok("Opened report".into());
                                }
                                let status = setup.as_ref().ok_or("Setup status is unavailable. Open the staged settings application.")?;
                                match command {
                                    WorkerCommand::Startup {
                                        job,
                                        operation,
                                        dry_run,
                                    } => sidepulse_installer::management::manage_startup(
                                        status, &endpoint, job, operation, dry_run,
                                    )
                                    .map_err(|error| error.to_string()),
                                    #[cfg(target_os = "macos")]
                                    WorkerCommand::SleepHelper { install } => {
                                        let user = std::env::var("USER")
                                            .or_else(|_| std::env::var("USERNAME"))
                                            .map_err(|_| "Setup user is unavailable.")?;
                                        let target =
                                            sidepulse_installer::management::sleep_helper_target(
                                                status, &endpoint, install, &user,
                                            )
                                            .map_err(|error| error.to_string())?;
                                        sidepulse_platform::open_session(&target, "terminal", "")
                                            .map_err(|error| error.to_string())?;
                                        Ok("Administrator setup opened in Terminal".into())
                                    }
                                    #[cfg(target_os = "macos")]
                                    WorkerCommand::SleepHelperStatus => {
                                        let user = std::env::var("USER")
                                            .or_else(|_| std::env::var("USERNAME"))
                                            .map_err(|_| "Setup user is unavailable.")?;
                                        sidepulse_installer::management::sleep_helper_status(&user)
                                            .map(|installed| {
                                                if installed {
                                                    "Closed-lid setup is installed"
                                                } else {
                                                    "Closed-lid setup is not installed"
                                                }
                                                .into()
                                            })
                                            .map_err(|error| error.to_string())
                                    }
                                    _ => unreachable!(),
                                }
                            })();
                            if updates.send(Update::Managed(result)).is_err() {
                                break;
                            }
                            continue;
                        }
                    };
                    if matches!(kind, RequestKind::ConfigureHooks { .. }) {
                        let result = request(&endpoint, kind).and_then(|payload| {
                            let ServerPayload::HooksConfigured {
                                provider,
                                install,
                                changed,
                                trust_review_required,
                                dry_run,
                                ..
                            } = payload
                            else {
                                return Err("The service did not confirm the hook change.".into());
                            };
                            Ok(format!(
                                "{provider}: {}{}",
                                if dry_run {
                                    if changed {
                                        "Change available"
                                    } else {
                                        "Already configured"
                                    }
                                } else if install {
                                    "Hooks installed"
                                } else {
                                    "Hooks removed"
                                },
                                if trust_review_required && !dry_run {
                                    ". Review and trust these hooks in Codex using /hooks."
                                } else {
                                    ""
                                }
                            ))
                        });
                        if updates.send(Update::Managed(result)).is_err() {
                            break;
                        }
                        continue;
                    }
                    if matches!(kind, RequestKind::ExportDiagnostics { .. }) {
                        let result = request(&endpoint, kind).and_then(|payload| {
                            let ServerPayload::DiagnosticsExported { path, events } = payload
                            else {
                                return Err("The service did not return an export.".into());
                            };
                            Ok((path, events))
                        });
                        if updates.send(Update::Exported(result)).is_err() {
                            break;
                        }
                        continue;
                    }
                    if matches!(kind, RequestKind::SessionTargets { .. }) {
                        let result = request(&endpoint, kind).and_then(|payload| {
                            let ServerPayload::SessionTargets {
                                options,
                                selected,
                                terminal,
                                custom_terminal_path,
                            } = payload
                            else {
                                return Err("The service did not return session actions.".into());
                            };
                            let option = options
                                .iter()
                                .find(|option| Some(option.action) == selected)
                                .ok_or("No opener is available for this session.")?;
                            sidepulse_platform::open_session(
                                &option.target,
                                &terminal,
                                &custom_terminal_path,
                            )
                            .map_err(|error| error.to_string())
                        });
                        if updates.send(Update::Opened(result)).is_err() {
                            break;
                        }
                        continue;
                    }
                    if matches!(kind, RequestKind::ExportAnimationProfile { .. }) {
                        let result = request(&endpoint, kind).and_then(|payload| {
                            let ServerPayload::AnimationProfileDocument { document } = payload
                            else {
                                return Err("The service did not return a profile.".into());
                            };
                            serde_json::to_string_pretty(&document)
                                .map_err(|error| error.to_string())
                        });
                        if updates.send(Update::ProfileExport(result)).is_err() {
                            break;
                        }
                        continue;
                    }
                    let draft = match kind {
                        RequestKind::SetPhoneDisplay { .. }
                        | RequestKind::RegisterPhone { .. }
                        | RequestKind::RemovePhone { .. }
                        | RequestKind::BeginPhonePairing { .. }
                        | RequestKind::CancelPhonePairing
                        | RequestKind::ReloadPhoneLinks => DraftKind::Phone,
                        RequestKind::SetBatterySettings { .. } => DraftKind::Battery,
                        RequestKind::SetAgentListSettings { .. } => DraftKind::Monitoring,
                        RequestKind::SetSleepSettings { .. } => DraftKind::Sleep,
                        RequestKind::SetAgentAnimation { .. } => DraftKind::Animation,
                        RequestKind::SetSessionTerminal { .. } => DraftKind::Terminal,
                        RequestKind::EditAnimationLibrary { .. }
                        | RequestKind::SetAnimationState { .. } => DraftKind::Library,
                        RequestKind::SetLidAnimationTiming { .. } => DraftKind::LidTiming,
                        RequestKind::SetRelaySettings { ref patch }
                            if patch.server.is_none()
                                && patch.machine_name.is_none()
                                && patch.outbound_code.is_some() =>
                        {
                            DraftKind::RelayControl
                        }
                        RequestKind::SetRelaySettings { ref patch }
                            if patch.server.is_some()
                                || patch.machine_name.is_some()
                                || patch.outbound_code.is_some() =>
                        {
                            DraftKind::Relay
                        }
                        RequestKind::SetRelaySettings { .. } | RequestKind::ReloadRelaySettings => {
                            DraftKind::RelayControl
                        }
                        _ => DraftKind::None,
                    };
                    let result = request(&endpoint, kind).and_then(|payload| match payload {
                        ServerPayload::Settings { .. }
                        | ServerPayload::Devices { .. }
                        | ServerPayload::RelaySettings { .. }
                        | ServerPayload::PhoneLinks { .. }
                        | ServerPayload::PowerControl { .. } => Ok(()),
                        _ => Err("The service did not confirm the change.".into()),
                    });
                    if updates.send(Update::Saved { result, draft }).is_err() {
                        break;
                    }
                }
                Err(mpsc::RecvTimeoutError::Timeout) => {}
                Err(mpsc::RecvTimeoutError::Disconnected) => break,
            }
        }
    });
    (commands, received)
}

#[cfg(test)]
mod tests;
