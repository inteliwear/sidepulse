//! The webview can only request Settings actions. Policy and OS operations stay
//! in the shared Rust client; no shell or filesystem API is exposed to HTML.
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sidepulse_core::{ClientRequest, PROTOCOL_VERSION, RequestKind};
use sidepulse_ui_client::{Update, WorkerCommand};
use std::sync::mpsc::{Receiver, Sender};

#[derive(Deserialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub enum Action {
    Request {
        request: RequestKind,
    },
    Startup {
        job: String,
        operation: String,
        dry_run: bool,
    },
    SleepHelper {
        operation: String,
    },
    OpenExport {},
}

#[derive(Serialize)]
pub struct Poll {
    pub revision: u64,
    pub connected: bool,
    pub busy: bool,
    pub platform: &'static str,
    pub state: Option<Value>,
    pub setup: Option<sidepulse_core::HookSetupStatus>,
    pub events: Vec<Value>,
    pub previews: std::collections::BTreeMap<String, crate::previews::Frame>,
}

pub struct Bridge {
    previews: crate::previews::Previews,
    commands: Sender<WorkerCommand>,
    updates: Receiver<Update>,
    state: Option<Value>,
    setup: Option<sidepulse_core::HookSetupStatus>,
    connected: bool,
    busy: bool,
    revision: u64,
    last_export: Option<String>,
    endpoint: String,
    virtual_child: Option<std::process::Child>,
    virtual_launch_attempted: bool,
}

impl Bridge {
    pub fn new(endpoint: String) -> Self {
        let (commands, updates) = sidepulse_ui_client::start_worker(endpoint.clone());
        Self::with_channels(endpoint, commands, updates)
    }

    fn with_channels(
        endpoint: String,
        commands: Sender<WorkerCommand>,
        updates: Receiver<Update>,
    ) -> Self {
        Self {
            previews: crate::previews::Previews::default(),
            commands,
            updates,
            state: None,
            setup: None,
            connected: false,
            busy: false,
            revision: 0,
            last_export: None,
            endpoint,
            virtual_child: None,
            virtual_launch_attempted: false,
        }
    }

    pub fn action(&mut self, action: Action) -> Result<(), String> {
        if self.busy {
            return Err("Wait for the current change to finish.".into());
        }
        let command = match action {
            Action::Request { request } => {
                validate_settings_request(&request)?;
                if !self.connected {
                    return Err("The service is unavailable. Your changes have been kept.".into());
                }
                WorkerCommand::Request(request)
            }
            Action::Startup {
                job,
                operation,
                dry_run,
            } => {
                let job = sidepulse_installer::startup::Job::parse(&job)
                    .map_err(|error| error.to_string())?;
                let operation = sidepulse_installer::startup::Operation::parse(&operation)
                    .map_err(|error| error.to_string())?;
                WorkerCommand::Startup {
                    job,
                    operation,
                    dry_run,
                }
            }
            Action::SleepHelper { operation } => sleep_helper_command(&operation)?,
            Action::OpenExport {} => WorkerCommand::OpenFile {
                path: self.last_export.clone().ok_or("Export a report first.")?,
            },
        };
        self.commands
            .send(command)
            .map_err(|_| "The Settings client has closed.".to_string())?;
        self.busy = true;
        Ok(())
    }

    pub fn poll_with_previews(
        &mut self,
        after_revision: u64,
        editor: Option<&str>,
    ) -> Result<Poll, String> {
        let mut poll = self.poll(after_revision)?;
        if let Some(state) = &self.state {
            poll.previews = self.previews.frames(state, editor);
        }
        Ok(poll)
    }

