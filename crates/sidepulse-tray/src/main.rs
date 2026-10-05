//! Development tray client: renders service snapshots without owning agent state.

use std::env;
use std::error::Error;
use std::time::Duration;

use sidepulse_core::{
    ClientRequest, DeviceInfo, MonitorSnapshot, PROTOCOL_VERSION, PhoneLinkSummary, RequestKind,
    ServerMessage, ServerPayload,
};
#[cfg(not(target_os = "macos"))]
use sidepulse_ui_model::BRIGHTNESS_CHOICES;
#[cfg(target_os = "macos")]
use sidepulse_ui_model::SLEEP_CHOICES;
use sidepulse_ui_model::{
    DISPLAY_CHOICES, StatusIcon, TrayControls, TrayDevice, TrayState, tray_devices,
};
use tray_icon::menu::accelerator::{Accelerator, Code, Modifiers};
use tray_icon::menu::{
    CheckMenuItem, IconMenuItem, Menu, MenuEvent, MenuId, MenuItem, PredefinedMenuItem, Submenu,
};
use tray_icon::{Icon, TrayIcon, TrayIconBuilder};

#[cfg(target_os = "macos")]
mod mac_menu;

struct TrayView {
    tray: TrayIcon,
    icon_visible: bool,
    actions: Vec<(MenuId, TrayAction)>,
    settings_child: Option<std::process::Child>,
    virtual_child: Option<std::process::Child>,
    virtual_launch_attempted: bool,
    #[cfg(target_os = "macos")]
    brightness_target: objc2::rc::Retained<mac_menu::BrightnessTarget>,
    #[cfg(target_os = "macos")]
    animation_target: objc2::rc::Retained<mac_menu::AnimationTarget>,
}

