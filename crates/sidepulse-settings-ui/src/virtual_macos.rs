//! Native notch geometry and non-interactive window behavior.

use objc2::MainThreadMarker;
use objc2_app_kit::{NSColor, NSScreen, NSStatusWindowLevel, NSView, NSWindowCollectionBehavior};
use objc2_foundation::{NSPoint, NSRect, NSSize};
use raw_window_handle::{HasWindowHandle, RawWindowHandle};

pub fn configure_window(context: &eframe::CreationContext<'_>) {
    let Some(main_thread) = MainThreadMarker::new() else {
        return;
    };
    let Ok(handle) = context.window_handle() else {
        return;
    };
    let RawWindowHandle::AppKit(handle) = handle.as_raw() else {
        return;
    };
    // eframe keeps this NSView alive for the lifetime of its creation context;
    // the function is called only from the application's main thread.
    let view = unsafe { &*handle.ns_view.as_ptr().cast::<NSView>() };
    let Some(window) = view.window() else {
        return;
    };
    let screens = NSScreen::screens(main_thread);
    let screen = screens
        .iter()
        .find(|screen| screen.safeAreaInsets().top >= 1.0)
        .or_else(|| NSScreen::mainScreen(main_thread));
    let Some(screen) = screen else {
        return;
    };
    let frame = screen.frame();
    let left = screen.auxiliaryTopLeftArea();
    let right = screen.auxiliaryTopRightArea();
    let gap = right.origin.x - left.origin.x - left.size.width;
    let width = if gap >= 120.0 {
        gap.clamp(180.0, 320.0)
    } else {
        220.0
    };
    let height = screen.safeAreaInsets().top.max(0.0) + 5.0;
    window.setFrame_display(
        NSRect::new(
            NSPoint::new(
                frame.origin.x + (frame.size.width - width) / 2.0,
                frame.origin.y + frame.size.height - height,
            ),
            NSSize::new(width, height),
        ),
        false,
    );
    window.setLevel(NSStatusWindowLevel + 1);
    window.setCollectionBehavior(
        NSWindowCollectionBehavior::CanJoinAllSpaces
            | NSWindowCollectionBehavior::FullScreenAuxiliary
            | NSWindowCollectionBehavior::Stationary,
    );
    window.setIgnoresMouseEvents(true);
    window.setOpaque(false);
    window.setBackgroundColor(Some(&NSColor::clearColor()));
}
