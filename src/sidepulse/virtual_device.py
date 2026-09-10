from __future__ import annotations

import math
import re
import time

import objc
from AppKit import (
    NSBackingStoreBuffered,
    NSBezierPath,
    NSColor,
    NSGraphicsContext,
    NSScreen,
    NSView,
    NSWindow,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowStyleMaskBorderless,
)
from Foundation import NSObject, NSTimer
from Quartz import CGContextFillRect, CGContextSetRGBFillColor

from .led_status import LedDisplayState, normalize_brightness, program_for_display_state
from .led_wasm import LedWasmUnavailableError, SdLedWasmController


VIRTUAL_DEVICE_ID = "virtual:status-bar"
VIRTUAL_DEVICE_NAME = "SidePulse Notch"
LED_COUNT = 8
WINDOW_WIDTH = 220.0
FALLBACK_NOTCH_DEPTH = 32.0
LED_BAND_HEIGHT = 5.0
WINDOW_HEIGHT = FALLBACK_NOTCH_DEPTH + LED_BAND_HEIGHT
NOTCH_BOTTOM_RADIUS = 8.0
LED_BLEND_RADIUS_LEDS = 1.5
BLEND_COLUMN_WIDTH = 4.0
LED_GLOW_HEIGHT = 11.0
LED_CORE_BOOST = 1.22
LED_HOTLINE_BOOST = 1.46
LED_GAMMA = 0.86
FRAME_RATE = 60.0
FRAME_INTERVAL = 1.0 / FRAME_RATE
IDLE_FRAME_RATE = 12.0
IDLE_FRAME_INTERVAL = 1.0 / IDLE_FRAME_RATE


def monotonic_ms() -> int:
    return int(time.monotonic() * 1000.0)


def slot_width_for_screen(screen) -> float:
    """Use the system-reported gap between the notch's left and right areas."""
    try:
        left = screen.auxiliaryTopLeftArea()
        right = screen.auxiliaryTopRightArea()
        left_edge = left.origin.x + left.size.width
        width = right.origin.x - left_edge
        if width >= 120.0:
            return max(180.0, min(320.0, float(width)))
    except Exception:
        pass
    return WINDOW_WIDTH


def notch_depth_for_screen(screen) -> float:
    try:
        notch_depth = float(screen.safeAreaInsets().top)
    except Exception:
        return 0.0
    return notch_depth if notch_depth >= 1.0 else 0.0


def screen_has_notch(screen) -> bool:
    return notch_depth_for_screen(screen) > 0.0


def window_height_for_notch_depth(notch_depth: float) -> float:
    return max(0.0, float(notch_depth)) + LED_BAND_HEIGHT


def virtual_window_frame_for_screen(screen):
    frame = screen.frame()
    width = slot_width_for_screen(screen)
    height = window_height_for_notch_depth(notch_depth_for_screen(screen))
    x = frame.origin.x + (frame.size.width - width) / 2.0
    y = frame.origin.y + frame.size.height - height
    return ((x, y), (width, height))


def led_band_rect(width: float):
    return ((0.0, 0.0), (float(width), LED_BAND_HEIGHT))


def notch_bar_path(rect):
    """A top-attached notch silhouette extended by the 5 pt LED band."""
    x, y = rect[0]
    width, height = rect[1]
    right = x + width
    top = y + height
    bottom_radius = min(NOTCH_BOTTOM_RADIUS, width / 2.0, height / 2.0)

    path = NSBezierPath.bezierPath()
    path.moveToPoint_((x, top))
    path.lineToPoint_((right, top))
    path.lineToPoint_((right, y + bottom_radius))
    path.curveToPoint_controlPoint1_controlPoint2_(
        (right - bottom_radius, y),
        (right, y + bottom_radius * 0.45),
        (right - bottom_radius * 0.45, y),
    )
    path.lineToPoint_((x + bottom_radius, y))
    path.curveToPoint_controlPoint1_controlPoint2_(
        (x, y + bottom_radius),
        (x + bottom_radius * 0.45, y),
        (x, y + bottom_radius * 0.45),
    )
    path.lineToPoint_((x, top))
    path.closePath()
    return path


