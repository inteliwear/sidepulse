//! Rust presentation client. Settings and device output belong to the service.

use std::sync::mpsc::{self, Receiver, Sender};
use std::time::Duration;

use eframe::egui;
use sidepulse_core::{
    AgentAnimationState, AgentListSettingsPatch, AgentMode, AnimationChoice, BatterySettingsPatch,
    ChargerBaseline, ClientRequest, DeviceInfo, MonitorSnapshot, PROTOCOL_VERSION, RequestKind,
    ServerMessage, ServerPayload,
};
use sidepulse_ui_model::{DISPLAY_CHOICES, SettingsView, TrayState, device_display_name};

struct ServiceState {
    settings: SettingsView,
    activity: TrayState,
    agents: Vec<sidepulse_core::AgentStatus>,
    devices: Vec<DeviceInfo>,
    active_device: Option<String>,
    animation_choices: Vec<AnimationChoice>,
    animation_states: Vec<AgentAnimationState>,
    animation_library: sidepulse_core::AnimationLibrary,
    history_points: Vec<sidepulse_core::HistoryPoint>,
    history_timeframe: u32,
    history_sampled: bool,
    lid_durations: [f64; 2],
    relay: sidepulse_core::RelaySettings,
    phones_configured: bool,
    phone_output_enabled: bool,
    phones: Vec<sidepulse_core::PhoneLinkSummary>,
    phone_pairing: Option<sidepulse_core::PhonePairingView>,
}

enum Update {
    State(Result<Box<ServiceState>, String>),
    Opened(Result<(), String>),
    ProfileExport(Result<String, String>),
    Saved {
        result: Result<(), String>,
        draft: DraftKind,
    },
}

