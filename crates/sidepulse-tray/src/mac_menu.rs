//! AppKit details that the portable menu API cannot represent.

use std::{
    cell::{Cell, RefCell},
    sync::OnceLock,
};

use objc2::{
    AnyThread, ClassType, DefinedClass, MainThreadMarker, Message, define_class, msg_send,
    rc::{Allocated, Retained},
    runtime::AnyObject,
    sel,
};
use objc2_app_kit::{
    NSAffineTransformNSAppKitAdditions, NSColor, NSCompositingOperation,
    NSEventTrackingRunLoopMode, NSGraphicsContext, NSImage, NSMenu, NSMenuItem,
    NSRectFillUsingOperation, NSSlider, NSSquareStatusItemLength, NSStatusBarButton, NSView,
    NSWorkspace,
};
use objc2_foundation::{
    NSAffineTransform, NSObject, NSObjectProtocol, NSPoint, NSRect, NSRunLoop,
    NSRunLoopCommonModes, NSSize, NSString, NSTimer,
};
use sidepulse_ui_model::{AgentRow, StatusIcon, TrayDevice};
use tray_icon::{
    TrayIcon,
    menu::{ContextMenu, Menu},
};

static ENDPOINT: OnceLock<String> = OnceLock::new();

pub fn set_endpoint(endpoint: &str) {
    let _ = ENDPOINT.set(endpoint.to_owned());
}

define_class!(
    #[unsafe(super = NSObject)]
    #[thread_kind = objc2::MainThreadOnly]
    pub struct BrightnessTarget;

    unsafe impl NSObjectProtocol for BrightnessTarget {}

    impl BrightnessTarget {
        #[unsafe(method(brightnessChanged:))]
        fn brightness_changed(&self, sender: &NSSlider) {
            let root: Option<Retained<NSString>> = unsafe { msg_send![sender, identifier] };
            let Some(root) = root else { return; };
            let Some(endpoint) = ENDPOINT.get() else { return; };
            let endpoint = endpoint.clone();
            let root = root.to_string();
            let brightness = sender.doubleValue().round().clamp(0.0, 255.0) as u8;
            std::thread::spawn(move || {
                if let Err(error) = super::send_brightness(&endpoint, &root, brightness) {
                    eprintln!("Could not set brightness: {error}");
                }
            });
        }
    }
);

impl BrightnessTarget {
    pub fn new() -> Retained<Self> {
        let _: MainThreadMarker =
            MainThreadMarker::new().expect("tray must be created on the main thread");
        unsafe { msg_send![Self::class(), new] }
    }
}

struct AnimatedRow {
    item: Retained<NSMenuItem>,
    icon: StatusIcon,
    origin: Option<Retained<NSImage>>,
}

pub struct AnimationIvars {
    frame: Cell<u32>,
    state: Cell<StatusIcon>,
    button: RefCell<Option<Retained<NSStatusBarButton>>>,
    rows: RefCell<Vec<AnimatedRow>>,
    timer: RefCell<Option<Retained<NSTimer>>>,
}

impl Default for AnimationIvars {
    fn default() -> Self {
        Self {
            frame: Cell::new(0),
            state: Cell::new(StatusIcon::Idle),
            button: RefCell::new(None),
            rows: RefCell::new(Vec::new()),
            timer: RefCell::new(None),
        }
    }
}

