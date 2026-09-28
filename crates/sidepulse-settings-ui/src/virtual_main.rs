//! Virtual status window. The service supplies all LED program execution.

use std::sync::mpsc::{self, Receiver};
use std::time::Duration;

use eframe::egui;
use sidepulse_core::{
    ClientRequest, PROTOCOL_VERSION, RequestKind, ServerMessage, ServerPayload, VirtualDisplayFrame,
};

#[cfg(target_os = "macos")]
mod virtual_macos;

struct VirtualApp {
    frames: Receiver<Option<VirtualDisplayFrame>>,
    frame: Option<VirtualDisplayFrame>,
    visible: bool,
}

impl VirtualApp {
    fn new(endpoint: String, context: &eframe::CreationContext<'_>) -> Self {
        #[cfg(target_os = "macos")]
        virtual_macos::configure_window(context);
        let (sent, frames) = mpsc::sync_channel(1);
        let repaint = context.egui_ctx.clone();
        std::thread::spawn(move || {
            loop {
                let request = ClientRequest {
                    version: PROTOCOL_VERSION,
                    request_id: 1,
                    kind: RequestKind::VirtualDisplay,
                };
                let frame = sidepulse_ipc::request::<_, ServerMessage>(
                    &endpoint,
                    &request,
                    Duration::from_millis(500),
                )
                .ok()
                .and_then(|reply| {
                    if reply.version == PROTOCOL_VERSION
                        && reply.request_id == Some(1)
                        && let ServerPayload::VirtualDisplay { frame } = reply.payload
                    {
                        Some(frame)
                    } else {
                        None
                    }
                });
                let delay = if frame.as_ref().is_some_and(|frame| frame.enabled) {
                    33
                } else {
                    1000
                };
                match sent.try_send(frame) {
                    Ok(()) | Err(mpsc::TrySendError::Full(_)) => repaint.request_repaint(),
                    Err(mpsc::TrySendError::Disconnected(_)) => break,
                }
                std::thread::sleep(Duration::from_millis(delay));
            }
        });
        Self {
            frames,
            frame: None,
            visible: false,
        }
    }
}

impl eframe::App for VirtualApp {
    fn clear_color(&self, _: &egui::Visuals) -> [f32; 4] {
        [0.0; 4]
    }

    fn ui(&mut self, ui: &mut egui::Ui, _frame: &mut eframe::Frame) {
        while let Ok(frame) = self.frames.try_recv() {
            self.frame = frame;
        }
        let visible = self.frame.as_ref().is_some_and(|frame| frame.enabled);
        if self.visible != visible {
            ui.ctx()
                .send_viewport_cmd(egui::ViewportCommand::Visible(visible));
            self.visible = visible;
        }
        if !visible {
            return;
        }
        #[cfg(not(target_os = "macos"))]
        {
            ui.label("SidePulse status");
            ui.weak("Disable the virtual display in Settings → Devices to hide this window.");
            ui.add_space(8.0);
        }
        let rect = if cfg!(target_os = "macos") {
            ui.max_rect()
        } else {
            ui.available_rect_before_wrap()
        };
        let painter = ui.painter();
        #[cfg(target_os = "macos")]
        let led_rect = {
            painter.rect_filled(
                rect,
                egui::CornerRadius {
                    nw: 0,
                    ne: 0,
                    sw: 8,
                    se: 8,
                },
                egui::Color32::BLACK,
            );
            egui::Rect::from_min_max(
                egui::pos2(rect.left(), rect.bottom() - 5.0),
                rect.right_bottom(),
            )
        };
        #[cfg(not(target_os = "macos"))]
        let led_rect = egui::Rect::from_min_size(rect.min, egui::vec2(rect.width(), 24.0));
        let pixels = &self.frame.as_ref().unwrap().pixels;
        if pixels.is_empty() {
            return;
        }
        let led_width = led_rect.width() / pixels.len() as f32;
        // Spatial blending and tone mapping are drawing, not animation policy.
        let columns = (led_rect.width() / 4.0).ceil() as usize;
        for column in 0..columns {
            let x = column as f32 * 4.0;
            let mut rgb = [0.0_f32; 3];
            for (index, pixel) in pixels.iter().enumerate() {
                let phase = ((x + 2.0 - (index as f32 + 0.5) * led_width).abs()
                    / (led_width * 1.5))
                    .min(1.0);
                let weight = 0.5 + 0.5 * (std::f32::consts::PI * phase).cos();
                for channel in 0..3 {
                    rgb[channel] += f32::from(pixel[channel]) / 255.0 * weight;
                }
            }
            let mapped =
                rgb.map(|value| (value.clamp(0.0, 1.0).powf(0.86) * 1.22 * 255.0).min(255.0) as u8);
            let column_rect = egui::Rect::from_min_max(
                egui::pos2(led_rect.left() + x, led_rect.top()),
                egui::pos2(
                    (led_rect.left() + x + 4.0).min(led_rect.right()),
                    led_rect.bottom(),
                ),
            );
            painter.rect_filled(
                column_rect,
                0,
                egui::Color32::from_rgb(mapped[0], mapped[1], mapped[2]),
            );
        }
        ui.ctx().request_repaint_after(Duration::from_millis(33));
    }
}

fn main() -> eframe::Result {
    let mut args = std::env::args().skip(1);
    let endpoint = match (args.next(), args.next()) {
        (Some(endpoint), None) => Some(endpoint),
        (None, None) => std::env::var("SIDEPULSE_NEXT_ENDPOINT").ok().or_else(|| {
            let executable = std::env::current_exe().ok()?;
            std::fs::read_to_string(
                executable
                    .parent()?
                    .parent()?
                    .join("Resources/endpoint.txt"),
            )
            .ok()
        }),
        _ => None,
    };
    let Some(endpoint) = endpoint.filter(|endpoint| !endpoint.trim().is_empty()) else {
        eprintln!("usage: sidepulse-next-virtual ENDPOINT");
        std::process::exit(2);
    };
    use std::hash::{Hash, Hasher};
    let mut hasher = std::collections::hash_map::DefaultHasher::new();
    endpoint.hash(&mut hasher);
    let lock_path = std::env::temp_dir().join(format!(
        "sidepulse-next-virtual-{:016x}.lock",
        hasher.finish()
    ));
    let instance_lock = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(lock_path)
        .map_err(|error| eframe::Error::AppCreation(Box::new(error)))?;
    if instance_lock.try_lock().is_err() {
        return Ok(());
    }
    let viewport = egui::ViewportBuilder::default()
        .with_visible(false)
        .with_inner_size([400.0, 120.0]);
    #[cfg(target_os = "macos")]
    let viewport = viewport
        .with_inner_size([220.0, 37.0])
        .with_decorations(false)
        .with_transparent(true)
        .with_resizable(false)
        .with_always_on_top()
        .with_mouse_passthrough(true)
        .with_taskbar(false);
    eframe::run_native(
        "SidePulse virtual display",
        eframe::NativeOptions {
            viewport,
            ..Default::default()
        },
        Box::new(move |context| Ok(Box::new(VirtualApp::new(endpoint, context)))),
    )
}
