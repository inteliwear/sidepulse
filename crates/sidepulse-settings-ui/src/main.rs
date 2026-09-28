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
    devices: Vec<DeviceInfo>,
    active_device: Option<String>,
    animation_choices: Vec<AnimationChoice>,
    animation_states: Vec<AgentAnimationState>,
}

enum Update {
    State(Result<Box<ServiceState>, String>),
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
    Ok(ServiceState {
        settings,
        activity,
        devices,
        active_device,
        animation_choices: choices,
        animation_states: states,
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
                    let draft = match kind {
                        RequestKind::SetBatterySettings { .. } => DraftKind::Battery,
                        RequestKind::SetAgentListSettings { .. } => DraftKind::Monitoring,
                        RequestKind::SetSleepSettings { .. } => DraftKind::Sleep,
                        RequestKind::SetAgentAnimation { .. } => DraftKind::Animation,
                        _ => DraftKind::None,
                    };
                    let result = request(&endpoint, kind).and_then(|payload| match payload {
                        ServerPayload::Settings { .. } | ServerPayload::Devices { .. } => Ok(()),
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
                        DraftKind::None => {}
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

    fn activity(&self, ui: &mut egui::Ui) {
        let Some(state) = &self.state else {
            ui.label("Waiting for activity…");
            return;
        };
        ui.heading(&state.activity.tooltip);
        ui.add_space(12.0);
        if state.activity.rows.is_empty() {
            ui.label("No active agents.");
        }
        for row in &state.activity.rows {
            ui.group(|ui| {
                ui.strong(&row.title);
                ui.label(&row.subtitle);
            });
        }
        if !state.activity.stale_rows.is_empty() {
            ui.add_space(16.0);
            ui.collapsing("Recent sessions", |ui| {
                for row in &state.activity.stale_rows {
                    ui.label(&row.title);
                    ui.weak(&row.subtitle);
                }
            });
        }
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

    fn animations(&mut self, ui: &mut egui::Ui) {
        ui.heading("Agent animations");
        ui.label("Choose a device animation for each agent status.");
        ui.add_space(16.0);
        let Some(state) = &self.state else {
            return;
        };
        let choices = state.animation_choices.clone();
        let states = state.animation_states.clone();
        ui.add_enabled_ui(!self.animation_saving, |ui| {
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