define_class!(
    #[unsafe(super = NSObject)]
    #[thread_kind = objc2::MainThreadOnly]
    #[ivars = AnimationIvars]
    pub struct AnimationTarget;

    unsafe impl NSObjectProtocol for AnimationTarget {}

    impl AnimationTarget {
        #[unsafe(method_id(init))]
        fn init(this: Allocated<Self>) -> Retained<Self> {
            let this = this.set_ivars(AnimationIvars::default());
            unsafe { msg_send![super(this), init] }
        }

        #[unsafe(method(animateStatusIcons:))]
        fn animate_status_icons(&self, _timer: &NSTimer) {
            let frame = self.ivars().frame.get().wrapping_add(1);
            self.ivars().frame.set(frame);
            #[cfg(debug_assertions)]
            if frame == 30 && std::env::var_os("SIDEPULSE_TRAY_DEBUG_ANIMATION").is_some() {
                eprintln!("SidePulse tray animation timer reached 30 frames");
            }
            let state = self.ivars().state.get();
            if animates(state)
                && let Some(button) = self.ivars().button.borrow().as_ref()
                && let Some(image) = animated_status_image(state, frame) {
                button.setImage(Some(&image));
            }
            for row in self.ivars().rows.borrow().iter() {
                if animates(row.icon)
                    && let Some(image) = session_icon_from_origin(row.icon, row.origin.as_deref(), Some(frame)) {
                    row.item.setImage(Some(&image));
                }
            }
        }
    }
);

impl AnimationTarget {
    pub fn new() -> Retained<Self> {
        let _: MainThreadMarker =
            MainThreadMarker::new().expect("tray must be created on the main thread");
        unsafe { msg_send![Self::class(), new] }
    }

    pub fn configure(
        &self,
        tray: &TrayIcon,
        menu: &Menu,
        rows: &[(usize, &AgentRow)],
        state: StatusIcon,
        visible: bool,
    ) {
        self.ivars().state.set(state);
        self.ivars().button.replace(
            tray.ns_status_item()
                .and_then(|item| MainThreadMarker::new().and_then(|mtm| item.button(mtm))),
        );
        let raw = menu.ns_menu();
        let animated_rows = if raw.is_null() {
            Vec::new()
        } else {
            // SAFETY: muda owns this menu while the tray retains it.
            let menu = unsafe { &*(raw.cast::<NSMenu>()) };
            rows.iter()
                .filter_map(|(index, row)| {
                    menu.itemAtIndex(*index as isize).map(|item| AnimatedRow {
                        item,
                        icon: row.icon,
                        origin: origin_icon(row),
                    })
                })
                .collect()
        };
        self.ivars().rows.replace(animated_rows);
        let animated = visible
            && !NSWorkspace::sharedWorkspace().accessibilityDisplayShouldReduceMotion()
            && (animates(state)
                || self
                    .ivars()
                    .rows
                    .borrow()
                    .iter()
                    .any(|row| animates(row.icon)));
        if animated && self.ivars().timer.borrow().is_none() {
            // SAFETY: selector is implemented above; the tray retains self until the timer is invalidated.
            let timer = unsafe {
                NSTimer::timerWithTimeInterval_target_selector_userInfo_repeats(
                    1.0 / 30.0,
                    self as &AnyObject,
                    sel!(animateStatusIcons:),
                    None,
                    true,
                )
            };
            timer.setTolerance(0.005);
            let run_loop = NSRunLoop::mainRunLoop();
            // SAFETY: AppKit's main run loop owns the timer in both modes.
            unsafe {
                run_loop.addTimer_forMode(&timer, NSRunLoopCommonModes);
                run_loop.addTimer_forMode(&timer, NSEventTrackingRunLoopMode);
            }
            self.ivars().timer.replace(Some(timer));
        } else if !animated {
            self.stop();
        }
    }

    pub fn stop(&self) {
        if let Some(timer) = self.ivars().timer.borrow_mut().take() {
            timer.invalidate();
        }
        self.ivars().rows.borrow_mut().clear();
        self.ivars().button.borrow_mut().take();
    }
}

