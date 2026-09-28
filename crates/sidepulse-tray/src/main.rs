//! Development tray client: renders service snapshots without owning agent state.

use std::env;
use std::error::Error;
use std::time::Duration;

use sidepulse_core::{
    BatterySettingsPatch, ClientRequest, DeviceInfo, MonitorSnapshot, PROTOCOL_VERSION,
    RequestKind, ServerMessage, ServerPayload,
};
#[cfg(target_os = "macos")]
use sidepulse_ui_model::SLEEP_CHOICES;
use sidepulse_ui_model::{
    BRIGHTNESS_CHOICES, DISPLAY_CHOICES, StatusIcon, TrayControls, TrayState, brightness_label,
    device_display_name,
};
use tray_icon::menu::{Menu, MenuEvent, MenuItem, PredefinedMenuItem};
use tray_icon::{Icon, TrayIcon, TrayIconBuilder};

struct TrayView {
    tray: TrayIcon,
    menu: Menu,
    status: MenuItem,
    device_status: MenuItem,
    device_items: Vec<(MenuItem, String)>,
    brightness_status: MenuItem,
    brightness_items: Vec<(MenuItem, u8)>,
    display_status: MenuItem,
    display_items: Vec<(MenuItem, &'static str)>,
    transcript_status: MenuItem,
    transcript_items: Vec<(MenuItem, &'static str)>,
    transcript_enabled: Option<(bool, bool)>,
    battery_preview_item: MenuItem,
    battery_preview_enabled: Option<bool>,
    settings_item: MenuItem,
    settings_child: Option<std::process::Child>,
    virtual_child: Option<std::process::Child>,
    virtual_launch_attempted: bool,
    #[cfg(target_os = "macos")]
    sleep_status: MenuItem,
    #[cfg(target_os = "macos")]
    sleep_items: Vec<(MenuItem, &'static str)>,
    quit: MenuItem,
    visible_rows: usize,
}

impl TrayView {
    fn new() -> Result<Self, Box<dyn Error>> {
        let menu = Menu::new();
        let status = MenuItem::new("Connecting to SidePulse…", false, None);
        let separator = PredefinedMenuItem::separator();
        let device_status = MenuItem::new("No devices", false, None);
        let brightness_status = MenuItem::new("Device brightness unavailable", false, None);
        let brightness_items = BRIGHTNESS_CHOICES
            .iter()
            .map(|choice| (MenuItem::new(choice.label, false, None), choice.value))
            .collect::<Vec<_>>();
        let display_status = MenuItem::new("Device display unavailable", false, None);
        let display_items =
            DISPLAY_CHOICES.map(|choice| (MenuItem::new(choice.label, false, None), choice.value));
        let transcript_status = MenuItem::new("Transcript monitoring unavailable", false, None);
        let transcript_items = [
            (MenuItem::new("Codex transcripts", false, None), "codex"),
            (MenuItem::new("Claude transcripts", false, None), "claude"),
        ];
        let battery_preview_item = MenuItem::new("Show battery when power changes", false, None);
        #[cfg(target_os = "macos")]
        let sleep_status = MenuItem::new("Sleep prevention unavailable", false, None);
        #[cfg(target_os = "macos")]
        let sleep_items =
            SLEEP_CHOICES.map(|choice| (MenuItem::new(choice.label, false, None), choice.value));
        let controls_separator = PredefinedMenuItem::separator();
        let settings_item = MenuItem::new("Settings…", true, None);
        let quit = MenuItem::new("Quit SidePulse tray", true, None);
        menu.append_items(&[&status, &separator, &device_status, &brightness_status])?;
        for (item, _) in &brightness_items {
            menu.append(item)?;
        }
        menu.append(&display_status)?;
        for (item, _) in &display_items {
            menu.append(item)?;
        }
        menu.append(&transcript_status)?;
        for (item, _) in &transcript_items {
            menu.append(item)?;
        }
        menu.append(&battery_preview_item)?;
        #[cfg(target_os = "macos")]
        {
            menu.append(&sleep_status)?;
            for (item, _) in &sleep_items {
                menu.append(item)?;
            }
        }
        menu.append_items(&[&controls_separator, &settings_item, &quit])?;
        let tray = TrayIconBuilder::new()
            .with_menu(Box::new(menu.clone()))
            .with_icon(icon(StatusIcon::Unknown)?)
            .with_tooltip("SidePulse: connecting")
            .build()?;
        Ok(Self {
            tray,
            menu,
            status,
            device_status,
            device_items: Vec::new(),
            brightness_status,
            brightness_items,
            display_status,
            display_items: display_items.into(),
            transcript_status,
            transcript_items: transcript_items.into(),
            transcript_enabled: None,
            battery_preview_item,
            battery_preview_enabled: None,
            settings_item,
            settings_child: None,
            virtual_child: None,
            virtual_launch_attempted: false,
            #[cfg(target_os = "macos")]
            sleep_status,
            #[cfg(target_os = "macos")]
            sleep_items: sleep_items.into(),
            quit,
            visible_rows: 0,
        })
    }

    fn show_snapshot(&mut self, state: &TrayState) -> Result<(), Box<dyn Error>> {
        self.status.set_text(&state.tooltip);
        self.tray.set_tooltip(Some(&state.tooltip))?;
        self.tray.set_icon(Some(icon(state.icon)?))?;
        self.tray.set_title(Some(&state.title));
        for _ in 0..self.visible_rows {
            self.menu.remove_at(1);
        }
        self.visible_rows = 0;
        for row in state.rows.iter().chain(&state.stale_rows).take(12) {
            let item = MenuItem::new(
                format!(
                    "{}{} — {}",
                    if row.stale { "Recent · " } else { "" },
                    row.title,
                    row.subtitle
                ),
                false,
                None,
            );
            self.menu.insert(&item, 1 + self.visible_rows)?;
            self.visible_rows += 1;
        }
        Ok(())
    }

    fn show_disconnected(&mut self) -> Result<(), Box<dyn Error>> {
        let state = TrayState::disconnected();
        self.status.set_text(&state.tooltip);
        self.tray.set_tooltip(Some(&state.tooltip))?;
        self.tray.set_icon(Some(icon(state.icon)?))?;
        self.tray.set_title(Some(&state.title));
        for _ in 0..self.visible_rows {
            self.menu.remove_at(1);
        }
        self.visible_rows = 0;
        self.show_brightness(None);
        self.show_display_mode(None);
        self.show_transcript_monitoring(None);
        self.show_battery_preview(None);
        #[cfg(target_os = "macos")]
        self.show_sleep_policy(None);
        self.show_devices(&[], None)?;
        Ok(())
    }

    fn show_brightness(&self, brightness: Option<u8>) {
        self.brightness_status
            .set_text(brightness_label(brightness));
        for (item, value) in &self.brightness_items {
            item.set_enabled(brightness.is_some());
            let label = BRIGHTNESS_CHOICES
                .iter()
                .find(|choice| choice.value == *value)
                .map_or("Brightness", |choice| choice.label);
            item.set_text(if brightness == Some(*value) {
                format!("✓ {label}")
            } else {
                label.to_owned()
            });
        }
    }

    fn brightness_for_menu_event(&self, event: &MenuEvent) -> Option<u8> {
        self.brightness_items
            .iter()
            .find(|(item, _)| event.id == *item.id())
            .map(|(_, value)| *value)
    }

    fn show_display_mode(&self, mode: Option<&str>) {
        self.display_status.set_text(if mode.is_some() {
            "Device display"
        } else {
            "Device display unavailable"
        });
        for (item, value) in &self.display_items {
            item.set_enabled(mode.is_some());
            let label = DISPLAY_CHOICES
                .iter()
                .find(|choice| choice.value == *value)
                .map_or("Display", |choice| choice.label);
            item.set_text(if mode == Some(*value) {
                format!("✓ {label}")
            } else {
                label.to_owned()
            });
        }
    }

    fn display_for_menu_event(&self, event: &MenuEvent) -> Option<&'static str> {
        self.display_items
            .iter()
            .find(|(item, _)| event.id == *item.id())
            .map(|(_, value)| *value)
    }

    fn show_transcript_monitoring(&mut self, enabled: Option<(bool, bool)>) {
        self.transcript_status.set_text(if enabled.is_some() {
            "Transcript monitoring"
        } else {
            "Transcript monitoring unavailable"
        });
        for (item, provider) in &self.transcript_items {
            item.set_enabled(enabled.is_some());
            let active = enabled.is_some_and(
                |(codex, claude)| {
                    if *provider == "codex" { codex } else { claude }
                },
            );
            let label = if *provider == "codex" {
                "Codex transcripts"
            } else {
                "Claude transcripts"
            };
            item.set_text(if active {
                format!("✓ {label}")
            } else {
                label.to_owned()
            });
        }
        self.transcript_enabled = enabled;
    }

    fn transcript_for_menu_event(&self, event: &MenuEvent) -> Option<(&'static str, bool)> {
        let (codex, claude) = self.transcript_enabled?;
        self.transcript_items
            .iter()
            .find(|(item, _)| event.id == *item.id())
            .map(|(_, provider)| {
                (
                    *provider,
                    if *provider == "codex" {
                        !codex
                    } else {
                        !claude
                    },
                )
            })
    }

    fn show_battery_preview(&mut self, enabled: Option<bool>) {
        self.battery_preview_item.set_enabled(enabled.is_some());
        self.battery_preview_item
            .set_text(if enabled == Some(true) {
                "✓ Show battery when power changes"
            } else {
                "Show battery when power changes"
            });
        self.battery_preview_enabled = enabled;
    }

    fn battery_preview_for_menu_event(&self, event: &MenuEvent) -> Option<bool> {
        (event.id == *self.battery_preview_item.id())
            .then_some(self.battery_preview_enabled)
            .flatten()
            .map(|enabled| !enabled)
    }

    fn sync_virtual_display(
        &mut self,
        enabled: bool,
        endpoint: &str,
    ) -> Result<(), Box<dyn Error>> {
        if !enabled {
            self.virtual_launch_attempted = false;
            return Ok(());
        }
        if self.virtual_launch_attempted
            || self
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
                .arg(endpoint)
                .spawn()?,
        );
        Ok(())
    }

    fn open_settings(&mut self, endpoint: &str) -> Result<(), Box<dyn Error>> {
        if self
            .settings_child
            .as_mut()
            .is_some_and(|child| child.try_wait().is_ok_and(|status| status.is_none()))
        {
            return Ok(());
        }
        let current = std::env::current_exe()?;
        let executable = if cfg!(target_os = "macos") {
            current.ancestors().take(6).map(|path| path.join("applications/SidePulse Settings.app/Contents/MacOS/sidepulse-next-settings"))
                .find(|path| path.is_file())
        } else { None }.unwrap_or_else(|| current.with_file_name(if cfg!(windows) {
            "sidepulse-next-settings.exe"
        } else {
            "sidepulse-next-settings"
        }));
        self.settings_child = Some(
            std::process::Command::new(executable)
                .arg(endpoint)
                .spawn()?,
        );
        Ok(())
    }

    #[cfg(target_os = "macos")]
    fn show_sleep_policy(&self, policy: Option<&str>) {
        self.sleep_status.set_text(if policy.is_some() {
            "Prevent system sleep"
        } else {
            "Sleep prevention unavailable"
        });
        for (item, value) in &self.sleep_items {
            item.set_enabled(policy.is_some());
            let label = SLEEP_CHOICES
                .iter()
                .find(|choice| choice.value == *value)
                .map_or("Sleep policy", |choice| choice.label);
            item.set_text(if policy == Some(*value) {
                format!("✓ {label}")
            } else {
                label.to_owned()
            });
        }
    }

    #[cfg(target_os = "macos")]
    fn sleep_policy_for_menu_event(&self, event: &MenuEvent) -> Option<&'static str> {
        self.sleep_items
            .iter()
            .find(|(item, _)| event.id == *item.id())
            .map(|(_, policy)| *policy)
    }