    pub fn poll(&mut self, after_revision: u64) -> Result<Poll, String> {
        let mut events = Vec::new();
        while let Ok(update) = self.updates.try_recv() {
            match update {
                Update::Setup(status) => self.setup = Some(status),
                Update::State(Ok(state)) => {
                    self.connected = true;
                    self.setup = Some(state.setup.clone());
                    let virtual_enabled = state.settings.virtual_display_enabled;
                    let mut value =
                        serde_json::to_value(&state).map_err(|error| error.to_string())?;
                    // Opener availability and labels are computed by core, not by JS.
                    let actions = state.agents.iter().map(|agent| {
                        let options = sidepulse_core::session_open_options(agent, "").into_iter()
                            .map(|option| json!({"action": option.action, "label": option.label})).collect::<Vec<_>>();
                        (agent.agent_id.clone(), json!(options))
                    }).collect::<serde_json::Map<_, _>>();
                    value["session_actions"] = Value::Object(actions);
                    value["device_names"] = json!(
                        state
                            .devices
                            .iter()
                            .map(sidepulse_ui_model::device_display_name)
                            .collect::<Vec<_>>()
                    );
                    self.state = Some(value);
                    self.revision += 1;
                    if !virtual_enabled {
                        self.virtual_launch_attempted = false;
                    } else if !self.virtual_launch_attempted
                        && let Err(error) = self.open_virtual_display()
                    {
                        events.push(json!({"type": "message", "error": true, "message": format!("Could not open virtual display: {error}")}));
                    }
                }
                Update::State(Err(_)) => self.connected = false,
                Update::Managed(result) => {
                    self.busy = false;
                    events.push(message(result));
                }
                Update::Opened(result) => {
                    self.busy = false;
                    events.push(message(result.map(|()| "Opening session…".into())));
                }
                Update::Exported(result) => {
                    self.busy = false;
                    events.push(match result {
                        Ok((path, count)) => {
                            self.last_export = Some(path.clone());
                            json!({"type":"export", "path":path, "count":count})
                        }
                        Err(error) => message(Err(error)),
                    });
                }
                Update::ProfileExport(result) => {
                    self.busy = false;
                    events.push(match result {
                        Ok(document) => json!({"type":"profile_export", "document":document}),
                        Err(error) => message(Err(error)),
                    });
                }
                Update::Saved { result, draft } => {
                    self.busy = false;
                    events.push(json!({"type":"saved", "draft":draft, "revision":self.revision, "error":result.err()}));
                }
            }
        }
        Ok(Poll {
            revision: self.revision,
            connected: self.connected,
            busy: self.busy,
            platform: std::env::consts::OS,
            state: (after_revision != self.revision)
                .then(|| self.state.clone())
                .flatten(),
            setup: self.setup.clone(),
            events,
            previews: Default::default(),
        })
    }

    fn open_virtual_display(&mut self) -> std::io::Result<()> {
        self.virtual_launch_attempted = true;
        if self
            .virtual_child
            .as_mut()
            .is_some_and(|child| child.try_wait().is_ok_and(|status| status.is_none()))
        {
            return Ok(());
        }
        let current = std::env::current_exe()?;
        let executable = if cfg!(target_os = "macos") {
            current
                .ancestors()
                .take(6)
                .map(|root| {
                    root.join(
                        "applications/SidePulse Virtual.app/Contents/MacOS/sidepulse-next-virtual",
                    )
                })
                .find(|path| path.is_file())
        } else {
            None
        }
        .unwrap_or_else(|| {
            current.with_file_name(if cfg!(windows) {
                "sidepulse-next-virtual.exe"
            } else {
                "sidepulse-next-virtual"
            })
        });
        self.virtual_child = Some(
            std::process::Command::new(executable)
                .arg(&self.endpoint)
                .spawn()?,
        );
        Ok(())
    }
}

fn message(result: Result<String, String>) -> Value {
    match result {
        Ok(message) => {
            json!({"type":"message", "error":false, "message":message, "completed":true})
        }
        Err(error) => json!({"type":"message", "error":true, "message":error, "completed":true}),
    }
}

fn sleep_helper_command(operation: &str) -> Result<WorkerCommand, String> {
    #[cfg(target_os = "macos")]
    return match operation {
        "install" => Ok(WorkerCommand::SleepHelper { install: true }),
        "uninstall" => Ok(WorkerCommand::SleepHelper { install: false }),
        "status" => Ok(WorkerCommand::SleepHelperStatus),
        _ => Err("Unknown sleep setup action.".into()),
    };
    #[cfg(not(target_os = "macos"))]
    {
        let _ = operation;
        Err("Closed-lid setup is available on macOS.".into())
    }
}