pub fn append_brightness_slider(parent: &Menu, device: &TrayDevice, target: &BrightnessTarget) {
    let Some(mtm) = MainThreadMarker::new() else {
        return;
    };
    let raw = parent.ns_menu();
    if raw.is_null() {
        return;
    }
    // SAFETY: muda owns this NSMenu for the life of the Submenu, and this runs on AppKit's main thread.
    let parent = unsafe { &*(raw.cast::<NSMenu>()) };
    let Some(item) = parent.itemAtIndex(parent.numberOfItems() - 1) else {
        return;
    };
    let Some(menu) = item.submenu() else {
        return;
    };
    let item = NSMenuItem::new(mtm);
    item.setTitle(&NSString::from_str(""));
    let view = NSView::new(mtm);
    view.setFrame(NSRect::new(
        NSPoint::new(0.0, 0.0),
        NSSize::new(230.0, 34.0),
    ));
    let slider = NSSlider::new(mtm);
    slider.setFrame(NSRect::new(
        NSPoint::new(14.0, 6.0),
        NSSize::new(202.0, 22.0),
    ));
    slider.setMinValue(0.0);
    slider.setMaxValue(255.0);
    slider.setDoubleValue(f64::from(device.brightness));
    slider.setContinuous(false);
    let identifier = NSString::from_str(&device.path);
    let _: () = unsafe { msg_send![&*slider, setIdentifier: &*identifier] };
    // SAFETY: the TrayView retains `target` while the menu and slider exist.
    unsafe {
        slider.setTarget(Some(target as &AnyObject));
        slider.setAction(Some(sel!(brightnessChanged:)));
    }
    view.addSubview(&slider);
    item.setView(Some(&view));
    menu.insertItem_atIndex(&item, 5);
}

fn symbol(state: StatusIcon) -> (&'static str, &'static str) {
    match state {
        StatusIcon::Working | StatusIcon::Tool | StatusIcon::LongTask => {
            ("arrow.triangle.2.circlepath", "Working")
        }
        StatusIcon::Waiting | StatusIcon::Error => ("questionmark.circle", "Ask"),
        StatusIcon::Completed => ("checkmark.circle", "Done"),
        _ => ("circle", "Idle"),
    }
}

pub fn set_status_symbol(tray: &TrayIcon, state: StatusIcon) {
    let Some(mtm) = MainThreadMarker::new() else {
        return;
    };
    let Some(status) = tray.ns_status_item() else {
        return;
    };
    status.setLength(NSSquareStatusItemLength);
    let Some(button) = status.button(mtm) else {
        return;
    };
    let (name, description) = symbol(state);
    if let Some(image) = NSImage::imageWithSystemSymbolName_accessibilityDescription(
        &NSString::from_str(name),
        Some(&NSString::from_str(description)),
    ) {
        image.setTemplate(true);
        button.setImage(Some(&image));
        button.setTitle(&NSString::from_str(""));
        let label = NSString::from_str(&format!(
            "SidePulse Rust v{}: {description}",
            super::VERSION
        ));
        let _: () = unsafe { msg_send![&*button, setAccessibilityLabel: &*label] };
    }
}

fn animates(state: StatusIcon) -> bool {
    matches!(
        state,
        StatusIcon::Working
            | StatusIcon::Tool
            | StatusIcon::LongTask
            | StatusIcon::Waiting
            | StatusIcon::Error
    )
}

#[allow(deprecated)]
fn animated_status_image(state: StatusIcon, frame: u32) -> Option<Retained<NSImage>> {
    let (name, description) = symbol(state);
    let source = sf_symbol(name, description)?;
    let image = NSImage::initWithSize(NSImage::alloc(), NSSize::new(18.0, 18.0));
    image.lockFocus();
    NSGraphicsContext::saveGraphicsState_class();
    let phase = f64::from(frame % 48) / 48.0;
    let pulse = (1.0 + (2.0 * std::f64::consts::PI * phase).cos()) / 2.0;
    let working = matches!(
        state,
        StatusIcon::Working | StatusIcon::Tool | StatusIcon::LongTask
    );
    let scale = if working { 1.0 } else { 0.82 + 0.18 * pulse };
    let opacity = if working { 1.0 } else { 0.45 + 0.55 * pulse };
    let transform = NSAffineTransform::transform();
    transform.translateXBy_yBy(9.0, 9.0);
    if working {
        transform.rotateByDegrees(-360.0 * phase);
    }
    transform.scaleBy(scale);
    transform.concat();
    source.drawInRect_fromRect_operation_fraction(
        NSRect::new(NSPoint::new(-7.5, -7.5), NSSize::new(15.0, 15.0)),
        NSRect::new(NSPoint::new(0.0, 0.0), source.size()),
        NSCompositingOperation::SourceOver,
        opacity,
    );
    NSGraphicsContext::restoreGraphicsState_class();
    image.unlockFocus();
    image.setTemplate(true);
    Some(image)
}