#[derive(Clone)]
enum TrayAction {
    Session(String),
    DeviceMode(String, &'static str),
    #[cfg(not(target_os = "macos"))]
    DeviceBrightness(String, u8),
    RemoveDevice(String),
    RemovePhone(String, String),
    ToggleVirtual(bool),
    #[cfg(target_os = "macos")]
    Sleep(&'static str),
    Setup,
    Settings,
    Quit,
}

impl TrayView {
    fn new() -> Result<Self, Box<dyn Error>> {
        let menu = Menu::new();
        menu.append(&MenuItem::new("SidePulse", false, None))?;
        let tray = TrayIconBuilder::new()
            .with_menu(Box::new(menu.clone()))
            .with_icon(icon(StatusIcon::Unknown)?)
            .with_icon_as_template(cfg!(target_os = "macos"))
            .with_tooltip("SidePulse Agent Monitor: Idle")
            .build()?;
        let mut view = Self {
            tray,
            icon_visible: true,
            actions: Vec::new(),
            settings_child: None,
            virtual_child: None,
            virtual_launch_attempted: false,
            #[cfg(target_os = "macos")]
            brightness_target: mac_menu::BrightnessTarget::new(),
            #[cfg(target_os = "macos")]
            animation_target: mac_menu::AnimationTarget::new(),
        };
        view.show_disconnected()?;
        Ok(view)
    }

    fn show_visibility(&mut self, visible: bool) -> Result<(), Box<dyn Error>> {
        if self.icon_visible != visible {
            self.tray.set_visible(visible)?;
            self.icon_visible = visible;
        }
        Ok(())
    }

    fn show_snapshot(
        &mut self,
        state: &TrayState,
        controls: Option<&TrayControls>,
        devices: &[TrayDevice],
    ) -> Result<(), Box<dyn Error>> {
        let label = match state.icon {
            StatusIcon::Working | StatusIcon::Tool | StatusIcon::LongTask => "Working",
            StatusIcon::Waiting | StatusIcon::Error => "Ask",
            StatusIcon::Completed => "Done",
            _ => "Idle",
        };
        self.tray
            .set_tooltip(Some(format!("SidePulse Agent Monitor: {label}")))?;
        self.tray.set_icon(Some(icon(state.icon)?))?;
        #[cfg(target_os = "macos")]
        mac_menu::set_status_symbol(&self.tray, state.icon);
        self.tray.set_title(None::<&str>);
        let menu = Menu::new();
        let mut actions = Vec::new();
        menu.append(&MenuItem::new("SidePulse", false, None))?;
        menu.append(&PredefinedMenuItem::separator())?;
        menu.append(&MenuItem::new("Agents", false, None))?;
        let mut rows = state.rows.iter().collect::<Vec<_>>();
        rows.truncate(10);
        if rows.is_empty() {
            menu.append(&MenuItem::new("No recent sessions", false, None))?;
        }
        #[cfg(target_os = "macos")]
        let mut session_symbols = Vec::new();
        for row in rows {
            #[cfg(target_os = "macos")]
            session_symbols.push((menu.items().len(), row));
            let item = IconMenuItem::new(&row.title, row.can_open, None, None);
            actions.push((item.id().clone(), TrayAction::Session(row.id.clone())));
            menu.append(&item)?;
        }
        menu.append(&PredefinedMenuItem::separator())?;
        menu.append(&MenuItem::new("Devices", false, None))?;
        if devices.is_empty() {
            menu.append(&MenuItem::new("No devices", false, None))?;
        }
        for device in devices {
            self.append_device(&menu, device, &mut actions)?;
        }
        if controls.is_some_and(|controls| !controls.virtual_display_enabled) {
            let item = MenuItem::new("Add SidePulse Notch", true, None);
            actions.push((item.id().clone(), TrayAction::ToggleVirtual(true)));
            menu.append(&item)?;
        }
        #[cfg(target_os = "macos")]
        {
            menu.append(&PredefinedMenuItem::separator())?;
            menu.append(&MenuItem::new("Closed-Lid Sleep Prevention", false, None))?;
            for choice in SLEEP_CHOICES {
                let item = CheckMenuItem::new(
                    choice.label,
                    controls.is_some(),
                    controls.and_then(|c| c.sleep_policy.as_deref()) == Some(choice.value),
                    None,
                );
                actions.push((item.id().clone(), TrayAction::Sleep(choice.value)));
                menu.append(&item)?;
            }
        }
        menu.append(&PredefinedMenuItem::separator())?;
        let modifier = if cfg!(target_os = "macos") {
            Modifiers::META
        } else {
            Modifiers::CONTROL
        };
        for (label, action, key) in [
            ("Setup...", TrayAction::Setup, None),
            ("Settings...", TrayAction::Settings, Some(Code::Comma)),
            ("Quit", TrayAction::Quit, Some(Code::KeyQ)),
        ] {
            let item = MenuItem::new(label, true, key.map(|key| Accelerator::new(modifier, key)));
            actions.push((item.id().clone(), action));
            menu.append(&item)?;
        }
        #[cfg(target_os = "macos")]
        mac_menu::set_session_icons(&menu, &session_symbols);
        #[cfg(target_os = "macos")]
        self.animation_target.configure(
            &self.tray,
            &menu,
            &session_symbols,
            state.icon,
            controls.is_none_or(|controls| controls.visible),
        );
        #[cfg(all(target_os = "macos", debug_assertions))]
        if std::env::var_os("SIDEPULSE_TRAY_DEBUG_MENU").is_some() {
            mac_menu::dump_menu(&menu);
        }
        self.tray.set_menu(Some(Box::new(menu)));
        self.actions = actions;
        Ok(())
    }

    fn append_device(
        &self,
        menu: &Menu,
        device: &TrayDevice,
        actions: &mut Vec<(MenuId, TrayAction)>,
    ) -> Result<(), Box<dyn Error>> {
        let submenu = Submenu::new(
            if device.connected && !cfg!(target_os = "macos") {
                format!("✓ {}", device.name)
            } else {
                device.name.clone()
            },
            true,
        );
        for choice in DISPLAY_CHOICES {
            if choice.value == "battery" && device.linked_phone {
                continue;
            }
            let item = CheckMenuItem::new(choice.label, true, device.display == choice.value, None);
            actions.push((
                item.id().clone(),
                TrayAction::DeviceMode(device.path.clone(), choice.value),
            ));
            submenu.append(&item)?;
        }
        if !device.virtual_device && !device.linked_phone {
            submenu.append(&PredefinedMenuItem::separator())?;
            let percent = (u16::from(device.brightness) * 100 + 127) / 255;
            submenu.append(&MenuItem::new(
                format!("Brightness {percent}%"),
                false,
                None,
            ))?;
            #[cfg(not(target_os = "macos"))]
            for choice in BRIGHTNESS_CHOICES {
                let item =
                    CheckMenuItem::new(choice.label, true, device.brightness == choice.value, None);
                actions.push((
                    item.id().clone(),
                    TrayAction::DeviceBrightness(device.path.clone(), choice.value),
                ));
                submenu.append(&item)?;
            }
        }
        if device.virtual_device {
            submenu.append(&PredefinedMenuItem::separator())?;
            let item = MenuItem::new("Remove SidePulse Notch", true, None);
            actions.push((item.id().clone(), TrayAction::ToggleVirtual(false)));
            submenu.append(&item)?;
        }
        if let Some(id) = &device.phone_id {
            submenu.append(&PredefinedMenuItem::separator())?;
            submenu.append(&MenuItem::new("Linked iPhone", false, None))?;
            submenu.append(&MenuItem::new(format!("ID {id}"), false, None))?;
            if let Some(server) = &device.phone_server {
                submenu.append(&MenuItem::new(server, false, None))?;
            }
            #[cfg(target_os = "macos")]
            {
                let item = MenuItem::new("Remove iPhone...", true, None);
                actions.push((
                    item.id().clone(),
                    TrayAction::RemovePhone(id.clone(), device.name.clone()),
                ));
                submenu.append(&item)?;
            }
            #[cfg(not(target_os = "macos"))]
            {
                let confirmation = Submenu::new("Remove iPhone...", true);
                let item = MenuItem::new("Confirm Remove iPhone", true, None);
                actions.push((
                    item.id().clone(),
                    TrayAction::RemovePhone(id.clone(), device.name.clone()),
                ));
                confirmation.append(&item)?;
                submenu.append(&confirmation)?;
            }
        }
        if !device.connected {
            submenu.append(&PredefinedMenuItem::separator())?;
            submenu.append(&MenuItem::new("Not connected", false, None))?;
            let item = MenuItem::new("Remove", true, None);
            actions.push((
                item.id().clone(),
                TrayAction::RemoveDevice(device.path.clone()),
            ));
            submenu.append(&item)?;
        }
        menu.append(&submenu)?;
        #[cfg(target_os = "macos")]
        if device.connected {
            mac_menu::check_last_item(menu);
        }
        #[cfg(target_os = "macos")]
        if !device.virtual_device && !device.linked_phone {
            mac_menu::append_brightness_slider(menu, device, &self.brightness_target);
        }
        Ok(())
    }

    fn show_disconnected(&mut self) -> Result<(), Box<dyn Error>> {
        self.show_snapshot(&TrayState::disconnected(), None, &[])
    }

    fn action_for_menu_event(&self, event: &MenuEvent) -> Option<TrayAction> {
        self.actions
            .iter()
            .find(|(id, _)| *id == event.id)
            .map(|(_, action)| action.clone())
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

    fn open_settings(&mut self, endpoint: &str, setup: bool) -> Result<(), Box<dyn Error>> {
        if !setup
            && self
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
        let mut command = std::process::Command::new(executable);
        if setup {
            command.arg("--setup");
        }
        self.settings_child = Some(command.arg(endpoint).spawn()?);
        Ok(())
    }
}

impl Drop for TrayView {
    fn drop(&mut self) {
        #[cfg(target_os = "macos")]
        self.animation_target.stop();
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

fn send_brightness(endpoint: &str, root: &str, brightness: u8) -> Result<(), Box<dyn Error>> {
    let request = ClientRequest {
        version: PROTOCOL_VERSION,
        request_id: 3,
        kind: RequestKind::SetDeviceBrightness {
            root: root.to_owned(),
            brightness,
        },
    };
    let response: ServerMessage =
        sidepulse_ipc::request(endpoint, &request, Duration::from_secs(2))?;
    match response.payload {
        ServerPayload::Settings { .. } => Ok(()),
        ServerPayload::Error { message, .. } => Err(message.into()),
        _ => Err("service did not update brightness".into()),
    }
}

fn send_display_mode(endpoint: &str, root: &str, mode: &str) -> Result<(), Box<dyn Error>> {
    let request = ClientRequest {
        version: PROTOCOL_VERSION,
        request_id: 5,
        kind: RequestKind::SetDeviceDisplayMode {
            root: root.to_owned(),
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

fn fetch_phone_links(endpoint: &str) -> Result<Vec<PhoneLinkSummary>, Box<dyn Error>> {
    let request = ClientRequest {
        version: PROTOCOL_VERSION,
        request_id: 14,
        kind: RequestKind::PhoneLinks,
    };
    let response: ServerMessage =
        sidepulse_ipc::request(endpoint, &request, Duration::from_secs(2))?;
    match response.payload {
        ServerPayload::PhoneLinks { links, .. } => Ok(links),
        ServerPayload::Error { message, .. } => Err(message.into()),
        _ => Err("service did not return linked phones".into()),
    }
}

fn send_mutation(endpoint: &str, kind: RequestKind) -> Result<(), Box<dyn Error>> {
    let request = ClientRequest {
        version: PROTOCOL_VERSION,
        request_id: 13,
        kind,
    };
    let response: ServerMessage =
        sidepulse_ipc::request(endpoint, &request, Duration::from_secs(2))?;
    match response.payload {
        ServerPayload::Settings { .. }
        | ServerPayload::Devices { .. }
        | ServerPayload::PhoneLinks { .. }
        | ServerPayload::Ack => Ok(()),
        ServerPayload::Error { message, .. } => Err(message.into()),
        _ => Err("unexpected service response".into()),
    }
}

#[cfg(target_os = "macos")]
fn confirm_remove_phone(name: &str) -> bool {
    use objc2::MainThreadMarker;
    use objc2_app_kit::{NSAlert, NSAlertFirstButtonReturn};
    use objc2_foundation::NSString;
    let Some(marker) = MainThreadMarker::new() else {
        return false;
    };
    let alert = NSAlert::new(marker);
    alert.setMessageText(&NSString::from_str(&format!("Remove {name}?")));
    alert.setInformativeText(&NSString::from_str(
        "This Mac will stop sending SidePulse updates to this iPhone. You can link it again later.",
    ));
    alert.addButtonWithTitle(&NSString::from_str("Remove"));
    alert.addButtonWithTitle(&NSString::from_str("Cancel"));
    alert.runModal() == NSAlertFirstButtonReturn
}

#[cfg(not(target_os = "macos"))]
fn confirm_remove_phone(_name: &str) -> bool {
    true
}

fn execute_action(
    view: &mut TrayView,
    action: TrayAction,
    endpoint: &str,
) -> Result<bool, Box<dyn Error>> {
    match action {
        TrayAction::Session(agent_id) => {
            let endpoint = endpoint.to_owned();
            std::thread::spawn(move || {
                if let Err(error) = open_agent_session(&endpoint, agent_id) {
                    eprintln!("Could not open agent session: {error}");
                }
            });
        }
        TrayAction::DeviceMode(root, mode) => {
            send_display_mode(endpoint, &root, mode)?;
        }
        #[cfg(not(target_os = "macos"))]
        TrayAction::DeviceBrightness(root, brightness) => {
            send_brightness(endpoint, &root, brightness)?;
        }
        TrayAction::RemoveDevice(root) => {
            send_mutation(endpoint, RequestKind::RemoveRememberedDevice { root })?;
        }
        TrayAction::RemovePhone(id, name) => {
            if confirm_remove_phone(&name) {
                send_mutation(endpoint, RequestKind::RemovePhone { id })?;
            }
        }
        TrayAction::ToggleVirtual(enabled) => {
            send_mutation(
                endpoint,
                RequestKind::SetVirtualDisplay {
                    patch: sidepulse_core::VirtualDisplaySettingsPatch {
                        enabled: Some(enabled),
                        ..Default::default()
                    },
                },
            )?;
        }
        #[cfg(target_os = "macos")]
        TrayAction::Sleep(policy) => {
            send_sleep_policy(endpoint, policy)?;
        }
        TrayAction::Setup => {
            view.open_settings(endpoint, true)?;
        }
        TrayAction::Settings => {
            view.open_settings(endpoint, false)?;
        }
        TrayAction::Quit => return Ok(true),
    }
    Ok(false)
}

fn open_agent_session(endpoint: &str, agent_id: String) -> Result<(), String> {
    let request = ClientRequest {
        version: PROTOCOL_VERSION,
        request_id: 1,
        kind: RequestKind::SessionTargets {
            agent_id,
            action: None,
        },
    };
    let response: ServerMessage =
        sidepulse_ipc::request(endpoint, &request, Duration::from_secs(2))
            .map_err(|error| error.to_string())?;
    let ServerPayload::SessionTargets {
        options,
        selected,
        terminal,
        custom_terminal_path,
    } = response.payload
    else {
        return Err(match response.payload {
            ServerPayload::Error { message, .. } => message,
            _ => "The service did not return session actions.".into(),
        });
    };
    let option = options
        .iter()
        .find(|option| Some(option.action) == selected)
        .ok_or("No opener is available for this session.")?;
    sidepulse_platform::open_session(&option.target, &terminal, &custom_terminal_path)
        .map_err(|error| error.to_string())
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
            Vec<PhoneLinkSummary>,
        ),
        Menu(MenuEvent),
    }
    let event_loop = EventLoopBuilder::<UserEvent>::with_user_event().build();
    let proxy = event_loop.create_proxy();
    MenuEvent::set_event_handler(Some({
        let proxy = proxy.clone();
        move |event| {
            let _ = proxy.send_event(UserEvent::Menu(event));
        }
    }));
    let mut endpoint_for_worker = Some(endpoint.clone());
    let mut view: Option<TrayView> = None;
    let mut last_presentation: Option<(TrayState, Option<TrayControls>, Vec<TrayDevice>)> = None;
    event_loop.run(move |event, _, flow| {
        *flow = ControlFlow::Wait;
        match event {
            Event::NewEvents(StartCause::Init) => {
                view = Some(TrayView::new().expect("create SidePulse tray"));
                if let Some(endpoint) = endpoint_for_worker.take() {
                    let proxy = proxy.clone();
                    std::thread::spawn(move || {
                        loop {
                            let snapshot = fetch_snapshot(&endpoint).ok().map(Box::new);
                            let controls = fetch_controls(&endpoint).ok();
                            let devices = fetch_devices(&endpoint).ok();
                            let links = fetch_phone_links(&endpoint).unwrap_or_default();
                            if proxy
                                .send_event(UserEvent::Snapshot(snapshot, controls, devices, links))
                                .is_err()
                            {
                                break;
                            }
                            std::thread::sleep(Duration::from_secs(1));
                        }
                    });
                }
            }
            Event::UserEvent(UserEvent::Snapshot(snapshot, controls, discovered, links)) => {
                let Some(view) = view.as_mut() else {
                    return;
                };
                if let Err(error) = view.sync_virtual_display(
                    controls.as_ref().is_some_and(|c| c.virtual_display_enabled),
                    &endpoint,
                ) {
                    eprintln!("Could not open virtual display: {error}");
                }
                let state = snapshot
                    .as_deref()
                    .map_or_else(TrayState::disconnected, |snapshot| {
                        TrayState::from_snapshot_with_retention(
                            snapshot,
                            controls
                                .as_ref()
                                .map_or(48.0 * 3600.0, |c| c.recent_session_retention_seconds),
                        )
                    });
                let devices = controls.as_ref().map_or_else(Vec::new, |controls| {
                    tray_devices(
                        discovered.as_ref().map_or(&[], |(devices, _)| devices),
                        &links,
                        controls,
                    )
                });
                let presentation = (state, controls, devices);
                if last_presentation.as_ref() != Some(&presentation) {
                    if let Err(error) = view.show_snapshot(
                        &presentation.0,
                        presentation.1.as_ref(),
                        &presentation.2,
                    ) {
                        eprintln!("Could not update tray: {error}");
                    }
                    last_presentation = Some(presentation.clone());
                }
                if let Some(controls) = presentation.1.as_ref() {
                    let _ = view.show_visibility(controls.visible);
                }
            }
            Event::UserEvent(UserEvent::Menu(event)) => {
                if let Some(view) = view.as_mut()
                    && let Some(action) = view.action_for_menu_event(&event)
                {
                    match execute_action(view, action, &endpoint) {
                        Ok(true) => {
                            *flow = ControlFlow::Exit;
                        }
                        Ok(false) => {
                            last_presentation = None;
                        }
                        Err(error) => {
                            eprintln!("Tray action failed: {error}");
                            let _ = view.tray.set_tooltip(Some(format!("SidePulse: {error}")));
                        }
                    }
                }
            }
            _ => {}
        }
    });
}

#[cfg(target_os = "linux")]
fn run(endpoint: String) -> Result<(), Box<dyn Error>> {
    let mut view = TrayView::new()?;
    let mut last_presentation: Option<(TrayState, Option<TrayControls>, Vec<TrayDevice>)> = None;
    loop {
        let snapshot = fetch_snapshot(&endpoint).ok();
        let controls = fetch_controls(&endpoint).ok();
        view.sync_virtual_display(
            controls.as_ref().is_some_and(|c| c.virtual_display_enabled),
            &endpoint,
        )?;
        let discovered = fetch_devices(&endpoint).ok();
        let links = fetch_phone_links(&endpoint).unwrap_or_default();
        let state = snapshot
            .as_ref()
            .map_or_else(TrayState::disconnected, |snapshot| {
                TrayState::from_snapshot_with_retention(
                    snapshot,
                    controls
                        .as_ref()
                        .map_or(48.0 * 3600.0, |c| c.recent_session_retention_seconds),
                )
            });
        let devices = controls.as_ref().map_or_else(Vec::new, |controls| {
            tray_devices(
                discovered.as_ref().map_or(&[], |(devices, _)| devices),
                &links,
                controls,
            )
        });
        let presentation = (state, controls, devices);
        if last_presentation.as_ref() != Some(&presentation) {
            view.show_snapshot(&presentation.0, presentation.1.as_ref(), &presentation.2)?;
            last_presentation = Some(presentation.clone());
        }
        if let Some(controls) = presentation.1.as_ref() {
            view.show_visibility(controls.visible)?;
        }
        if let Ok(event) = MenuEvent::receiver().try_recv()
            && let Some(action) = view.action_for_menu_event(&event)
        {
            if execute_action(&mut view, action, &endpoint)? {
                break;
            }
            last_presentation = None;
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
            sidepulse_installer::endpoint_from_executable(&executable)
                .ok()
                .flatten()
        })
        .ok_or("usage: sidepulse-next-tray ENDPOINT")?;
    #[cfg(target_os = "macos")]
    mac_menu::set_endpoint(&endpoint);
    run(endpoint)
}
