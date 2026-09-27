//! Development tray client: renders service snapshots without owning agent state.

use std::env;
use std::error::Error;
use std::time::Duration;

use sidepulse_core::{
    ClientRequest, MonitorSnapshot, PROTOCOL_VERSION, RequestKind, ServerMessage, ServerPayload,
};
use sidepulse_ui_model::{
    BRIGHTNESS_CHOICES, DISPLAY_CHOICES, StatusIcon, TrayState, brightness_label,
};
use tray_icon::menu::{Menu, MenuEvent, MenuItem, PredefinedMenuItem};
use tray_icon::{Icon, TrayIcon, TrayIconBuilder};

struct TrayView {
    tray: TrayIcon,
    menu: Menu,
    status: MenuItem,
    brightness_status: MenuItem,
    brightness_items: Vec<(MenuItem, u8)>,
    display_status: MenuItem,
    display_items: Vec<(MenuItem, &'static str)>,
    quit: MenuItem,
    visible_rows: usize,
}

impl TrayView {
    fn new() -> Result<Self, Box<dyn Error>> {
        let menu = Menu::new();
        let status = MenuItem::new("Connecting to SidePulse…", false, None);
        let separator = PredefinedMenuItem::separator();
        let brightness_status = MenuItem::new("Device brightness unavailable", false, None);
        let brightness_items = BRIGHTNESS_CHOICES
            .iter()
            .map(|choice| (MenuItem::new(choice.label, false, None), choice.value))
            .collect::<Vec<_>>();
        let display_status = MenuItem::new("Device display unavailable", false, None);
        let display_items =
            DISPLAY_CHOICES.map(|choice| (MenuItem::new(choice.label, false, None), choice.value));
        let controls_separator = PredefinedMenuItem::separator();
        let quit = MenuItem::new("Quit SidePulse tray", true, None);
        menu.append_items(&[&status, &separator, &brightness_status])?;
        for (item, _) in &brightness_items {
            menu.append(item)?;
        }
        menu.append(&display_status)?;
        for (item, _) in &display_items {
            menu.append(item)?;
        }
        menu.append_items(&[&controls_separator, &quit])?;
        let tray = TrayIconBuilder::new()
            .with_menu(Box::new(menu.clone()))
            .with_icon(icon(StatusIcon::Unknown)?)
            .with_tooltip("SidePulse: connecting")
            .build()?;
        Ok(Self {
            tray,
            menu,
            status,
            brightness_status,
            brightness_items,
            display_status,
            display_items: display_items.into(),
            quit,
            visible_rows: 0,
        })
    }

    fn show_snapshot(&mut self, snapshot: &MonitorSnapshot) -> Result<(), Box<dyn Error>> {
        let state = TrayState::from_snapshot(snapshot);
        self.status.set_text(&state.tooltip);
        self.tray.set_tooltip(Some(&state.tooltip))?;
        self.tray.set_icon(Some(icon(state.icon)?))?;
        self.tray.set_title(Some(&state.title));
        for _ in 0..self.visible_rows {
            self.menu.remove_at(1);
        }
        self.visible_rows = 0;
        for row in state.rows.iter().take(12) {
            let item = MenuItem::new(format!("{} — {}", row.title, row.subtitle), false, None);
            self.menu.insert(&item, 1 + self.visible_rows)?;
            self.visible_rows += 1;
        }
        Ok(())
    }

