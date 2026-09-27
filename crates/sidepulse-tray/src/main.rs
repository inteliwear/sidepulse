//! Development tray client: renders service snapshots without owning agent state.

use std::env;
use std::error::Error;
use std::time::Duration;

use sidepulse_core::{
    ClientRequest, MonitorSnapshot, PROTOCOL_VERSION, RequestKind, ServerMessage, ServerPayload,
};
use sidepulse_ui_model::{StatusIcon, TrayState};
use tray_icon::menu::{Menu, MenuEvent, MenuItem, PredefinedMenuItem};
use tray_icon::{Icon, TrayIcon, TrayIconBuilder};

struct TrayView {
    tray: TrayIcon,
    menu: Menu,
    status: MenuItem,
    quit: MenuItem,
    visible_rows: usize,
}

impl TrayView {
    fn new() -> Result<Self, Box<dyn Error>> {
        let menu = Menu::new();
        let status = MenuItem::new("Connecting to SidePulse…", false, None);
        let separator = PredefinedMenuItem::separator();
        let quit = MenuItem::new("Quit SidePulse tray", true, None);
        menu.append_items(&[&status, &separator, &quit])?;
        let tray = TrayIconBuilder::new()
            .with_menu(Box::new(menu.clone()))
            .with_icon(icon(StatusIcon::Unknown)?)
            .with_tooltip("SidePulse: connecting")
            .build()?;
        Ok(Self {
            tray,
            menu,
            status,
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
        Ok(())
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

#[cfg(any(target_os = "macos", target_os = "windows"))]
fn run(endpoint: String) -> Result<(), Box<dyn Error>> {
    use tao::event::{Event, StartCause};
    use tao::event_loop::{ControlFlow, EventLoopBuilder};

    enum UserEvent {
        Snapshot(Option<Box<MonitorSnapshot>>),
        Menu(MenuEvent),
    }
    let event_loop = EventLoopBuilder::<UserEvent>::with_user_event().build();
    let proxy = event_loop.create_proxy();
    MenuEvent::set_event_handler(Some(move |event| {
        let _ = proxy.send_event(UserEvent::Menu(event));
    }));
    let mut worker_proxy = Some(event_loop.create_proxy());
    let mut worker_endpoint = Some(endpoint);
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
                            if proxy.send_event(UserEvent::Snapshot(snapshot)).is_err() {
                                break;
                            }
                            std::thread::sleep(Duration::from_secs(1));
                        }
                    });
                }
            }
            Event::UserEvent(UserEvent::Snapshot(snapshot)) => {
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
        if let Ok(event) = MenuEvent::receiver().try_recv()
            && event.id == *view.quit.id()
        {
            break;
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