    fn show_devices(
        &mut self,
        devices: &[DeviceInfo],
        active: Option<&str>,
    ) -> Result<(), Box<dyn Error>> {
        for (item, _) in self.device_items.drain(..) {
            self.menu.remove(&item)?;
        }
        self.device_status.set_text(if devices.is_empty() {
            "No devices".to_owned()
        } else {
            format!("Devices ({})", devices.len())
        });
        for (index, device) in devices.iter().enumerate() {
            let name = device_display_name(device);
            let label = if active == Some(device.target.as_str()) {
                format!("✓ {name}")
            } else {
                name
            };
            let item = MenuItem::new(label, true, None);
            self.menu.insert(&item, 3 + self.visible_rows + index)?;
            self.device_items.push((item, device.root.clone()));
        }
        Ok(())
    }

    fn device_for_menu_event(&self, event: &MenuEvent) -> Option<&str> {
        self.device_items
            .iter()
            .find(|(item, _)| event.id == *item.id())
            .map(|(_, root)| root.as_str())
    }
}

fn icon(state: StatusIcon) -> Result<Icon, tray_icon::BadIcon> {
    let color = match state {
        StatusIcon::Idle => [125, 130, 138],
        StatusIcon::Working => [37, 142, 232],
        StatusIcon::Tool => [103, 83, 212],
        StatusIcon::Waiting => [232, 159, 27],
        StatusIcon::LongTask => [42, 167, 180],
        StatusIcon::Error => [213, 61, 63],
        StatusIcon::Completed => [48, 167, 97],
        StatusIcon::Unknown => [110, 110, 110],
    };
    let mut rgba = vec![0; 16 * 16 * 4];
    for y in 0_i32..16 {
        for x in 0_i32..16 {
            let dx = x - 8;
            let dy = y - 8;
            let distance = dx * dx + dy * dy;
            let index = ((y * 16 + x) * 4) as usize;
            if distance <= 42 {
                rgba[index..index + 3].copy_from_slice(&color);
                rgba[index + 3] = 255;
            }
        }
    }
    Icon::from_rgba(rgba, 16, 16)
}