    fn show_disconnected(&mut self) -> Result<(), Box<dyn Error>> {
        self.status.set_text("SidePulse service unavailable");
        self.tray
            .set_tooltip(Some("SidePulse service unavailable"))?;
        self.tray.set_icon(Some(icon(StatusIcon::Unknown)?))?;
        for _ in 0..self.visible_rows {
            self.menu.remove_at(1);
        }
        self.visible_rows = 0;
        self.show_brightness(None);
        self.show_display_mode(None);
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

fn fetch_brightness(endpoint: &str) -> Result<Option<u8>, Box<dyn Error>> {
    let request = ClientRequest {
        version: PROTOCOL_VERSION,
        request_id: 2,
        kind: RequestKind::Settings,
    };
    let response: ServerMessage =
        sidepulse_ipc::request(endpoint, &request, Duration::from_secs(2))?;
    match response.payload {
        ServerPayload::Settings {
            active_device: Some(_),
            brightness,
            ..
        } => Ok(brightness),
        ServerPayload::Error { .. } => Ok(None),
        _ => Err("service did not return settings".into()),
    }
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

fn fetch_display_mode(endpoint: &str) -> Result<Option<String>, Box<dyn Error>> {
    let request = ClientRequest {
        version: PROTOCOL_VERSION,
        request_id: 4,
        kind: RequestKind::Settings,
    };
    let response: ServerMessage =
        sidepulse_ipc::request(endpoint, &request, Duration::from_secs(2))?;
    match response.payload {
        ServerPayload::Settings {
            active_device: Some(_),
            display_mode,
            ..
        } => Ok(display_mode),
        ServerPayload::Error { .. } => Ok(None),
        _ => Err("service did not return settings".into()),
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

#[cfg(any(target_os = "macos", target_os = "windows"))]
fn run(endpoint: String) -> Result<(), Box<dyn Error>> {
    use tao::event::{Event, StartCause};
    use tao::event_loop::{ControlFlow, EventLoopBuilder};

    enum UserEvent {
        Snapshot(Option<Box<MonitorSnapshot>>, Option<u8>, Option<String>),
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
                            let brightness = fetch_brightness(&endpoint).ok().flatten();
                            let display_mode = fetch_display_mode(&endpoint).ok().flatten();
                            if proxy
                                .send_event(UserEvent::Snapshot(snapshot, brightness, display_mode))
                                .is_err()
                            {
                                break;
                            }
                            std::thread::sleep(Duration::from_secs(1));
                        }
                    });
                }
            }
            Event::UserEvent(UserEvent::Snapshot(snapshot, brightness, display_mode)) => {
                let connected = snapshot.is_some();
                if let Some(snapshot) = snapshot {
                    let snapshot = *snapshot;
                    let state = TrayState::from_snapshot(&snapshot);
                    if last_state.as_ref() != Some(&state)
                        && let Some(view) = &mut view
                    {
                        let _ = view.show_snapshot(&snapshot);
                    }
                    last_state = Some(state);
                    if let Some(view) = &view {
                        view.show_brightness(brightness);
                        view.show_display_mode(display_mode.as_deref());
                    }
                } else if last_connected != Some(false) {
                    if let Some(view) = &mut view {
                        let _ = view.show_disconnected();
                    }
                    last_state = None;
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
    loop {
        match fetch_snapshot(&endpoint) {
            Ok(snapshot) => {
                let state = TrayState::from_snapshot(&snapshot);
                if last_state.as_ref() != Some(&state) {
                    view.show_snapshot(&snapshot)?;
                }
                view.show_brightness(fetch_brightness(&endpoint).ok().flatten());
                view.show_display_mode(fetch_display_mode(&endpoint).ok().flatten().as_deref());
                last_state = Some(state);
                connected = true;
            }
            Err(_) if connected => {
                view.show_disconnected()?;
                last_state = None;
                connected = false;
            }
            Err(_) => {}
        }
        if let Ok(event) = MenuEvent::receiver().try_recv() {
            if event.id == *view.quit.id() {
                break;
            }
            if let Some(brightness) = view.brightness_for_menu_event(&event) {
                let _ = send_brightness(&endpoint, brightness);
            } else if let Some(mode) = view.display_for_menu_event(&event) {
                let _ = send_display_mode(&endpoint, mode);
            }
        }
        std::thread::sleep(Duration::from_secs(1));
    }
    Ok(())
}

fn main() -> Result<(), Box<dyn Error>> {
    let endpoint = env::args()
        .nth(1)
        .ok_or("usage: sidepulse-next-tray ENDPOINT")?;
    run(endpoint)
}