fn validate_settings_request(kind: &RequestKind) -> Result<(), String> {
    if !matches!(
        kind,
        RequestKind::ConfigureHooks { .. }
            | RequestKind::ExportDiagnostics { .. }
            | RequestKind::RetryPowerControl
            | RequestKind::PreviewAnimation { .. }
            | RequestKind::EditAnimationLibrary { .. }
            | RequestKind::ExportAnimationProfile { .. }
            | RequestKind::SetLidAnimationTiming { .. }
            | RequestKind::SetAnimationState { .. }
            | RequestKind::SetPhoneDisplay { .. }
            | RequestKind::RegisterPhone { .. }
            | RequestKind::RemovePhone { .. }
            | RequestKind::BeginPhonePairing { .. }
            | RequestKind::CancelPhonePairing
            | RequestKind::ReloadPhoneLinks
            | RequestKind::SetRelaySettings { .. }
            | RequestKind::ReloadRelaySettings
            | RequestKind::SessionTargets { .. }
            | RequestKind::SetSessionOpenPreference { .. }
            | RequestKind::SetSessionTerminal { .. }
            | RequestKind::SetHistoryTimeframe { .. }
            | RequestKind::SetVirtualDisplay { .. }
            | RequestKind::SetAgentAnimation { .. }
            | RequestKind::SelectDevice { .. }
            | RequestKind::SetBrightness { .. }
            | RequestKind::SetDisplayMode { .. }
            | RequestKind::SetTrayVisibility { .. }
            | RequestKind::SetBatterySettings { .. }
            | RequestKind::SetAgentListSettings { .. }
            | RequestKind::SetSleepSettings { .. }
            | RequestKind::SetSleepPolicy { .. }
            | RequestKind::SetTranscriptMonitoring { .. }
    ) {
        return Err("This action is not available from Settings.".into());
    }
    ClientRequest {
        version: PROTOCOL_VERSION,
        request_id: 1,
        kind: kind.clone(),
    }
    .validate()
    .map_err(String::from)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::mpsc;

    #[test]
    fn browser_actions_deserialize_and_validate_at_the_rust_boundary() {
        let actions: Vec<Action> =
            serde_json::from_str(include_str!("../tests/actions.json")).unwrap();
        assert!(actions.len() >= 30);
        for action in actions {
            match action {
                Action::Request { request } => validate_settings_request(&request).unwrap(),
                Action::Startup { job, operation, .. } => {
                    sidepulse_installer::startup::Job::parse(&job).unwrap();
                    sidepulse_installer::startup::Operation::parse(&operation).unwrap();
                }
                Action::OpenExport {} => {}
                Action::SleepHelper { operation } => assert!(matches!(
                    operation.as_str(),
                    "install" | "uninstall" | "status"
                )),
            }
        }
    }

    #[test]
    fn browser_fixture_matches_the_shared_rust_state() {
        let state: sidepulse_ui_client::ServiceState =
            serde_json::from_str(include_str!("../tests/state.json")).unwrap();
        assert_eq!(state.activity.active_count, 1);
        assert_eq!(state.history_points.len(), 8);
        assert_eq!(state.setup.providers.len(), 5);
        assert!(state.phone_pairing.is_some());
    }

    #[test]
    fn webview_boundary_rejects_service_ingestion_shutdown_and_invalid_preferences() {
        for request in [
            RequestKind::Shutdown,
            RequestKind::Subscribe,
            RequestKind::IngestHook {
                provider: "codex".into(),
                line: json!({}),
            },
            RequestKind::SetDisplayMode { mode: "bad".into() },
            RequestKind::SetHistoryTimeframe { seconds: 42 },
        ] {
            assert!(validate_settings_request(&request).is_err());
        }
        assert!(
            validate_settings_request(&RequestKind::SetTrayVisibility { visible: false }).is_ok()
        );
        assert!(
            serde_json::from_value::<Action>(json!({"type":"open_file", "path":"/tmp/arbitrary"}))
                .is_err()
        );
        assert!(
            serde_json::from_value::<Action>(
                json!({"type":"open_export", "path":"/tmp/arbitrary"})
            )
            .is_err()
        );
    }

    #[test]
    fn offline_startup_is_available_and_exports_only_open_host_returned_paths() {
        let (commands, received) = mpsc::channel();
        let (updates, receiver) = mpsc::channel();
        let mut bridge = Bridge::with_channels("unused".into(), commands, receiver);
        assert!(
            bridge
                .action(Action::Request {
                    request: RequestKind::SetTrayVisibility { visible: false }
                })
                .is_err()
        );
        assert!(bridge.action(Action::OpenExport {}).is_err());
        bridge
            .action(Action::Startup {
                job: "service".into(),
                operation: "status".into(),
                dry_run: true,
            })
            .unwrap();
        assert!(matches!(
            received.recv().unwrap(),
            WorkerCommand::Startup { dry_run: true, .. }
        ));
        assert!(bridge.action(Action::OpenExport {}).is_err());
        updates.send(Update::Managed(Ok("Stopped".into()))).unwrap();
        assert!(!bridge.poll(0).unwrap().busy);
        updates
            .send(Update::Exported(Ok((
                "/tmp/trusted-report.html".into(),
                42,
            ))))
            .unwrap();
        let poll = bridge.poll(0).unwrap();
        assert_eq!(poll.events[0]["count"], 42);
        bridge.action(Action::OpenExport {}).unwrap();
        assert!(
            matches!(received.recv().unwrap(), WorkerCommand::OpenFile {path} if path=="/tmp/trusted-report.html")
        );
    }

    #[test]
    fn errors_and_draft_acknowledgements_are_delivered_once() {
        let (commands, _) = mpsc::channel();
        let (updates, receiver) = mpsc::channel();
        let mut bridge = Bridge::with_channels("unused".into(), commands, receiver);
        updates
            .send(Update::Saved {
                result: Err("Settings changed in another window".into()),
                draft: sidepulse_ui_client::DraftKind::Battery,
            })
            .unwrap();
        let poll = bridge.poll(0).unwrap();
        assert_eq!(poll.events[0]["draft"], "battery");
        assert_eq!(
            poll.events[0]["error"],
            "Settings changed in another window"
        );
        assert!(bridge.poll(0).unwrap().events.is_empty());
    }
}