fn fetch_snapshot(endpoint: &str) -> Result<MonitorSnapshot, Box<dyn Error>> {
    let request = ClientRequest {
        version: PROTOCOL_VERSION,
        request_id: 1,
        kind: RequestKind::Snapshot,
    };
    let response: ServerMessage =
        sidepulse_ipc::request(endpoint, &request, Duration::from_secs(2))?;
    if response.version != PROTOCOL_VERSION || response.request_id != Some(1) {
        return Err("invalid service response".into());
    }
    match response.payload {
        ServerPayload::Snapshot { state } => Ok(state),
        _ => Err("service did not return a snapshot".into()),
    }
}

fn fetch_controls(endpoint: &str) -> Result<TrayControls, Box<dyn Error>> {
    let request = ClientRequest {
        version: PROTOCOL_VERSION,
        request_id: 2,
        kind: RequestKind::Settings,
    };
    let response: ServerMessage =
        sidepulse_ipc::request(endpoint, &request, Duration::from_secs(2))?;
    if response.version != PROTOCOL_VERSION || response.request_id != Some(2) {
        return Err("invalid service response".into());
    }
    TrayControls::from_service_payload(&response.payload)
        .ok_or_else(|| "service did not return settings".into())
}

fn send_brightness(endpoint: &str, brightness: u8) -> Result<(), Box<dyn Error>> {
    let request = ClientRequest {
        version: PROTOCOL_VERSION,
        request_id: 3,
        kind: RequestKind::SetBrightness { brightness },
    };
    let response: ServerMessage =
        sidepulse_ipc::request(endpoint, &request, Duration::from_secs(2))?;
    match response.payload {
        ServerPayload::Settings { .. } => Ok(()),
        ServerPayload::Error { message, .. } => Err(message.into()),
        _ => Err("service did not update brightness".into()),
    }
}