pub fn set_session_icons(menu: &Menu, rows: &[(usize, &AgentRow)]) {
    let raw = menu.ns_menu();
    if raw.is_null() {
        return;
    }
    // SAFETY: muda owns this menu and the caller has it live on AppKit's main thread.
    let menu = unsafe { &*(raw.cast::<NSMenu>()) };
    for &(index, row) in rows {
        let Some(image) = session_row_icon(row) else {
            continue;
        };
        if let Some(item) = menu.itemAtIndex(index as isize) {
            item.setImage(Some(&image));
        }
    }
}

fn sf_symbol(name: &str, description: &str) -> Option<Retained<NSImage>> {
    NSImage::imageWithSystemSymbolName_accessibilityDescription(
        &NSString::from_str(name),
        Some(&NSString::from_str(description)),
    )
}

fn app_icon(paths: &[&str]) -> Option<Retained<NSImage>> {
    let path = paths
        .iter()
        .find(|path| std::path::Path::new(path).exists())?;
    Some(NSWorkspace::sharedWorkspace().iconForFile(&NSString::from_str(path)))
}

fn provider_icon(provider: &str) -> Option<Retained<NSImage>> {
    match provider.to_lowercase().as_str() {
        "codex" => app_icon(&["/Applications/Codex.app", "/Applications/ChatGPT.app"])
            .or_else(|| sf_symbol("sparkles", "Codex")),
        "claude" => app_icon(&["/Applications/Claude.app"])
            .or_else(|| sf_symbol("brain.head.profile", "Claude")),
        "grok" => {
            app_icon(&["/Applications/Grok.app"]).or_else(|| sf_symbol("bolt.circle", "Grok"))
        }
        "opencode" => app_icon(&["/Applications/OpenCode.app"])
            .or_else(|| sf_symbol("chevron.left.forwardslash.chevron.right", "OpenCode")),
        other => {
            let path = format!("/Applications/{other}.app");
            app_icon(&[&path]).or_else(|| sf_symbol("terminal", other))
        }
    }
}

fn host_icon(origin: Option<&str>) -> Option<Retained<NSImage>> {
    let origin = origin?.to_lowercase().replace('-', " ");
    if origin.contains("vs code")
        || origin.contains("vscode")
        || origin.contains("visual studio code")
    {
        app_icon(&[
            "/Applications/Visual Studio Code.app",
            "/Applications/Visual Studio Code - Insiders.app",
        ])
        .or_else(|| sf_symbol("chevron.left.forwardslash.chevron.right", "VS Code"))
    } else if origin.contains("cursor") {
        app_icon(&["/Applications/Cursor.app"]).or_else(|| sf_symbol("cursorarrow", "Cursor"))
    } else if origin.contains("windsurf") {
        app_icon(&["/Applications/Windsurf.app"]).or_else(|| sf_symbol("wind", "Windsurf"))
    } else if origin.contains("cli")
        || origin.contains("terminal")
        || origin.contains("command line")
    {
        app_icon(&[
            "/System/Applications/Utilities/Terminal.app",
            "/Applications/iTerm.app",
            "/Applications/iTerm2.app",
        ])
        .or_else(|| sf_symbol("terminal", "Terminal"))
    } else if origin.contains("transcript") {
        sf_symbol("doc.text", "Transcript")
    } else {
        None
    }
}

fn draw_image(image: &NSImage, rect: NSRect) {
    let size = image.size();
    image.drawInRect_fromRect_operation_fraction(
        rect,
        NSRect::new(NSPoint::new(0.0, 0.0), size),
        NSCompositingOperation::SourceOver,
        1.0,
    );
}