def virtual_led_colors(
    state: LedDisplayState,
    elapsed: float,
    brightness: int | float = 255,
) -> list[tuple[float, float, float, float]]:
    """Return the eight LED colors for the same status animations as the device."""
    scale = normalize_brightness(brightness) / 255.0
    if state == LedDisplayState.DONE:
        return [(0.0, scale, 0.4 * scale, 1.0)] * LED_COUNT
    if state == LedDisplayState.ASK:
        amount = 0.5 - 0.5 * math.cos(2.0 * math.pi * (elapsed % 1.6) / 1.6)
        return [(scale * amount, 0.227 * scale * amount, 0.0, amount)] * LED_COUNT
    if state == LedDisplayState.IDLE:
        amount = 0.5 - 0.5 * math.cos(2.0 * math.pi * (elapsed % 6.0) / 6.0)
        dim = 2.0 / 255.0
        return [(dim * scale * amount, dim * scale * amount, 2 * dim * scale * amount, amount)] * LED_COUNT

    # Mirrors rolling_program(): 760 ms pulses staggered by 95 ms.
    cycle = 0.76 + 0.095 * (LED_COUNT - 1)
    colors = []
    for index in range(LED_COUNT):
        local = (elapsed % cycle) - index * 0.095
        amount = 0.0
        if 0.0 <= local <= 0.76:
            amount = math.sin(math.pi * local / 0.76) ** 2
        colors.append((0.0, 0.898 * scale * amount, scale * amount, amount))
    return colors


def blended_led_color_at_x(
    colors: list[tuple[float, float, float, float]],
    x: float,
    led_width: float,
) -> tuple[float, float, float, float]:
    """Blend one source LED across exactly three virtual LED slots."""
    if led_width <= 0.0:
        return (0.0, 0.0, 0.0, 0.0)

    radius = led_width * LED_BLEND_RADIUS_LEDS
    totals = [0.0, 0.0, 0.0, 0.0]
    for index, color in enumerate(colors):
        center = (index + 0.5) * led_width
        distance = abs(float(x) - center)
        if distance > radius:
            continue
        phase = distance / radius
        weight = 0.5 + 0.5 * math.cos(math.pi * phase)
        for channel in range(4):
            totals[channel] += color[channel] * weight
    return tuple(min(1.0, max(0.0, value)) for value in totals)


def tone_mapped_led_color(
    red: float,
    green: float,
    blue: float,
    alpha: float,
    *,
    boost: float = 1.0,
    alpha_scale: float = 1.0,
) -> tuple[float, float, float, float]:
    def channel(value: float) -> float:
        if value <= 0.0:
            return 0.0
        return min(1.0, (value ** LED_GAMMA) * boost)

    return (
        channel(red),
        channel(green),
        channel(blue),
        min(1.0, max(0.0, alpha * alpha_scale)),
    )


def fill_rect_with_color(rect, color) -> None:
    red, green, blue, alpha = color
    if max(red, green, blue, alpha) <= 0.001:
        return
    NSColor.colorWithCalibratedRed_green_blue_alpha_(
        red, green, blue, alpha
    ).set()
    NSBezierPath.bezierPathWithRect_(rect).fill()


def fill_rect_with_cg(context, rect, color) -> None:
    red, green, blue, alpha = color
    if max(red, green, blue, alpha) <= 0.001:
        return
    if context is None:
        fill_rect_with_color(rect, color)
        return
    CGContextSetRGBFillColor(context, red, green, blue, alpha)
    CGContextFillRect(context, rect)


def current_cg_context():
    try:
        return NSGraphicsContext.currentContext().CGContext()
    except Exception:
        try:
            return NSGraphicsContext.currentContext().graphicsPort()
        except Exception:
            return None