fn send_display_mode(endpoint: &str, mode: &str) -> Result<(), Box<dyn Error>> {
    let request = ClientRequest {
        version: PROTOCOL_VERSION,
        request_id: 5,
        kind: RequestKind::SetDisplayMode {
            mode: mode.to_owned(),
        },
    };
    let response: ServerMessage =
        sidepulse_ipc::request(endpoint, &request, Duration::from_secs(2))?;
    match response.payload {
        ServerPayload::Settings { .. } => Ok(()),
        ServerPayload::Error { message, .. } => Err(message.into()),
        _ => Err("service did not update display mode".into()),
    }
}

fn send_transcript_monitoring(
    endpoint: &str,
    provider: &str,
    enabled: bool,
) -> Result<(), Box<dyn Error>> {
    let request = ClientRequest {
        version: PROTOCOL_VERSION,
        request_id: 11,
        kind: RequestKind::SetTranscriptMonitoring {
            provider: provider.to_owned(),
            enabled,
        },
    };
    let response: ServerMessage =
        sidepulse_ipc::request(endpoint, &request, Duration::from_secs(2))?;
    match response.payload {
        ServerPayload::Settings { .. } => Ok(()),
        ServerPayload::Error { message, .. } => Err(message.into()),
        _ => Err("service did not update transcript monitoring".into()),
    }
}