#[derive(Clone, Copy)]
enum DraftKind {
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
    let response: ServerMessage =
        sidepulse_ipc::request(endpoint, &request, Duration::from_secs(2))
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

fn fetch_state(endpoint: &str) -> Result<ServiceState, String> {
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
    Ok(ServiceState {
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

fn start_worker(endpoint: String) -> (Sender<RequestKind>, Receiver<Update>) {
    let (commands, pending) = mpsc::channel();
    let (updates, received) = mpsc::channel();
    std::thread::spawn(move || {
        loop {
            if updates
                .send(Update::State(fetch_state(&endpoint).map(Box::new)))
                .is_err()
            {
                break;
            }
            match pending.recv_timeout(Duration::from_secs(1)) {
                Ok(kind) => {
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
                        | ServerPayload::PhoneLinks { .. } => Ok(()),
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

#[derive(Clone, Copy, PartialEq, Eq)]
enum Page {
    Activity,
    Devices,
    Battery,
    Monitoring,
    Sleep,
    Animations,
    History,
    Sessions,
    Relay,
    Phones,
}

struct SettingsApp {
    commands: Sender<RequestKind>,
    updates: Receiver<Update>,
    state: Option<ServiceState>,
    connected: bool,
    message: Option<(String, bool)>,
    page: Page,
    battery_dirty: bool,
    battery_saving: bool,
    baseline_auto: bool,
    baseline_watts: f64,
    preview_enabled: bool,
    preview_seconds: f64,
    brightness: u8,
    brightness_dragging: bool,
    monitoring_dirty: bool,
    monitoring_saving: bool,
    idle_minutes: f64,
    retention_hours: f64,
    sleep_dirty: bool,
    sleep_saving: bool,
    sleep_battery_percent: f64,
    animation_mode: AgentMode,
    animation_style: String,
    animation_program: String,
    animation_dirty: bool,
    animation_saving: bool,
    endpoint: String,
    virtual_child: Option<std::process::Child>,
    virtual_launch_attempted: bool,
    virtual_brightness: u8,
    virtual_brightness_dragging: bool,
    terminal_dirty: bool,
    terminal_saving: bool,
    session_terminal: String,
    custom_terminal_path: String,
    profile_name: String,
    profile_json: String,
    asset_id: Option<String>,
    asset_name: String,
    asset_program: String,
    library_saving: bool,
    lid_durations: [f64; 2],
    lid_duration_dirty: bool,
    relay_dirty: bool,
    relay_saving: bool,
    relay_server: String,
    relay_name: String,
    relay_outbound: String,
    phone_saving: bool,
    phone_token: String,
    phone_name: String,
}

impl SettingsApp {
    fn new(endpoint: String) -> Self {
        let (commands, updates) = start_worker(endpoint.clone());
        Self {
            commands,
            updates,
            state: None,
            connected: false,
            message: None,
            page: Page::Activity,
            battery_dirty: false,
            battery_saving: false,
            baseline_auto: true,
            baseline_watts: 100.0,
            preview_enabled: true,
            preview_seconds: 7.0,
            brightness: 255,
            brightness_dragging: false,
            monitoring_dirty: false,
            monitoring_saving: false,
            idle_minutes: 60.0,
            retention_hours: 48.0,
            sleep_dirty: false,
            sleep_saving: false,
            sleep_battery_percent: 20.0,
            animation_mode: AgentMode::Working,
            animation_style: "cyan-roll".into(),
            animation_program: String::new(),
            animation_dirty: false,
            animation_saving: false,
            endpoint,
            virtual_child: None,
            virtual_launch_attempted: false,
            virtual_brightness: 255,
            virtual_brightness_dragging: false,
            terminal_dirty: false,
            terminal_saving: false,
            session_terminal: "terminal".into(),
            custom_terminal_path: String::new(),
            profile_name: String::new(),
            profile_json: String::new(),
            asset_id: None,
            asset_name: String::new(),
            asset_program: "#00E5FF".into(),
            library_saving: false,
            lid_durations: [1.0, 1.3],
            lid_duration_dirty: false,
            relay_dirty: false,
            relay_saving: false,
            relay_server: String::new(),
            relay_name: String::new(),
            relay_outbound: String::new(),
            phone_saving: false,
            phone_token: String::new(),
            phone_name: "iPhone".into(),
        }
    }

    fn open_virtual_display(&mut self) -> std::io::Result<()> {
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
        self.virtual_launch_attempted = true;
        self.virtual_child = Some(
            std::process::Command::new(executable)
                .arg(&self.endpoint)
                .spawn()?,
        );
        Ok(())
    }

    fn send(&mut self, kind: RequestKind) {
        if self.commands.send(kind).is_ok() {
            self.message = Some(("Saving…".into(), false));
        } else {
            self.message = Some(("Could not contact the service.".into(), true));
        }
    }

    fn poll(&mut self) {
        while let Ok(update) = self.updates.try_recv() {
            match update {
                Update::State(Ok(state)) => {
                    self.connected = true;
                    if !self.terminal_dirty {
                        self.session_terminal = state.settings.session_terminal.clone();
                        self.custom_terminal_path = state.settings.custom_terminal_path.clone();
                    }
                    if !self.battery_dirty {
                        self.baseline_auto = state.settings.full_charge_watts.is_none();
                        self.baseline_watts = state.settings.full_charge_watts.unwrap_or(100.0);
                        self.preview_enabled = state.settings.controls.battery_power_preview;
                        self.preview_seconds = state.settings.power_change_preview_seconds;
                    }
                    if !self.brightness_dragging {
                        self.brightness = state.settings.controls.brightness.unwrap_or(255);
                    }
                    if !self.monitoring_dirty {
                        self.idle_minutes = state.settings.idle_timeout_seconds / 60.0;
                        self.retention_hours =
                            state.settings.controls.recent_session_retention_seconds / 3600.0;
                    }
                    if !self.sleep_dirty {
                        self.sleep_battery_percent = state.settings.sleep_min_battery_percent;
                    }
                    if !self.relay_dirty {
                        self.relay_server = state.relay.server.clone();
                        self.relay_name = state.relay.machine_name.clone();
                        self.relay_outbound = state.relay.outbound_code.clone();
                    }
                    if !self.lid_duration_dirty {
                        self.lid_durations = state.lid_durations;
                    }
                    if !self.animation_dirty
                        && let Some(animation) = state
                            .animation_states
                            .iter()
                            .find(|animation| animation.mode == self.animation_mode)
                    {
                        self.animation_style = animation.style.clone();
                        self.animation_program = animation.program.clone();
                    }
                    if !self.virtual_brightness_dragging {
                        self.virtual_brightness = state.settings.virtual_display_brightness;
                    }
                    let virtual_enabled = state.settings.virtual_display_enabled;
                    if !virtual_enabled {
                        self.virtual_launch_attempted = false;
                    }
                    self.state = Some(*state);
                    if virtual_enabled
                        && !self.virtual_launch_attempted
                        && let Err(error) = self.open_virtual_display()
                    {
                        self.message =
                            Some((format!("Could not open virtual display: {error}"), true));
                    }
                }
                Update::State(Err(_)) => self.connected = false,
                Update::Opened(result) => {
                    self.message = Some(match result {
                        Ok(()) => ("Opening session…".into(), false),
                        Err(error) => (format!("Could not open session: {error}"), true),
                    });
                }
                Update::ProfileExport(result) => {
                    self.message = Some(match result {
                        Ok(document) => {
                            self.profile_json = document;
                            ("Profile ready to copy or save".into(), false)
                        }
                        Err(error) => (format!("Could not export: {error}"), true),
                    });
                }
                Update::Saved { result, draft } => {
                    let success = result.is_ok();
                    match draft {
                        DraftKind::Battery => {
                            self.battery_saving = false;
                            if success {
                                self.battery_dirty = false;
                            }
                        }
                        DraftKind::Monitoring => {
                            self.monitoring_saving = false;
                            if success {
                                self.monitoring_dirty = false;
                            }
                        }
                        DraftKind::Sleep => {
                            self.sleep_saving = false;
                            if success {
                                self.sleep_dirty = false;
                            }
                        }
                        DraftKind::Terminal => {
                            self.terminal_saving = false;
                            if success {
                                self.terminal_dirty = false;
                            }
                        }
                        DraftKind::Relay => {
                            self.relay_saving = false;
                            if success {
                                self.relay_dirty = false;
                            }
                        }
                        DraftKind::RelayControl => {
                            self.relay_saving = false;
                        }
                        DraftKind::Phone => {
                            self.phone_saving = false;
                            if success {
                                self.phone_token.clear();
                            }
                        }
                        DraftKind::None => {}
                        DraftKind::Library => {
                            self.library_saving = false;
                        }
                        DraftKind::LidTiming => {
                            self.library_saving = false;
                            if success {
                                self.lid_duration_dirty = false;
                            }
                        }
                        DraftKind::Animation => {
                            self.animation_saving = false;
                            if success {
                                self.animation_dirty = false;
                            }
                        }
                    }
                    self.message = Some(match result {
                        Ok(()) => ("Saved".into(), false),
                        Err(error) => (format!("Could not save: {error}"), true),
                    });
                }
            }
        }
    }

    fn activity(&mut self, ui: &mut egui::Ui) {
        let Some(state) = &self.state else {
            ui.label("Waiting for activity…");
            return;
        };
        let activity = state.activity.clone();
        let agents = state.agents.clone();
        ui.heading(&activity.tooltip);
        ui.add_space(12.0);
        if activity.rows.is_empty() {
            ui.label("No active agents.");
        }
        for row in &activity.rows {
            ui.group(|ui| {
                ui.strong(&row.title);
                ui.label(&row.subtitle);
                if let Some(agent) = agents.iter().find(|agent| agent.agent_id == row.id) {
                    self.session_buttons(ui, agent);
                }
            });
        }
        if !activity.stale_rows.is_empty() {
            ui.add_space(16.0);
            ui.collapsing("Recent sessions", |ui| {
                for row in &activity.stale_rows {
                    ui.label(&row.title);
                    ui.weak(&row.subtitle);
                    if let Some(agent) = agents.iter().find(|agent| agent.agent_id == row.id) {
                        self.session_buttons(ui, agent);
                    }
                }
            });
        }
    }

    fn session_buttons(&mut self, ui: &mut egui::Ui, agent: &sidepulse_core::AgentStatus) {
        let options = sidepulse_core::session_open_options(agent, "");
        if options.is_empty() {
            return;
        }
        ui.horizontal(|ui| {
            if ui.button("Open session").clicked() {
                self.send(RequestKind::SessionTargets {
                    agent_id: agent.agent_id.clone(),
                    action: None,
                });
                self.message = Some(("Opening session…".into(), false));
            }
            ui.menu_button("Open with…", |ui| {
                for option in options {
                    if ui.button(option.label).clicked() {
                        self.send(RequestKind::SessionTargets {
                            agent_id: agent.agent_id.clone(),
                            action: Some(option.action),
                        });
                        self.message = Some(("Opening session…".into(), false));
                        ui.close();
                    }
                }
            });
        });
    }

    fn sessions(&mut self, ui: &mut egui::Ui) {
        use sidepulse_core::SessionAction;
        ui.heading("Session opening");
        ui.label("Choose where agent sessions open when you select them.");
        let Some(state) = &self.state else {
            return;
        };
        let preferences = state.settings.session_open_preferences.clone();
        ui.add_space(16.0);
        for (provider, mut action) in preferences {
            ui.horizontal(|ui| {
                ui.label(match provider.as_str() {
                    "codex" => "Codex",
                    "claude" => "Claude",
                    _ => "Grok",
                });
                let choices = match provider.as_str() {
                    "claude" => vec![
                        (SessionAction::Vscode, "VS Code"),
                        (SessionAction::App, "Claude App"),
                        (SessionAction::Terminal, "Terminal"),
                    ],
                    "codex" => vec![
                        (SessionAction::App, "Codex App"),
                        (SessionAction::Terminal, "Terminal"),
                    ],
                    _ => vec![(SessionAction::Terminal, "Terminal")],
                };
                let selected = choices
                    .iter()
                    .find(|choice| choice.0 == action)
                    .map_or("Terminal", |choice| choice.1);
                egui::ComboBox::from_id_salt(format!("session-opener-{provider}"))
                    .selected_text(selected)
                    .show_ui(ui, |ui| {
                        for (choice, label) in choices {
                            if ui.selectable_value(&mut action, choice, label).changed() {
                                self.send(RequestKind::SetSessionOpenPreference {
                                    provider: provider.clone(),
                                    origin: None,
                                    action,
                                });
                            }
                        }
                    });
            });
        }
        ui.add_space(20.0);
        ui.strong("Terminal app");
        ui.add_enabled_ui(!self.terminal_saving, |ui| {
            let choices = if cfg!(target_os = "macos") {
                vec![
                    ("terminal", "Terminal"),
                    ("iterm", "iTerm"),
                    ("ghostty", "Ghostty"),
                    ("warp", "Warp"),
                    ("kitty", "Kitty"),
                    ("wezterm", "WezTerm"),
                    ("alacritty", "Alacritty"),
                    ("custom", "Custom"),
                ]
            } else if cfg!(windows) {
                vec![("terminal", "Windows Terminal / PowerShell")]
            } else {
                vec![
                    ("terminal", "System terminal"),
                    ("ghostty", "Ghostty"),
                    ("kitty", "Kitty"),
                    ("wezterm", "WezTerm"),
                    ("alacritty", "Alacritty"),
                    ("custom", "Custom"),
                ]
            };
            let selected = choices
                .iter()
                .find(|choice| choice.0 == self.session_terminal)
                .map_or(self.session_terminal.as_str(), |choice| choice.1)
                .to_owned();
            egui::ComboBox::from_id_salt("session-terminal")
                .selected_text(selected)
                .show_ui(ui, |ui| {
                    for (id, label) in choices {
                        self.terminal_dirty |= ui
                            .selectable_value(&mut self.session_terminal, id.into(), label)
                            .changed();
                    }
                });
            if self.session_terminal == "custom" {
                ui.label(if cfg!(target_os = "macos") {
                    "Application path"
                } else {
                    "Terminal executable path"
                });
                self.terminal_dirty |= ui
                    .text_edit_singleline(&mut self.custom_terminal_path)
                    .changed();
            }
            if ui
                .add_enabled(
                    self.terminal_dirty,
                    egui::Button::new("Save terminal preference"),
                )
                .clicked()
            {
                self.send(RequestKind::SetSessionTerminal {
                    terminal: self.session_terminal.clone(),
                    custom_path: Some(self.custom_terminal_path.clone()),
                });
                self.terminal_saving = true;
            }
        });
        ui.add_space(12.0);
        ui.weak("Choose an installed terminal. Session opening is available from Activity and the tray.");
    }

    fn devices(&mut self, ui: &mut egui::Ui) {
        ui.heading("Devices");
        ui.label("Choose which device shows your agent or battery status.");
        let Some(state) = &self.state else {
            return;
        };
        let devices = state.devices.clone();
        let active = state.active_device.clone();
        let controls = state.settings.controls.clone();
        let mut virtual_enabled = state.settings.virtual_display_enabled;
        let virtual_display = state.settings.virtual_display_mode.clone();
        ui.add_space(12.0);
        if devices.is_empty() {
            ui.label("No SidePulse devices connected.");
        }
        for device in devices {
            let selected = active.as_deref() == Some(device.target.as_str());
            if ui
                .selectable_label(selected, device_display_name(&device))
                .clicked()
                && !selected
            {
                self.send(RequestKind::SelectDevice { root: device.root });
            }
        }
        ui.add_space(16.0);
        ui.add_enabled_ui(controls.brightness.is_some(), |ui| {
            ui.strong("Brightness");
            let response =
                ui.add(egui::Slider::new(&mut self.brightness, 0..=255).show_value(false));
            self.brightness_dragging = response.dragged();
            ui.label(format!(
                "{}%",
                (u16::from(self.brightness) * 100 + 127) / 255
            ));
            if response.drag_stopped() || (response.changed() && !response.dragged()) {
                self.send(RequestKind::SetBrightness {
                    brightness: self.brightness,
                });
            }
        });
        ui.add_space(12.0);
        ui.add_enabled_ui(controls.display_mode.is_some(), |ui| {
            ui.strong("Display");
            for choice in DISPLAY_CHOICES {
                if ui
                    .radio(
                        controls.display_mode.as_deref() == Some(choice.value),
                        choice.label,
                    )
                    .clicked()
                {
                    self.send(RequestKind::SetDisplayMode {
                        mode: choice.value.into(),
                    });
                }
            }
            ui.weak("Manual output keeps the program already on the device.");
        });
        ui.add_space(20.0);
        ui.separator();
        ui.strong("Virtual display");
        if ui
            .checkbox(&mut virtual_enabled, "Show status on screen")
            .changed()
        {
            self.send(RequestKind::SetVirtualDisplay {
                patch: sidepulse_core::VirtualDisplaySettingsPatch {
                    enabled: Some(virtual_enabled),
                    ..Default::default()
                },
            });
        }
        ui.weak(if cfg!(target_os = "macos") {
            "Appears beneath the notch, or at the top of a screen without a notch."
        } else {
            "Appears in a movable status window."
        });
        ui.add_enabled_ui(virtual_enabled, |ui| {
            ui.horizontal(|ui| {
                ui.label("Virtual brightness");
                let response = ui.add(egui::Slider::new(&mut self.virtual_brightness, 0..=255));
                self.virtual_brightness_dragging = response.dragged();
                if response.drag_stopped() || (response.changed() && !response.dragged()) {
                    self.send(RequestKind::SetVirtualDisplay {
                        patch: sidepulse_core::VirtualDisplaySettingsPatch {
                            brightness: Some(self.virtual_brightness),
                            ..Default::default()
                        },
                    });
                }
            });
            for choice in DISPLAY_CHOICES {
                if ui
                    .radio(virtual_display == choice.value, choice.label)
                    .clicked()
                {
                    self.send(RequestKind::SetVirtualDisplay {
                        patch: sidepulse_core::VirtualDisplaySettingsPatch {
                            display: Some(choice.value.into()),
                            ..Default::default()
                        },
                    });
                }
            }
            ui.weak("Manual output hides the virtual display.");
        });
    }

    fn battery(&mut self, ui: &mut egui::Ui) {
        ui.heading("Battery");
        ui.label("Set how battery status appears on your devices.");
        ui.add_space(16.0);
        self.battery_dirty |= ui
            .checkbox(
                &mut self.baseline_auto,
                "Detect full-speed charger wattage automatically",
            )
            .changed();
        ui.add_enabled_ui(!self.baseline_auto, |ui| {
            ui.horizontal(|ui| {
                ui.label("Full-speed charger");
                self.battery_dirty |= ui
                    .add(
                        egui::DragValue::new(&mut self.baseline_watts)
                            .range(1.0..=1000.0)
                            .suffix(" W"),
                    )
                    .changed();
            });
        });
        ui.add_space(16.0);
        self.battery_dirty |= ui
            .checkbox(
                &mut self.preview_enabled,
                "Show battery briefly when power is connected or disconnected",
            )
            .changed();
        ui.add_enabled_ui(self.preview_enabled, |ui| {
            ui.horizontal(|ui| {
                ui.label("Show for");
                self.battery_dirty |= ui
                    .add(
                        egui::DragValue::new(&mut self.preview_seconds)
                            .range(0.0..=3600.0)
                            .suffix(" seconds"),
                    )
                    .changed();
            });
        });
        ui.add_space(20.0);
        ui.horizontal(|ui| {
            if ui
                .add_enabled(
                    self.battery_dirty && !self.battery_saving,
                    egui::Button::new("Save battery settings"),
                )
                .clicked()
            {
                self.send(RequestKind::SetBatterySettings {
                    patch: BatterySettingsPatch {
                        full_charge_watts: Some(if self.baseline_auto {
                            ChargerBaseline::Auto
                        } else {
                            ChargerBaseline::Watts {
                                watts: self.baseline_watts,
                            }
                        }),
                        show_on_power_change: Some(self.preview_enabled),
                        power_change_preview_seconds: Some(self.preview_seconds),
                        ..Default::default()
                    },
                });
                self.battery_saving = true;
            }
            if ui
                .add_enabled(
                    self.battery_dirty && !self.battery_saving,
                    egui::Button::new("Reset changes"),
                )
                .clicked()
            {
                self.battery_dirty = false;
            }
        });
    }

    fn monitoring(&mut self, ui: &mut egui::Ui) {
        ui.heading("Monitoring");
        ui.label("Provider hooks supply activity. Optional transcript monitoring adds events from saved sessions.");
        ui.add_space(16.0);
        let Some(state) = &self.state else {
            return;
        };
        let controls = state.settings.controls.clone();
        for (provider, label, mut enabled) in [
            ("codex", "Codex transcripts", controls.codex_transcripts),
            ("claude", "Claude transcripts", controls.claude_transcripts),
        ] {
            if ui.checkbox(&mut enabled, label).changed() {
                self.send(RequestKind::SetTranscriptMonitoring {
                    provider: provider.into(),
                    enabled,
                });
            }
        }
        ui.add_space(20.0);
        ui.add_enabled_ui(!self.monitoring_saving, |ui| {
            ui.strong("Agent list");
            ui.horizontal(|ui| {
                ui.label("Consider inactive after");
                self.monitoring_dirty |= ui
                    .add(
                        egui::DragValue::new(&mut self.idle_minutes)
                            .range(0.0..=525600.0)
                            .suffix(" minutes"),
                    )
                    .changed();
            });
            ui.horizontal(|ui| {
                ui.label("Keep completed sessions for");
                self.monitoring_dirty |= ui
                    .add(
                        egui::DragValue::new(&mut self.retention_hours)
                            .range(0.0..=8760.0)
                            .suffix(" hours"),
                    )
                    .changed();
            });
            ui.add_space(12.0);
            ui.horizontal(|ui| {
                if ui
                    .add_enabled(
                        self.monitoring_dirty,
                        egui::Button::new("Save agent list settings"),
                    )
                    .clicked()
                {
                    self.send(RequestKind::SetAgentListSettings {
                        patch: AgentListSettingsPatch {
                            idle_timeout_seconds: Some(self.idle_minutes * 60.0),
                            recent_session_retention_seconds: Some(self.retention_hours * 3600.0),
                        },
                    });
                    self.monitoring_saving = true;
                }
                if ui
                    .add_enabled(self.monitoring_dirty, egui::Button::new("Reset changes"))
                    .clicked()
                {
                    self.monitoring_dirty = false;
                }
            });
        });
    }

    fn sleep(&mut self, ui: &mut egui::Ui) {
        ui.heading("Sleep prevention");
        #[cfg(target_os = "macos")]
        {
            ui.label("Choose when SidePulse keeps your Mac awake.");
            ui.add_space(16.0);
            let policy = self
                .state
                .as_ref()
                .and_then(|state| state.settings.controls.sleep_policy.clone());
            for choice in sidepulse_ui_model::SLEEP_CHOICES {
                if ui
                    .radio(policy.as_deref() == Some(choice.value), choice.label)
                    .clicked()
                {
                    self.send(RequestKind::SetSleepPolicy {
                        policy: choice.value.into(),
                    });
                }
            }
            ui.add_space(16.0);
            ui.add_enabled_ui(!self.sleep_saving, |ui| {
                ui.horizontal(|ui| {
                    ui.label("Allow sleep when battery falls below");
                    self.sleep_dirty |= ui
                        .add(
                            egui::DragValue::new(&mut self.sleep_battery_percent)
                                .range(0.0..=100.0)
                                .suffix("%"),
                        )
                        .changed();
                });
                ui.weak("The safeguard applies while your Mac is running on battery power.");
                if ui
                    .add_enabled(
                        self.sleep_dirty,
                        egui::Button::new("Save battery safeguard"),
                    )
                    .clicked()
                {
                    self.send(RequestKind::SetSleepSettings {
                        patch: sidepulse_core::SleepSettingsPatch {
                            min_battery_percent: Some(self.sleep_battery_percent),
                            ..Default::default()
                        },
                    });
                    self.sleep_saving = true;
                }
            });
            ui.add_space(16.0);
            ui.weak("These preferences apply when sleep prevention is enabled.");
        }
        #[cfg(not(target_os = "macos"))]
        ui.label("Sleep prevention is currently available on macOS.");
    }

    fn history(&mut self, ui: &mut egui::Ui) {
        ui.heading("Activity history");
        ui.label("Agent status, battery level, charger power, and sleep activity over time.");
        let Some(state) = &self.state else {
            return;
        };
        let points = state.history_points.clone();
        let mut timeframe = state.history_timeframe;
        let sampled = state.history_sampled;
        ui.add_space(12.0);
        egui::ComboBox::from_id_salt("history-timeframe")
            .selected_text(format!("Last {} hours", timeframe / 3600))
            .show_ui(ui, |ui| {
                for choice in sidepulse_core::HISTORY_TIMEFRAMES {
                    if ui
                        .selectable_value(
                            &mut timeframe,
                            choice,
                            format!("{} hours", choice / 3600),
                        )
                        .changed()
                    {
                        self.send(RequestKind::SetHistoryTimeframe { seconds: choice });
                    }
                }
            });
        if points.is_empty() {
            ui.add_space(20.0);
            ui.label("No history yet. New observations appear while the service is running.");
            return;
        }
        if sampled {
            ui.weak("Showing a summary of the recorded observations.");
        }
        ui.add_space(16.0);
        let (rect, response) = ui.allocate_exact_size(
            egui::vec2(ui.available_width(), 260.0),
            egui::Sense::hover(),
        );
        let painter = ui.painter_at(rect);
        let chart = rect.shrink2(egui::vec2(12.0, 20.0));
        painter.rect_filled(rect, 6, ui.visuals().faint_bg_color);
        let first = points.first().unwrap().recorded_at.timestamp_millis();
        let last = points.last().unwrap().recorded_at.timestamp_millis();
        let span = (last - first).max(1) as f32;
        let x = |point: &sidepulse_core::HistoryPoint| {
            chart.left()
                + (point.recorded_at.timestamp_millis() - first) as f32 / span * chart.width()
        };
        let battery_rect =
            egui::Rect::from_min_max(chart.min, egui::pos2(chart.right(), chart.top() + 95.0));
        let charger_rect = egui::Rect::from_min_max(
            egui::pos2(chart.left(), chart.top() + 115.0),
            egui::pos2(chart.right(), chart.top() + 175.0),
        );
        let charger_max = points
            .iter()
            .filter_map(|point| point.charger_power_watts)
            .fold(10.0_f64, f64::max)
            .max(10.0);
        for level in [0.0_f32, 0.5, 1.0] {
            let y = battery_rect.bottom() - level * battery_rect.height();
            painter.line_segment(
                [egui::pos2(chart.left(), y), egui::pos2(chart.right(), y)],
                egui::Stroke::new(1.0, ui.visuals().widgets.noninteractive.bg_stroke.color),
            );
        }
        for pair in points.windows(2) {
            let a = &pair[0];
            let b = &pair[1];
            if let (Some(a_value), Some(b_value)) = (a.battery_level, b.battery_level) {
                painter.line_segment(
                    [
                        egui::pos2(
                            x(a),
                            battery_rect.bottom()
                                - a_value.clamp(0.0, 100.0) as f32 / 100.0 * battery_rect.height(),
                        ),
                        egui::pos2(
                            x(b),
                            battery_rect.bottom()
                                - b_value.clamp(0.0, 100.0) as f32 / 100.0 * battery_rect.height(),
                        ),
                    ],
                    egui::Stroke::new(2.0, egui::Color32::from_rgb(80, 190, 130)),
                );
            }
            if let (Some(a_value), Some(b_value)) = (a.charger_power_watts, b.charger_power_watts) {
                painter.line_segment(
                    [
                        egui::pos2(
                            x(a),
                            charger_rect.bottom()
                                - (a_value / charger_max) as f32 * charger_rect.height(),
                        ),
                        egui::pos2(
                            x(b),
                            charger_rect.bottom()
                                - (b_value / charger_max) as f32 * charger_rect.height(),
                        ),
                    ],
                    egui::Stroke::new(2.0, egui::Color32::from_rgb(95, 165, 230)),
                );
            }
            let color = match a.agent_status {
                AgentMode::Working | AgentMode::ToolRunning | AgentMode::LongTaskProgress => {
                    egui::Color32::from_rgb(50, 190, 210)
                }
                AgentMode::WaitingForInput | AgentMode::BlockedError => {
                    egui::Color32::from_rgb(225, 155, 55)
                }
                AgentMode::Completed => egui::Color32::from_rgb(80, 190, 130),
                _ => ui.visuals().weak_text_color(),
            };
            painter.rect_filled(
                egui::Rect::from_min_max(
                    egui::pos2(x(a), chart.bottom() - 14.0),
                    egui::pos2(x(b).max(x(a) + 1.0), chart.bottom() - 6.0),
                ),
                0,
                color,
            );
        }
        painter.text(
            battery_rect.left_top(),
            egui::Align2::LEFT_TOP,
            "Battery · 0–100%",
            egui::FontId::proportional(12.0),
            ui.visuals().text_color(),
        );
        painter.text(
            charger_rect.left_top(),
            egui::Align2::LEFT_TOP,
            format!("Charger · 0–{charger_max:.0} W"),
            egui::FontId::proportional(12.0),
            ui.visuals().text_color(),
        );
        if let Some(position) = response.hover_pos() {
            let nearest = points
                .iter()
                .min_by(|a, b| {
                    (x(a) - position.x)
                        .abs()
                        .total_cmp(&(x(b) - position.x).abs())
                })
                .unwrap();
            response.on_hover_text(format!(
                "{}\n{}\nBattery: {}\nCharger: {}\nLid: {}\nKeeping awake: {}",
                nearest.recorded_at.format("%b %d %H:%M:%S UTC"),
                nearest.agent_status.label(),
                nearest
                    .battery_level
                    .map_or("Unknown".into(), |value| format!("{value:.0}%")),
                nearest
                    .charger_power_watts
                    .map_or("Unknown".into(), |value| format!("{value:.1} W")),
                nearest.lid_closed.map_or("Unknown", |closed| if closed {
                    "Closed"
                } else {
                    "Open"
                }),
                nearest
                    .keep_awake_active
                    .map_or("Unknown", |active| if active { "Yes" } else { "No" })
            ));
        }
        ui.horizontal(|ui| {
            ui.weak(
                points
                    .first()
                    .unwrap()
                    .recorded_at
                    .format("%b %d %H:%M UTC")
                    .to_string(),
            );
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                ui.weak(
                    points
                        .last()
                        .unwrap()
                        .recorded_at
                        .format("%b %d %H:%M UTC")
                        .to_string(),
                );
            });
        });
        ui.add_space(8.0);
        ui.label("Status: cyan = working · amber = needs attention · green = completed");
        if let Some(latest) = points.last() {
            ui.label(format!(
                "Latest: {} · Battery {} · Charger {}",
                latest.agent_status.label(),
                latest
                    .battery_level
                    .map_or("unknown".into(), |value| format!("{value:.0}%")),
                latest
                    .charger_power_watts
                    .map_or("unknown".into(), |value| format!("{value:.1} W"))
            ));
        }
    }

    fn phones(&mut self, ui: &mut egui::Ui) {
        ui.heading("Link phones");
        ui.label("Send LED programs and notifications to a phone running SidePulse.");
        let Some(state) = &self.state else {
            return;
        };
        if !state.phones_configured {
            ui.weak("Phone linking is unavailable in this session.");
            return;
        }
        if !state.phone_output_enabled {
            ui.weak("Automatic phone updates are paused in this session.");
        }
        let phones = state.phones.clone();
        let pairing = state.phone_pairing.clone();
        ui.add_space(16.0);
        ui.add_enabled_ui(!self.phone_saving, |ui| {
            if let Some(pairing) = pairing {
                if pairing.state == "awaiting" {
                    ui.label("Scan this code with SidePulse on your phone.");
                    let cells = pairing.qr.len();
                    if cells > 0 && pairing.qr.iter().all(|row| row.len() == cells) {
                        let size = (240.0 / cells as f32).floor().max(1.0) * cells as f32;
                        let (rect, _) =
                            ui.allocate_exact_size(egui::vec2(size, size), egui::Sense::hover());
                        let cell = size / cells as f32;
                        ui.painter().rect_filled(rect, 0.0, egui::Color32::WHITE);
                        for (y, row) in pairing.qr.iter().enumerate() {
                            for (x, dark) in row.iter().enumerate() {
                                if *dark {
                                    let origin =
                                        rect.min + egui::vec2(x as f32 * cell, y as f32 * cell);
                                    ui.painter().rect_filled(
                                        egui::Rect::from_min_size(origin, egui::vec2(cell, cell)),
                                        0.0,
                                        egui::Color32::BLACK,
                                    );
                                }
                            }
                        }
                    }
                    ui.horizontal(|ui| {
                        if ui.button("Copy pairing link").clicked() {
                            ui.ctx().copy_text(pairing.url.clone());
                        }
                        if ui.button("Cancel pairing").clicked() {
                            self.send(RequestKind::CancelPhonePairing);
                            self.phone_saving = true;
                        }
                    });
                    ui.weak(format!(
                        "Expires at {} UTC",
                        pairing.expires_at.format("%H:%M:%S")
                    ));
                } else {
                    ui.label(match pairing.state.as_str() {
                        "linked" => "Phone linked.",
                        "cancelled" => "Pairing cancelled.",
                        "expired" => "Pairing expired. Start again to get a new code.",
                        _ => "Pairing could not finish.",
                    });
                }
                if let Some(message) = pairing.message {
                    ui.weak(message);
                }
            }
            if ui.button("Start new pairing").clicked() {
                self.send(RequestKind::BeginPhonePairing { server: None });
                self.phone_saving = true;
            }
            ui.separator();
            ui.heading("Saved phones");
            if phones.is_empty() {
                ui.weak("No phones linked yet.");
            }
            for phone in phones {
                ui.horizontal(|ui| {
                    ui.label(&phone.name);
                    ui.weak(&phone.id);
                    let mut display = phone.display.clone();
                    egui::ComboBox::from_id_salt(("phone-display", &phone.id))
                        .selected_text(match display.as_str() {
                            "battery" => "Battery",
                            "custom" => "Manual",
                            _ => "Agent activity",
                        })
                        .show_ui(ui, |ui| {
                            ui.selectable_value(&mut display, "agent".into(), "Agent activity");
                            ui.selectable_value(&mut display, "battery".into(), "Battery");
                            ui.selectable_value(&mut display, "custom".into(), "Manual");
                        });
                    if display != phone.display {
                        self.send(RequestKind::SetPhoneDisplay {
                            id: phone.id.clone(),
                            display,
                        });
                        self.phone_saving = true;
                    }
                    if ui.button("Remove").clicked() {
                        self.send(RequestKind::RemovePhone {
                            id: phone.id.clone(),
                        });
                        self.phone_saving = true;
                    }
                });
                if let Some(at) = phone.last_sent_at {
                    ui.weak(format!(
                        "Last update sent: {} UTC",
                        at.format("%Y-%m-%d %H:%M:%S")
                    ));
                }
                if let Some(error) = phone.delivery_error {
                    ui.colored_label(
                        egui::Color32::from_rgb(210, 80, 65),
                        format!("Could not send an update: {error}"),
                    );
                }
            }
            ui.collapsing("Link with a push token", |ui| {
                ui.label("Phone name");
                ui.text_edit_singleline(&mut self.phone_name);
                ui.label("Push token");
                ui.add(egui::TextEdit::singleline(&mut self.phone_token).password(true));
                if ui
                    .add_enabled(
                        !self.phone_token.trim().is_empty(),
                        egui::Button::new("Save phone"),
                    )
                    .clicked()
                {
                    self.send(RequestKind::RegisterPhone {
                        token: self.phone_token.clone(),
                        name: self.phone_name.clone(),
                        server: None,
                    });
                    self.phone_saving = true;
                }
            });
            if ui.button("Reload saved phones").clicked() {
                self.send(RequestKind::ReloadPhoneLinks);
                self.phone_saving = true;
            }
        });
    }

    fn relay(&mut self, ui: &mut egui::Ui) {
        use sidepulse_core::RelaySettingsPatch;
        ui.heading("Link computers");
        ui.label("See agent activity from your other computers.");
        let Some(state) = &self.state else {
            return;
        };
        let relay = state.relay.clone();
        if !relay.configured {
            ui.weak("Relay is unavailable in this session.");
            return;
        }
        ui.add_space(16.0);
        ui.add_enabled_ui(!self.relay_saving, |ui| {
            ui.heading("Receive activity here");
            if relay.receiver_code.is_empty() {
                if ui.button("Create receiving code").clicked() {
                    self.send(RequestKind::SetRelaySettings {
                        patch: RelaySettingsPatch {
                            receiver_enabled: Some(true),
                            ..Default::default()
                        },
                    });
                    self.relay_saving = true;
                }
            } else {
                ui.horizontal(|ui| {
                    ui.monospace(&relay.receiver_code);
                    if ui.button("Copy code").clicked() {
                        ui.ctx().copy_text(relay.receiver_code.clone());
                    }
                });
                ui.weak("Use this code to link the sending computer.");
                ui.horizontal(|ui| {
                    if ui.button("Replace code").clicked() {
                        self.send(RequestKind::SetRelaySettings {
                            patch: RelaySettingsPatch {
                                rotate_receiver: true,
                                ..Default::default()
                            },
                        });
                        self.relay_saving = true;
                    }
                    if ui.button("Stop receiving").clicked() {
                        self.send(RequestKind::SetRelaySettings {
                            patch: RelaySettingsPatch {
                                receiver_enabled: Some(false),
                                ..Default::default()
                            },
                        });
                        self.relay_saving = true;
                    }
                });
                if let Some(at) = relay.last_received_at {
                    ui.weak(format!(
                        "Last activity received: {} UTC",
                        at.format("%Y-%m-%d %H:%M:%S")
                    ));
                }
                if let Some(error) = &relay.receive_error {
                    ui.colored_label(
                        egui::Color32::from_rgb(210, 80, 65),
                        format!("Could not receive activity: {error}"),
                    );
                }
            }
            ui.separator();
            ui.heading("Send activity to another computer");
            ui.label("Receiving code from the other computer");
            self.relay_dirty |= ui.text_edit_singleline(&mut self.relay_outbound).changed();
            ui.label("This computer's name");
            self.relay_dirty |= ui.text_edit_singleline(&mut self.relay_name).changed();
            ui.collapsing("Server", |ui| {
                self.relay_dirty |= ui.text_edit_singleline(&mut self.relay_server).changed();
            });
            ui.horizontal(|ui| {
                if ui
                    .add_enabled(self.relay_dirty, egui::Button::new("Save link"))
                    .clicked()
                {
                    self.send(RequestKind::SetRelaySettings {
                        patch: RelaySettingsPatch {
                            server: Some(self.relay_server.clone()),
                            machine_name: Some(self.relay_name.clone()),
                            outbound_code: Some(self.relay_outbound.clone()),
                            ..Default::default()
                        },
                    });
                    self.relay_saving = true;
                }
                if ui
                    .add_enabled(self.relay_dirty, egui::Button::new("Reset changes"))
                    .clicked()
                {
                    self.relay_dirty = false;
                }
                if ui.button("Reload saved link").clicked() {
                    self.send(RequestKind::ReloadRelaySettings);
                    self.relay_saving = true;
                }
            });
            if !relay.outbound_code.is_empty() {
                if ui.button("Disconnect sending link").clicked() {
                    self.relay_outbound.clear();
                    self.relay_dirty = true;
                    self.send(RequestKind::SetRelaySettings {
                        patch: RelaySettingsPatch {
                            outbound_code: Some(String::new()),
                            ..Default::default()
                        },
                    });
                    self.relay_saving = true;
                }
                if let Some(at) = relay.last_sent_at {
                    ui.weak(format!(
                        "Last activity sent: {} UTC",
                        at.format("%Y-%m-%d %H:%M:%S")
                    ));
                }
                if let Some(error) = &relay.send_error {
                    ui.colored_label(
                        egui::Color32::from_rgb(210, 80, 65),
                        format!("Could not send activity: {error}"),
                    );
                }
            }
        });
    }

    fn edit_library(&mut self, edit: sidepulse_core::AnimationLibraryEdit) {
        self.send(RequestKind::EditAnimationLibrary { edit });
        self.library_saving = true;
    }

    fn animation_profiles(&mut self, ui: &mut egui::Ui) {
        use sidepulse_core::AnimationLibraryEdit;
        let Some(state) = &self.state else {
            return;
        };
        let library = state.animation_library.clone();
        let choices = state.animation_choices.clone();
        ui.add_enabled_ui(
            !self.library_saving && !self.animation_dirty && !self.animation_saving,
            |ui| {
                ui.collapsing("Animation profiles", |ui| {
                    let current = library
                        .matching_profile
                        .as_ref()
                        .and_then(|id| library.profiles.get(id))
                        .map_or("Custom selection", |profile| profile.name.as_str());
                    ui.label(format!("Current: {current}"));
                    for (id, profile) in &library.profiles {
                        ui.horizontal(|ui| {
                            ui.label(&profile.name);
                            if ui.button("Apply").clicked() {
                                self.edit_library(AnimationLibraryEdit::ApplyProfile {
                                    id: id.clone(),
                                });
                            }
                            if ui.button("Export").clicked() {
                                self.send(RequestKind::ExportAnimationProfile {
                                    id: Some(id.clone()),
                                });
                            }
                            if !sidepulse_core::BUILTIN_PROFILE_IDS.contains(&id.as_str())
                                && ui.button("Delete").clicked()
                            {
                                self.edit_library(AnimationLibraryEdit::DeleteProfile {
                                    id: id.clone(),
                                });
                            }
                        });
                    }
                    ui.horizontal(|ui| {
                        ui.label("Profile name");
                        ui.text_edit_singleline(&mut self.profile_name);
                        if ui
                            .add_enabled(
                                !self.profile_name.trim().is_empty(),
                                egui::Button::new("Save current as profile"),
                            )
                            .clicked()
                        {
                            self.edit_library(AnimationLibraryEdit::SaveProfile {
                                id: None,
                                name: self.profile_name.clone(),
                            });
                        }
                    });
                    if ui.button("Export current selection").clicked() {
                        self.send(RequestKind::ExportAnimationProfile { id: None });
                    }
                    ui.label("Import or export profile JSON");
                    ui.add(
                        egui::TextEdit::multiline(&mut self.profile_json)
                            .font(egui::TextStyle::Monospace)
                            .desired_rows(6)
                            .desired_width(f32::INFINITY),
                    );
                    ui.horizontal(|ui| {
                        if ui.button("Copy JSON").clicked() {
                            ui.ctx().copy_text(self.profile_json.clone());
                        }
                        if ui.button("Import and apply").clicked() {
                            match serde_json::from_str(&self.profile_json) {
                                Ok(document) => self
                                    .edit_library(AnimationLibraryEdit::ImportProfile { document }),
                                Err(error) => {
                                    self.message =
                                        Some((format!("Invalid profile JSON: {error}"), true))
                                }
                            }
                        }
                    });
                });
                ui.collapsing("Named custom animations", |ui| {
                    egui::ComboBox::from_id_salt("named-animation")
                        .selected_text(
                            self.asset_id
                                .as_ref()
                                .and_then(|id| library.custom_animations.get(id))
                                .map_or("New animation", |asset| asset.name.as_str()),
                        )
                        .show_ui(ui, |ui| {
                            if ui
                                .selectable_label(self.asset_id.is_none(), "New animation")
                                .clicked()
                            {
                                self.asset_id = None;
                                self.asset_name.clear();
                                self.asset_program = "#00E5FF".into();
                            }
                            for (id, asset) in &library.custom_animations {
                                if ui
                                    .selectable_label(
                                        self.asset_id.as_ref() == Some(id),
                                        &asset.name,
                                    )
                                    .clicked()
                                {
                                    self.asset_id = Some(id.clone());
                                    self.asset_name = asset.name.clone();
                                    self.asset_program = asset.program.clone();
                                }
                            }
                        });
                    ui.label("Animation name");
                    ui.text_edit_singleline(&mut self.asset_name);
                    ui.add(
                        egui::TextEdit::multiline(&mut self.asset_program)
                            .font(egui::TextStyle::Monospace)
                            .desired_rows(8)
                            .desired_width(f32::INFINITY),
                    );
                    ui.horizontal(|ui| {
                        if ui
                            .add_enabled(
                                !self.asset_name.trim().is_empty(),
                                egui::Button::new("Save named animation"),
                            )
                            .clicked()
                        {
                            self.edit_library(AnimationLibraryEdit::SaveAnimation {
                                id: self.asset_id.clone(),
                                name: self.asset_name.clone(),
                                program: self.asset_program.clone(),
                            });
                        }
                        if let Some(id) = &self.asset_id
                            && ui.button("Delete").clicked()
                        {
                            self.edit_library(AnimationLibraryEdit::DeleteAnimation {
                                id: id.clone(),
                            });
                        }
                    });
                });
                ui.collapsing("Lid transition animations", |ui| {
                    for (state, label) in [
                        ("lid_open", "Opening the lid"),
                        ("lid_closed", "Closing the lid"),
                    ] {
                        ui.horizontal(|ui| {
                            ui.label(label);
                            let mut style = library.current[state].clone();
                            let original = style.clone();
                            egui::ComboBox::from_id_salt(state)
                                .selected_text(
                                    choices
                                        .iter()
                                        .find(|choice| choice.id == style)
                                        .map_or(style.as_str(), |choice| choice.name.as_str()),
                                )
                                .show_ui(ui, |ui| {
                                    for choice in &choices {
                                        if choice.id != "custom" {
                                            ui.selectable_value(
                                                &mut style,
                                                choice.id.clone(),
                                                &choice.name,
                                            );
                                        }
                                    }
                                });
                            if style != original {
                                self.send(RequestKind::SetAnimationState {
                                    state: state.into(),
                                    style,
                                    custom_program: None,
                                });
                                self.library_saving = true;
                            }
                        });
                    }
                    ui.weak("Custom transition duration; default lid presets use their built-in timing.");
                ui.horizontal(|ui| {
                    ui.label("Open seconds");
                    self.lid_duration_dirty |= ui.add(egui::DragValue::new(&mut self.lid_durations[0]).range(0.1..=10.0).speed(0.1)).changed();
                    ui.label("Close seconds");
                    self.lid_duration_dirty |= ui.add(egui::DragValue::new(&mut self.lid_durations[1]).range(0.1..=10.0).speed(0.1)).changed();
                    if ui.add_enabled(self.lid_duration_dirty, egui::Button::new("Save timing")).clicked() {
                        self.send(RequestKind::SetLidAnimationTiming { open_seconds: Some(self.lid_durations[0]), close_seconds: Some(self.lid_durations[1]) });
                        self.library_saving = true;
                    }
                    if ui.add_enabled(self.lid_duration_dirty, egui::Button::new("Reset timing")).clicked() { self.lid_duration_dirty = false; }
                });
            });
            },
        );
    }

    fn animations(&mut self, ui: &mut egui::Ui) {
        ui.heading("Agent animations");
        ui.label("Choose a device animation for each agent status.");
        ui.add_space(16.0);
        let Some(state) = &self.state else {
            return;
        };
        let choices = state.animation_choices.clone();
        let states = state.animation_states.clone();
        self.animation_profiles(ui);
        ui.separator();
        ui.add_enabled_ui(!self.animation_saving && !self.library_saving, |ui| {
            ui.add_enabled_ui(!self.animation_dirty, |ui| {
                egui::ComboBox::from_id_salt("animation-mode")
                    .selected_text(self.animation_mode.label())
                    .show_ui(ui, |ui| {
                        for mode in AgentMode::ALL {
                            if ui
                                .selectable_value(&mut self.animation_mode, mode, mode.label())
                                .changed()
                                && let Some(animation) =
                                    states.iter().find(|animation| animation.mode == mode)
                            {
                                self.animation_style = animation.style.clone();
                                self.animation_program = animation.program.clone();
                            }
                        }
                    });
            });
            ui.add_space(8.0);
            let selected = choices
                .iter()
                .find(|choice| choice.id == self.animation_style)
                .map_or(self.animation_style.as_str(), |choice| choice.name.as_str())
                .to_owned();
            egui::ComboBox::from_id_salt("animation-style")
                .selected_text(selected)
                .show_ui(ui, |ui| {
                    for choice in choices {
                        if ui
                            .selectable_value(&mut self.animation_style, choice.id, choice.name)
                            .changed()
                        {
                            self.animation_dirty = true;
                            if self.animation_style == "custom"
                                && self.animation_program.trim().is_empty()
                            {
                                self.animation_program = "#00E5FF 500ms pulse\nrepeat\n".into();
                            }
                        }
                    }
                });
            if matches!(
                self.animation_mode,
                AgentMode::Working | AgentMode::ToolRunning | AgentMode::LongTaskProgress
            ) {
                ui.weak("Working, tool running, and long task progress share their animation.");
            }
            if self.animation_style == "custom" {
                ui.add_space(12.0);
                ui.label("Custom LED program");
                self.animation_dirty |= ui
                    .add(
                        egui::TextEdit::multiline(&mut self.animation_program)
                            .font(egui::TextStyle::Monospace)
                            .desired_width(f32::INFINITY)
                            .desired_rows(10),
                    )
                    .changed();
                ui.weak("The program is checked before it is saved or sent to a device.");
            }
            ui.add_space(16.0);
            ui.horizontal(|ui| {
                if ui
                    .add_enabled(self.animation_dirty, egui::Button::new("Save animation"))
                    .clicked()
                {
                    self.send(RequestKind::SetAgentAnimation {
                        mode: self.animation_mode,
                        style: self.animation_style.clone(),
                        custom_program: (self.animation_style == "custom")
                            .then(|| self.animation_program.clone()),
                    });
                    self.animation_saving = true;
                }
                if ui
                    .add_enabled(self.animation_dirty, egui::Button::new("Reset changes"))
                    .clicked()
                {
                    self.animation_dirty = false;
                }
            });
        });
    }
}

impl eframe::App for SettingsApp {
    fn ui(&mut self, ui: &mut egui::Ui, _frame: &mut eframe::Frame) {
        self.poll();
        ui.ctx().request_repaint_after(Duration::from_millis(100));
        egui::CentralPanel::default().show(ui, |ui| {
            ui.heading("SidePulse");
            ui.label("Agent activity and device settings");
            ui.add_space(8.0);
            if self.connected {
                ui.colored_label(egui::Color32::from_rgb(65, 180, 100), "Connected");
            } else {
                ui.colored_label(
                    egui::Color32::from_rgb(210, 140, 50),
                    "Service unavailable · reconnecting…",
                );
            }
            ui.add_space(12.0);
            ui.horizontal_wrapped(|ui| {
                for (page, label) in [
                    (Page::Activity, "Activity"),
                    (Page::Devices, "Devices"),
                    (Page::Battery, "Battery"),
                    (Page::Monitoring, "Monitoring"),
                    (Page::Sleep, "Sleep"),
                    (Page::Animations, "Animations"),
                    (Page::History, "History"),
                    (Page::Sessions, "Sessions"),
                    (Page::Relay, "Link computers"),
                    (Page::Phones, "Link phones"),
                ] {
                    ui.selectable_value(&mut self.page, page, label);
                }
            });
            ui.separator();
            if let Some((message, error)) = &self.message {
                if *error {
                    ui.colored_label(egui::Color32::from_rgb(210, 80, 65), message);
                } else {
                    ui.weak(message);
                }
                ui.add_space(8.0);
            }
            egui::ScrollArea::vertical().show(ui, |ui| {
                ui.add_enabled_ui(self.connected, |ui| match self.page {
                    Page::Activity => self.activity(ui),
                    Page::Devices => self.devices(ui),
                    Page::Battery => {
                        ui.add_enabled_ui(!self.battery_saving, |ui| self.battery(ui));
                    }
                    Page::Monitoring => self.monitoring(ui),
                    Page::Sleep => self.sleep(ui),
                    Page::Animations => self.animations(ui),
                    Page::History => self.history(ui),
                    Page::Sessions => self.sessions(ui),
                    Page::Relay => self.relay(ui),
                    Page::Phones => self.phones(ui),
                });
            });
        });
    }
}

fn main() -> eframe::Result {
    let mut args = std::env::args().skip(1);
    let endpoint = match (args.next(), args.next()) {
        (Some(endpoint), None) => Some(endpoint),
        (None, None) => std::env::var("SIDEPULSE_NEXT_ENDPOINT").ok().or_else(|| {
            let executable = std::env::current_exe().ok()?;
            let contents = executable.parent()?.parent()?;
            std::fs::read_to_string(contents.join("Resources/endpoint.txt")).ok()
        }),
        _ => {
            eprintln!("usage: sidepulse-next-settings ENDPOINT");
            std::process::exit(2);
        }
    };
    let Some(endpoint) = endpoint.filter(|endpoint| !endpoint.trim().is_empty()) else {
        eprintln!("usage: sidepulse-next-settings ENDPOINT");
        std::process::exit(2);
    };
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_inner_size([720.0, 600.0])
            .with_min_inner_size([580.0, 420.0]),
        ..Default::default()
    };
    eframe::run_native(
        "SidePulse settings",
        options,
        Box::new(move |_context| Ok(Box::new(SettingsApp::new(endpoint)))),
    )
}