def compact_preview_program(program: str) -> str:
    """Loop one-shot programs in compact previews without changing the source."""
    source = str(program)
    if any(line.strip().casefold() == "repeat" for line in source.splitlines()):
        return source
    colors = re.findall(r"#[0-9a-fA-F]{6}", source)
    if len(colors) >= 2 and all(color.casefold() == "#000000" for color in colors):
        # A shutdown transition depends on the physical LEDs' current pixels.
        # Seed compact previews with the working cyan so the outside-in fade is
        # visible without adding a color command to the real device program.
        source = f"#00E5FF\n{source}"
    stripped = source.rstrip()
    return f"{stripped}\nrepeat" if stripped else "repeat"


class VirtualLedView(NSView):
    def initWithFrame_(self, frame):
        self = objc.super(VirtualLedView, self).initWithFrame_(frame)
        if self is not None:
            self.state = LedDisplayState.IDLE
            self.brightness = 255
            self.started_at = time.monotonic()
            self.fixed_colors = None
            self.current_program = None
            self.rendered_program = None
            self.wasm_controller = None
            self.wasm_error = None
            self.has_notch = True
            self.compact_preview = False
        return self

    def setHasNotch_(self, has_notch):
        self.has_notch = bool(has_notch)
        self.setNeedsDisplay_(True)

    def setCompactPreview_(self, compact_preview):
        compact_preview = bool(compact_preview)
        if compact_preview != self.compact_preview and self.current_program is not None:
            current_program = self.current_program
            self.current_program = None
            self.compact_preview = compact_preview
            self.setProgram_(current_program)
            return
        self.compact_preview = compact_preview
        self.setNeedsDisplay_(True)

    def setState_brightness_(self, state, brightness):
        if state != self.state:
            self.started_at = time.monotonic()
        self.state = state
        self.brightness = normalize_brightness(brightness)
        self.fixed_colors = None
        self.setProgram_(
            program_for_display_state(
                state,
                led_count=LED_COUNT,
                brightness=self.brightness,
            )
        )

    def setProgram_(self, program):
        source_program = str(program)
        rendered_program = (
            compact_preview_program(source_program)
            if self.compact_preview
            else source_program
        )
        if (
            source_program == self.current_program
            and rendered_program == self.rendered_program
        ):
            self.setNeedsDisplay_(True)
            return
        self.current_program = source_program
        self.rendered_program = rendered_program
        self.fixed_colors = None
        self.started_at = time.monotonic()
        if self._ensure_wasm_controller():
            try:
                self.wasm_controller.parse(self.rendered_program, monotonic_ms())
                self.wasm_error = None
            except Exception as exc:
                self.wasm_error = str(exc)
        self.setNeedsDisplay_(True)

    def setBatteryPercent_brightness_(self, percent, brightness):
        self.current_program = None
        self.rendered_program = None
        scale = normalize_brightness(brightness) / 255.0
        filled = max(0.0, min(8.0, float(percent) * 8.0 / 100.0))
        colors = []
        for index in range(LED_COUNT):
            amount = max(0.0, min(1.0, filled - index))
            if percent <= 20:
                rgb = (1.0, 0.15, 0.0)
            elif percent <= 50:
                rgb = (1.0, 0.55, 0.0)
            else:
                rgb = (0.0, 1.0, 0.4)
            colors.append(tuple(channel * scale * amount for channel in rgb) + (amount,))
        self.fixed_colors = colors
        self.setNeedsDisplay_(True)

    def _ensure_wasm_controller(self) -> bool:
        if self.wasm_controller is not None:
            return True
        try:
            self.wasm_controller = SdLedWasmController(LED_COUNT)
            self.wasm_error = None
            return True
        except LedWasmUnavailableError as exc:
            self.wasm_error = str(exc)
            return False

    def _colors_for_draw(self):
        if self.fixed_colors is not None:
            return self.fixed_colors
        if self.current_program is not None and self._ensure_wasm_controller():
            try:
                pixels = self.wasm_controller.step(monotonic_ms())
                self.wasm_error = None
                return [
                    (
                        red / 255.0,
                        green / 255.0,
                        blue / 255.0,
                        max(red, green, blue) / 255.0,
                    )
                    for red, green, blue in pixels[:LED_COUNT]
                ]
            except Exception as exc:
                self.wasm_error = str(exc)
        return virtual_led_colors(
            self.state, time.monotonic() - self.started_at, self.brightness
        )

    def drawRect_(self, _rect):
        colors = self._colors_for_draw()
        width = self.bounds().size.width
        height = self.bounds().size.height

        if self.compact_preview:
            pill_height = min(12.0, max(4.0, height - 4.0))
            pill_y = (height - pill_height) / 2.0
            pill_rect = ((0.0, pill_y), (width, pill_height))
            pill = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                pill_rect,
                pill_height / 2.0,
                pill_height / 2.0,
            )
            NSColor.colorWithCalibratedRed_green_blue_alpha_(
                0.006, 0.007, 0.010, 0.94
            ).set()
            pill.fill()

            NSGraphicsContext.saveGraphicsState()
            pill.addClip()
            cg_context = current_cg_context()
            led_width = width / LED_COUNT
            column_x = 0.0
            while column_x < width:
                column_width = min(BLEND_COLUMN_WIDTH, width - column_x)
                sample_x = column_x + column_width / 2.0
                red, green, blue, alpha = blended_led_color_at_x(
                    colors,
                    sample_x,
                    led_width,
                )
                fill_rect_with_cg(
                    cg_context,
                    ((column_x, pill_y), (column_width, pill_height)),
                    tone_mapped_led_color(
                        red,
                        green,
                        blue,
                        alpha,
                        boost=LED_CORE_BOOST,
                        alpha_scale=0.88,
                    ),
                )
                fill_rect_with_cg(
                    cg_context,
                    (
                        (column_x, pill_y + pill_height * 0.38),
                        (column_width, pill_height * 0.24),
                    ),
                    tone_mapped_led_color(
                        red,
                        green,
                        blue,
                        alpha,
                        boost=LED_HOTLINE_BOOST,
                        alpha_scale=0.54,
                    ),
                )
                column_x += column_width
            NSGraphicsContext.restoreGraphicsState()
            return

        body = notch_bar_path(((0.0, 0.0), (width, height)))

        # A MacBook gets the black camera-housing continuation. On a notchless
        # display the window remains transparent and contains only LED color.
        if self.has_notch:
            NSColor.colorWithCalibratedRed_green_blue_alpha_(
                0.006, 0.007, 0.010, 0.93
            ).set()
            body.fill()

        NSGraphicsContext.saveGraphicsState()
        body.addClip()
        cg_context = current_cg_context()

        led_width = width / LED_COUNT
        glow_height = min(LED_GLOW_HEIGHT, max(0.0, height - LED_BAND_HEIGHT))
        column_x = 0.0
        while column_x < width:
            column_width = min(BLEND_COLUMN_WIDTH, width - column_x)
            sample_x = column_x + column_width / 2.0
            red, green, blue, alpha = blended_led_color_at_x(colors, sample_x, led_width)
            if max(red, green, blue, alpha) <= 0.001:
                column_x += column_width
                continue

            # The light source is centered on its target LED and fades through
            # the neighboring LED width on both sides, for a three-LED footprint.
            fill_rect_with_cg(
                cg_context,
                ((column_x, LED_BAND_HEIGHT), (column_width, glow_height * 0.45)),
                tone_mapped_led_color(
                    red, green, blue, alpha, boost=0.82, alpha_scale=0.18
                ),
            )
            fill_rect_with_cg(
                cg_context,
                (
                    (column_x, LED_BAND_HEIGHT + glow_height * 0.45),
                    (column_width, glow_height * 0.55),
                ),
                tone_mapped_led_color(
                    red, green, blue, alpha, boost=0.64, alpha_scale=0.07
                ),
            )
            fill_rect_with_cg(
                cg_context,
                ((column_x, 0.0), (column_width, LED_BAND_HEIGHT)),
                tone_mapped_led_color(
                    red, green, blue, alpha, boost=LED_CORE_BOOST, alpha_scale=0.92
                ),
            )
            fill_rect_with_cg(
                cg_context,
                ((column_x, 0.0), (column_width, 1.15)),
                tone_mapped_led_color(
                    red, green, blue, alpha, boost=LED_HOTLINE_BOOST, alpha_scale=0.72
                ),
            )
            column_x += column_width

        if self.has_notch:
            fill_rect_with_cg(
                cg_context,
                ((0.0, LED_BAND_HEIGHT - 0.55), (width, 0.55)),
                (0.0, 0.0, 0.0, 0.18),
            )
            fill_rect_with_cg(
                cg_context,
                ((0.0, 0.0), (width, 0.45)),
                (1.0, 1.0, 1.0, 0.055),
            )
        NSGraphicsContext.restoreGraphicsState()