fn send_battery_preview(endpoint: &str, enabled: bool) -> Result<(), Box<dyn Error>> {
    let request = ClientRequest {
        version: PROTOCOL_VERSION,
        request_id: 12,
        kind: RequestKind::SetBatterySettings {
            patch: BatterySettingsPatch {
                show_on_power_change: Some(enabled),
                ..Default::default()
            },
        },
    };
    let response: ServerMessage =
        sidepulse_ipc::request(endpoint, &request, Duration::from_secs(2))?;
    match response.payload {
        ServerPayload::Settings { .. } => Ok(()),
        ServerPayload::Error { message, .. } => Err(message.into()),
        _ => Err("service did not update battery preview".into()),
    }
}

#[cfg(target_os = "macos")]
fn send_sleep_policy(endpoint: &str, policy: &str) -> Result<(), Box<dyn Error>> {
    let request = ClientRequest {
        version: PROTOCOL_VERSION,
        request_id: 9,
        kind: RequestKind::SetSleepPolicy {
            policy: policy.to_owned(),
        },
    };
    let response: ServerMessage =
        sidepulse_ipc::request(endpoint, &request, Duration::from_secs(2))?;
    match response.payload {
        ServerPayload::Settings { .. } => Ok(()),
        ServerPayload::Error { message, .. } => Err(message.into()),
        _ => Err("service did not update sleep policy".into()),
    }
}

fn fetch_devices(endpoint: &str) -> Result<(Vec<DeviceInfo>, Option<String>), Box<dyn Error>> {
    let request = ClientRequest {
        version: PROTOCOL_VERSION,
        request_id: 6,
        kind: RequestKind::Devices,
    };
    let response: ServerMessage =
        sidepulse_ipc::request(endpoint, &request, Duration::from_secs(2))?;
    match response.payload {
        ServerPayload::Devices {
            devices,
            active_device,
        } => Ok((devices, active_device)),
        _ => Err("service did not return devices".into()),
    }
}

fn send_device_selection(endpoint: &str, root: &str) -> Result<(), Box<dyn Error>> {
    let request = ClientRequest {
        version: PROTOCOL_VERSION,
        request_id: 7,
        kind: RequestKind::SelectDevice {
            root: root.to_owned(),
        },
    };
    let response: ServerMessage =
        sidepulse_ipc::request(endpoint, &request, Duration::from_secs(2))?;
    match response.payload {
        ServerPayload::Devices { .. } => Ok(()),
        ServerPayload::Error { message, .. } => Err(message.into()),
        _ => Err("service did not select device".into()),
    }
}