// Match the legacy AppKit image composition, including its fixed 18-point menu image.
#[allow(deprecated)]
fn composite_origin(host: &NSImage, provider: &NSImage) -> Retained<NSImage> {
    let image = NSImage::initWithSize(NSImage::alloc(), NSSize::new(24.0, 18.0));
    image.lockFocus();
    NSGraphicsContext::saveGraphicsState_class();
    draw_image(
        host,
        NSRect::new(NSPoint::new(0.0, 1.0), NSSize::new(15.5, 15.5)),
    );
    draw_image(
        provider,
        NSRect::new(NSPoint::new(8.0, 1.0), NSSize::new(15.5, 15.5)),
    );
    NSGraphicsContext::restoreGraphicsState_class();
    image.unlockFocus();
    image
}

#[allow(deprecated)]
fn session_row_icon(row: &AgentRow) -> Option<Retained<NSImage>> {
    session_icon_from_origin(row.icon, origin_icon(row).as_deref(), None)
}

fn origin_icon(row: &AgentRow) -> Option<Retained<NSImage>> {
    let provider = provider_icon(&row.provider);
    match (host_icon(row.origin.as_deref()), provider) {
        (Some(host), Some(provider)) => Some(composite_origin(&host, &provider)),
        (Some(host), None) => Some(host),
        (None, provider) => provider,
    }
}

#[allow(deprecated)]
fn session_icon_from_origin(
    state: StatusIcon,
    origin: Option<&NSImage>,
    frame: Option<u32>,
) -> Option<Retained<NSImage>> {
    let _mtm = MainThreadMarker::new()?;
    let (symbol, description) = symbol(state);
    let status = frame
        .filter(|_| animates(state))
        .and_then(|frame| animated_status_image(state, frame))
        .or_else(|| sf_symbol(symbol, description));
    let origin = origin.map(|image| image.retain());
    let (status, origin) = match (status, origin) {
        (Some(status), Some(origin)) => (status, origin),
        (status, origin) => return status.or(origin),
    };
    let right_width = origin.size().width;
    let width = 15.5 + 3.0 + right_width;
    let image = NSImage::initWithSize(NSImage::alloc(), NSSize::new(width, 18.0));
    image.lockFocus();
    NSGraphicsContext::saveGraphicsState_class();
    draw_image(
        &status,
        NSRect::new(NSPoint::new(0.0, 1.25), NSSize::new(15.5, 15.5)),
    );
    NSColor::labelColor().set();
    NSRectFillUsingOperation(
        NSRect::new(NSPoint::new(0.0, 0.0), NSSize::new(15.5, 18.0)),
        NSCompositingOperation::SourceIn,
    );
    draw_image(
        &origin,
        NSRect::new(NSPoint::new(18.5, 0.0), NSSize::new(right_width, 18.0)),
    );
    NSGraphicsContext::restoreGraphicsState_class();
    image.unlockFocus();
    Some(image)
}

pub fn check_last_item(menu: &Menu) {
    let raw = menu.ns_menu();
    if raw.is_null() {
        return;
    }
    // SAFETY: muda owns the menu on AppKit's main thread.
    let menu = unsafe { &*(raw.cast::<NSMenu>()) };
    let count = menu.numberOfItems();
    if count > 0
        && let Some(item) = menu.itemAtIndex(count - 1)
    {
        item.setState(1);
    }
}

#[cfg(debug_assertions)]
pub fn dump_menu(menu: &Menu) {
    let raw = menu.ns_menu();
    if raw.is_null() {
        return;
    }
    // SAFETY: the menu is live on AppKit's main thread.
    dump_native_menu(unsafe { &*(raw.cast::<NSMenu>()) }, 0);
}

#[cfg(debug_assertions)]
fn dump_native_menu(menu: &NSMenu, depth: usize) {
    for index in 0..menu.numberOfItems() {
        let Some(item) = menu.itemAtIndex(index) else {
            continue;
        };
        eprintln!(
            "{}{} | enabled={} | checked={} | view={} | image={}",
            "  ".repeat(depth),
            item.title(),
            item.isEnabled(),
            item.state() == 1,
            item.view().is_some(),
            item.image().is_some()
        );
        if let Some(child) = item.submenu() {
            dump_native_menu(&child, depth + 1);
        }
    }
}