class VirtualStatusDevice(NSObject):
    def init(self):
        self = objc.super(VirtualStatusDevice, self).init()
        if self is not None:
            self.window = None
            self.view = None
            self.timer = None
            self.frame_interval = None
        return self

    def show(self):
        if self.window is None:
            self._build_window()
        self.reposition()
        self.window.orderFrontRegardless()

    def hide(self):
        self._stop_timer()
        if self.window is not None:
            self.window.orderOut_(None)

    def set_state(self, state: LedDisplayState, brightness: int | float):
        self.show()
        self.view.setState_brightness_(state, brightness)
        if state in {LedDisplayState.WORKING, LedDisplayState.ASK}:
            self._ensure_timer(FRAME_INTERVAL)
        elif state == LedDisplayState.IDLE:
            self._ensure_timer(IDLE_FRAME_INTERVAL)
        else:
            self._stop_timer()

    def set_battery(self, percent: int, brightness: int | float):
        self.show()
        self.view.setBatteryPercent_brightness_(percent, brightness)
        self._stop_timer()

    def set_program(self, program: str):
        self.show()
        self.view.setProgram_(program)
        self._ensure_timer(FRAME_INTERVAL)

    def _ensure_timer(self, interval: float):
        if self.timer is not None and self.frame_interval == interval:
            return
        self._stop_timer()
        self.frame_interval = interval
        self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            interval, self, "redraw:", None, True
        )

    def _stop_timer(self):
        if self.timer is not None:
            self.timer.invalidate()
            self.timer = None
        self.frame_interval = None

    def reposition(self):
        screen = NSScreen.mainScreen()
        if screen is None or self.window is None:
            return
        window_frame = virtual_window_frame_for_screen(screen)
        self.window.setFrame_display_(window_frame, True)
        if self.view is not None:
            self.view.setHasNotch_(screen_has_notch(screen))
            self.view.setFrame_(((0, 0), window_frame[1]))

    def _build_window(self):
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            ((0, 0), (WINDOW_WIDTH, WINDOW_HEIGHT)),
            NSWindowStyleMaskBorderless,
            NSBackingStoreBuffered,
            False,
        )
        self.window.setOpaque_(False)
        self.window.setBackgroundColor_(NSColor.clearColor())
        self.window.setHasShadow_(False)
        self.window.setIgnoresMouseEvents_(True)
        self.window.setLevel_(25)  # NSStatusWindowLevel without another SDK dependency.
        self.window.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorFullScreenAuxiliary
        )
        self.view = VirtualLedView.alloc().initWithFrame_(((0, 0), (WINDOW_WIDTH, WINDOW_HEIGHT)))
        self.window.setContentView_(self.view)

    @objc.IBAction
    def redraw_(self, _sender):
        if self.view is not None:
            self.view.setNeedsDisplay_(True)