#[cfg(any(target_os = "macos", target_os = "windows"))]
fn run(endpoint: String) -> Result<(), Box<dyn Error>> {
    use tao::event::{Event, StartCause};
    use tao::event_loop::{ControlFlow, EventLoopBuilder};

    enum UserEvent {
        Snapshot(
            Option<Box<MonitorSnapshot>>,
            Option<TrayControls>,
            Option<(Vec<DeviceInfo>, Option<String>)>,
        ),
        Menu(MenuEvent),
    }
    let event_loop = EventLoopBuilder::<UserEvent>::with_user_event().build();
    let proxy = event_loop.create_proxy();
    MenuEvent::set_event_handler(Some(move |event| {
        let _ = proxy.send_event(UserEvent::Menu(event));
    }));
    let mut worker_proxy = Some(event_loop.create_proxy());
    let mut worker_endpoint = Some(endpoint);
    let control_endpoint = worker_endpoint.as_ref().expect("endpoint is set").clone();
    let mut view: Option<TrayView> = None;
    let mut last_connected = None;
    let mut last_state: Option<TrayState> = None;
    let mut last_devices: Option<(Vec<DeviceInfo>, Option<String>)> = None;
    event_loop.run(move |event, _, flow| {
        *flow = ControlFlow::Wait;
        match event {
            Event::NewEvents(StartCause::Init) => {
                view = Some(TrayView::new().expect("create SidePulse tray"));
                if let (Some(proxy), Some(endpoint)) = (worker_proxy.take(), worker_endpoint.take())
                {
                    std::thread::spawn(move || {
                        loop {
                            let snapshot = fetch_snapshot(&endpoint).ok().map(Box::new);
                            let controls = fetch_controls(&endpoint).ok();
                            let devices = fetch_devices(&endpoint).ok();
                            if proxy
                                .send_event(UserEvent::Snapshot(snapshot, controls, devices))
                                .is_err()
                            {
                                break;
                            }
                            std::thread::sleep(Duration::from_secs(1));
                        }
                    });
                }
            }
            Event::UserEvent(UserEvent::Snapshot(snapshot, controls, devices)) => {
                let connected = snapshot.is_some();
                if let Some(view) = view.as_mut()
                    && let Err(error) = view.sync_virtual_display(
                        controls
                            .as_ref()
                            .is_some_and(|controls| controls.virtual_display_enabled),
                        &control_endpoint,
                    )
                {
                    view.status
                        .set_text(format!("Could not open virtual display: {error}"));
                }
                if let Some(snapshot) = snapshot {
                    let snapshot = *snapshot;
                    let retention = controls.as_ref().map_or(48.0 * 3600.0, |controls| {
                        controls.recent_session_retention_seconds
                    });
                    let state = TrayState::from_snapshot_with_retention(&snapshot, retention);
                    if last_state.as_ref() != Some(&state)
                        && let Some(view) = &mut view
                    {
                        let _ = view.show_snapshot(&state);
                    }
                    last_state = Some(state);
                    if let Some(view) = &mut view {
                        view.show_battery_preview(
                            controls.as_ref().map(|state| state.battery_power_preview),
                        );
                        view.show_brightness(controls.as_ref().and_then(|state| state.brightness));
                        view.show_display_mode(
                            controls
                                .as_ref()
                                .and_then(|state| state.display_mode.as_deref()),
                        );
                        view.show_transcript_monitoring(
                            controls
                                .as_ref()
                                .map(|state| (state.codex_transcripts, state.claude_transcripts)),
                        );
                        #[cfg(target_os = "macos")]
                        view.show_sleep_policy(
                            controls
                                .as_ref()
                                .and_then(|state| state.sleep_policy.as_deref()),
                        );
                    }
                    if last_devices != devices {
                        if let Some((ref entries, ref active)) = devices
                            && let Some(view) = &mut view
                        {
                            let _ = view.show_devices(entries, active.as_deref());
                        }
                        last_devices = devices;
                    }
                } else if last_connected != Some(false) {
                    if let Some(view) = &mut view {
                        let _ = view.show_disconnected();
                    }
                    last_state = None;
                    last_devices = None;
                }
                last_connected = Some(connected);
            }
            Event::UserEvent(UserEvent::Menu(event))
                if view
                    .as_ref()
                    .is_some_and(|view| event.id == *view.quit.id()) =>
            {
                view.take();
                *flow = ControlFlow::Exit;
            }
            Event::UserEvent(UserEvent::Menu(event)) => {
                if let Some(view) = &mut view
                    && event.id == *view.settings_item.id()
                    && let Err(error) = view.open_settings(&control_endpoint)
                {
                    view.status
                        .set_text(format!("Could not open settings: {error}"));
                }
                if let Some(enabled) = view
                    .as_ref()
                    .and_then(|view| view.battery_preview_for_menu_event(&event))
                {
                    let endpoint = control_endpoint.clone();
                    std::thread::spawn(move || {
                        let _ = send_battery_preview(&endpoint, enabled);
                    });
                }
                if let Some((provider, enabled)) = view
                    .as_ref()
                    .and_then(|view| view.transcript_for_menu_event(&event))
                {
                    let endpoint = control_endpoint.clone();
                    std::thread::spawn(move || {
                        let _ = send_transcript_monitoring(&endpoint, provider, enabled);
                    });
                }
                #[cfg(target_os = "macos")]
                if let Some(policy) = view
                    .as_ref()
                    .and_then(|view| view.sleep_policy_for_menu_event(&event))
                {
                    let endpoint = control_endpoint.clone();
                    std::thread::spawn(move || {
                        let _ = send_sleep_policy(&endpoint, policy);
                    });
                }
                if let Some(brightness) = view
                    .as_ref()
                    .and_then(|view| view.brightness_for_menu_event(&event))
                {
                    let endpoint = control_endpoint.clone();
                    std::thread::spawn(move || {
                        let _ = send_brightness(&endpoint, brightness);
                    });
                } else if let Some(mode) = view
                    .as_ref()
                    .and_then(|view| view.display_for_menu_event(&event))
                {
                    let endpoint = control_endpoint.clone();
                    std::thread::spawn(move || {
                        let _ = send_display_mode(&endpoint, mode);
                    });
                } else if let Some(root) = view
                    .as_ref()
                    .and_then(|view| view.device_for_menu_event(&event))
                {
                    let endpoint = control_endpoint.clone();
                    let root = root.to_owned();
                    std::thread::spawn(move || {
                        let _ = send_device_selection(&endpoint, &root);
                    });
                }
            }
            _ => {}
        }
    });
}

#[cfg(target_os = "linux")]
fn run(endpoint: String) -> Result<(), Box<dyn Error>> {
    let mut view = TrayView::new()?;
    let mut last_state: Option<TrayState> = None;
    let mut connected = true;
    let mut last_devices: Option<(Vec<DeviceInfo>, Option<String>)> = None;
    loop {
        match fetch_snapshot(&endpoint) {
            Ok(snapshot) => {
                let controls = fetch_controls(&endpoint).ok();
                let retention = controls.as_ref().map_or(48.0 * 3600.0, |controls| {
                    controls.recent_session_retention_seconds
                });
                view.sync_virtual_display(
                    controls
                        .as_ref()
                        .is_some_and(|controls| controls.virtual_display_enabled),
                    &endpoint,
                )?;
                let state = TrayState::from_snapshot_with_retention(&snapshot, retention);
                if last_state.as_ref() != Some(&state) {
                    view.show_snapshot(&state)?;
                }
                view.show_battery_preview(
                    controls.as_ref().map(|state| state.battery_power_preview),
                );
                view.show_brightness(controls.as_ref().and_then(|state| state.brightness));
                view.show_display_mode(
                    controls
                        .as_ref()
                        .and_then(|state| state.display_mode.as_deref()),
                );
                view.show_transcript_monitoring(
                    controls
                        .as_ref()
                        .map(|state| (state.codex_transcripts, state.claude_transcripts)),
                );
                let devices = fetch_devices(&endpoint).ok();
                if last_devices != devices {
                    if let Some((ref entries, ref active)) = devices {
                        view.show_devices(entries, active.as_deref())?;
                    }
                    last_devices = devices;
                }
                last_state = Some(state);
                connected = true;
            }
            Err(_) if connected => {
                view.show_disconnected()?;
                last_state = None;
                last_devices = None;
                connected = false;
            }
            Err(_) => {}
        }
        if let Ok(event) = MenuEvent::receiver().try_recv() {
            if event.id == *view.quit.id() {
                break;
            }
            if event.id == *view.settings_item.id() {
                if let Err(error) = view.open_settings(&endpoint) {
                    view.status
                        .set_text(format!("Could not open settings: {error}"));
                }
                continue;
            }
            if let Some(brightness) = view.brightness_for_menu_event(&event) {
                let _ = send_brightness(&endpoint, brightness);
            } else if let Some(enabled) = view.battery_preview_for_menu_event(&event) {
                let _ = send_battery_preview(&endpoint, enabled);
            } else if let Some((provider, enabled)) = view.transcript_for_menu_event(&event) {
                let _ = send_transcript_monitoring(&endpoint, provider, enabled);
            } else if let Some(mode) = view.display_for_menu_event(&event) {
                let _ = send_display_mode(&endpoint, mode);
            } else if let Some(root) = view.device_for_menu_event(&event) {
                let _ = send_device_selection(&endpoint, root);
            }
        }
        std::thread::sleep(Duration::from_secs(1));
    }
    Ok(())
}

fn main() -> Result<(), Box<dyn Error>> {
    let endpoint = env::args()
        .nth(1)
        .or_else(|| env::var("SIDEPULSE_NEXT_ENDPOINT").ok())
        .or_else(|| {
            let executable = env::current_exe().ok()?;
            std::fs::read_to_string(
                executable
                    .parent()?
                    .parent()?
                    .join("Resources/endpoint.txt"),
            )
            .ok()
        })
        .ok_or("usage: sidepulse-next-tray ENDPOINT")?;
    run(endpoint)
}
