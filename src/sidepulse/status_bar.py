from __future__ import annotations

import json
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

try:
    import objc
    from AppKit import (
        NSApp,
        NSApplication,
        NSApplicationActivationPolicyAccessory,
        NSBackingStoreBuffered,
        NSBezierPath,
        NSBezelStyleRounded,
        NSButton,
        NSButtonTypeSwitch,
        NSColor,
        NSCompositingOperationSourceOver,
        NSFont,
        NSFontAttributeName,
        NSForegroundColorAttributeName,
        NSImage,
        NSMenu,
        NSMenuItem,
        NSOffState,
        NSOnState,
        NSOpenPanel,
        NSPopUpButton,
        NSScrollView,
        NSSavePanel,
        NSSlider,
        NSStatusBar,
        NSTabView,
        NSTabViewItem,
        NSTextField,
        NSTextView,
        NSView,
        NSWorkspace,
        NSWindow,
        NSWindowStyleMaskClosable,
        NSWindowStyleMaskMiniaturizable,
        NSWindowStyleMaskTitled,
        NSVariableStatusItemLength,
    )
    from Foundation import NSObject, NSString, NSTimer, NSURL
except ImportError as exc:  # pragma: no cover - only exercised on non-macOS setups.
    raise SystemExit(
        f"The status-bar app requires PyObjC/AppKit ({exc}):\n"
        "  python3 -m pip install pyobjc-framework-Cocoa"
    ) from exc

try:
    from ScriptingBridge import SBApplication
except ImportError:  # pragma: no cover - only the Ghostty integration needs this.
    SBApplication = None

from .battery import (
    BatteryLedController,
    BatterySnapshot,
    format_watts,
    program_for_battery,
    read_battery_snapshot,
)
from .audit import (
    default_status_audit_log_path,
    export_status_audit_csv,
    export_status_audit_html,
)
from .collector import LiveAgentMonitor, SourceSpec, read_recent_lines
from .device_writer import (
    DEFAULT_FILE_NAME,
    MOUNT_ROOT,
    DeviceCandidate,
    DeviceWriteError,
    discover_devices,
    normalize_led_text,
    path_exists,
    target_from_device_path,
    validate_led_text,
    write_led_program,
)
from .keep_awake import KEEPALIVE_FILE_NAME, KeepAwakeController
from .ipc import HookEventServer, default_event_socket_path, default_latest_state_path
from .install import (
    install_claude_hooks,
    install_codex_hooks,
    install_grok_hooks,
    uninstall_claude_hooks,
    uninstall_codex_hooks,
    uninstall_grok_hooks,
)
from .led_status import (
    AgentLedController,
    apply_brightness,
    brightness_percent,
    normalize_brightness,
    normalized_device_name,
    program_for_display_state,
    write_mode_to_leds,
    display_state_for_mode,
)
from .virtual_device import VIRTUAL_DEVICE_ID, VIRTUAL_DEVICE_NAME, VirtualStatusDevice
from .lid_sleep import (
    LID_POLL_SECONDS,
    ClosedLidAwakeController,
    read_lid_closed,
    sleep_helper_install_command,
    sleep_helper_installed,
)
from .models import AgentMode, AgentStatus, provider_label
from .providers import (
    ProviderConfig,
    detect_claude_config,
    detect_codex_config,
    detect_grok_config,
    detect_log_path,
    default_state_dir,
    parse_log_line,
)
from .sd_eject_guard_launch import (
    SD_EJECT_GUARD_DISPLAY_NAME,
    install_sd_eject_guard,
    sd_eject_guard_installed,
    uninstall_sd_eject_guard,
)
from .session_actions import (
    SESSION_OPEN_APP,
    SESSION_OPEN_TERMINAL,
    SESSION_OPEN_VSCODE,
    available_session_open_actions,
    default_session_open_action,
    session_open_action_label,
    session_open_target,
)
from .settings import (
    CLOSED_LID_AWAKE_AGENTS,
    CLOSED_LID_AWAKE_ALWAYS,
    CLOSED_LID_AWAKE_CHOICES,
    CLOSED_LID_AWAKE_NEVER,
    DEFAULT_IDLE_TIMEOUT_SECONDS,
    DEFAULT_RECENT_SESSION_RETENTION_SECONDS,
    LED_DISPLAY_AGENT,
    LED_DISPLAY_BATTERY,
    LED_DISPLAY_CUSTOM,
    LID_ANIMATION_CLOSED,
    LID_ANIMATION_OPEN,
    TERMINAL_APP_ALACRITTY,
    TERMINAL_APP_CUSTOM,
    TERMINAL_APP_GHOSTTY,
    TERMINAL_APP_ITERM,
    TERMINAL_APP_KITTY,
    TERMINAL_APP_TERMINAL,
    TERMINAL_APP_CHOICES,
    TERMINAL_APP_WARP,
    TERMINAL_APP_WEZTERM,
    AgentMonitorSettings,
    LedAnimationSetting,
    default_settings_path,
    default_lid_animation,
    load_settings,
    normalize_terminal_app,
    normalize_animation_duration,
    save_settings,
)
from .status_bar_launch import install_launch_agent, launch_agent_installed


@dataclass(frozen=True)
class StatusBarState:
    label: str
    symbol: str
    priority: int


@dataclass(frozen=True)
class StatusBarDevice:
    device_id: str
    name: str
    root: Path
    target: Path
    connected: bool
    display: str
    brightness: int = 255
    reason: str = ""


@dataclass(frozen=True)
class TerminalAppSpec:
    key: str
    label: str
    app_names: tuple[str, ...]
    system_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class TerminalSessionHints:
    provider: str
    session_id: str
    cwd: str
    title: str
    match_title: str = ""


STATE_IDLE = StatusBarState("Idle", "circle", 4)
STATE_WORKING = StatusBarState("Working", "arrow.triangle.2.circlepath", 2)
STATE_DONE = StatusBarState("Done", "checkmark.circle", 3)
STATE_ASK = StatusBarState("Ask", "questionmark.circle", 1)
STATUS_BAR_DEVICE_PRIORITY = ("sidepulsepro", "sidepulsedot")
STATUS_BAR_KEEPALIVE_VOLUME_NAMES = (
    "SidePulsePro",
    "SidePulseDot",
)
STATUS_BAR_REFRESH_SECONDS = 15.0
STATUS_BAR_DEVICE_POLL_SECONDS = 2.0
STATUS_BAR_SESSION_HISTORY_LIMIT = 10
SCREEN_BAR_FEATURE_ENABLED = True
STATUS_BAR_MAX_LINES_PER_SOURCE = 500
STATUS_BAR_STARTUP_REPLAY_LINES = 200
LID_ANIMATION_RESTORE_FUDGE_SECONDS = 0.15
LID_ANIMATION_LABELS = {
    LID_ANIMATION_CLOSED: "Lid Closed",
    LID_ANIMATION_OPEN: "Lid Open",
}
CLOSED_LID_AWAKE_LABELS = {
    CLOSED_LID_AWAKE_NEVER: "Never",
    CLOSED_LID_AWAKE_AGENTS: "When Agents Work",
    CLOSED_LID_AWAKE_ALWAYS: "Always",
}
TERMINAL_APP_LABELS = {
    TERMINAL_APP_TERMINAL: "Terminal",
    TERMINAL_APP_ITERM: "iTerm",
    TERMINAL_APP_GHOSTTY: "Ghostty",
    TERMINAL_APP_WARP: "Warp",
    TERMINAL_APP_KITTY: "Kitty",
    TERMINAL_APP_WEZTERM: "WezTerm",
    TERMINAL_APP_ALACRITTY: "Alacritty",
    TERMINAL_APP_CUSTOM: "Custom",
}
TERMINAL_APP_SPECS = (
    TerminalAppSpec(
        TERMINAL_APP_TERMINAL,
        "Terminal",
        ("Terminal.app",),
        (
            "/System/Applications/Utilities/Terminal.app",
            "/Applications/Utilities/Terminal.app",
            "/Applications/Terminal.app",
        ),
    ),
    TerminalAppSpec(TERMINAL_APP_ITERM, "iTerm", ("iTerm.app", "iTerm2.app")),
    TerminalAppSpec(
        TERMINAL_APP_GHOSTTY,
        "Ghostty",
        ("Ghostty.app", "Ghostly.app"),
    ),
    TerminalAppSpec(TERMINAL_APP_WARP, "Warp", ("Warp.app",)),
    TerminalAppSpec(TERMINAL_APP_KITTY, "Kitty", ("kitty.app", "Kitty.app")),
    TerminalAppSpec(TERMINAL_APP_WEZTERM, "WezTerm", ("WezTerm.app",)),
    TerminalAppSpec(TERMINAL_APP_ALACRITTY, "Alacritty", ("Alacritty.app",)),
)


def state_for_mode(mode: AgentMode) -> StatusBarState:
    if mode in {AgentMode.WAITING_FOR_INPUT, AgentMode.BLOCKED_ERROR}:
        return STATE_ASK
    if mode in {
        AgentMode.WORKING,
        AgentMode.TOOL_RUNNING,
        AgentMode.LONG_TASK_PROGRESS,
    }:
        return STATE_WORKING
    if mode == AgentMode.COMPLETED:
        return STATE_DONE
    return STATE_IDLE


def replay_recent_debug_logs(
    monitor: LiveAgentMonitor,
    *,
    providers: tuple[str, ...] = ("codex", "claude", "grok"),
    max_lines: int = STATUS_BAR_STARTUP_REPLAY_LINES,
) -> int:
    replayed = 0
    for provider in providers:
        try:
            path = detect_log_path(provider)
            lines = read_recent_lines(path, max_lines) if path.exists() else []
        except Exception as exc:
            log_status_bar(f"startup_replay {provider} error: {exc}")
            continue

        for line in lines:
            record = parse_log_line(provider, line)
            if record is None:
                continue
            monitor.ingest_record(record)
            replayed += 1
    return replayed


def build_keep_awake_controller(settings: AgentMonitorSettings) -> KeepAwakeController:
    """Build the keep-awake controller from persisted settings.

    Kept module-level so the wiring between settings and controller is testable
    without instantiating the Objective-C status bar controller.
    """
    return KeepAwakeController(enabled=settings.keep_awake_enabled)


class StatusBarController(NSObject):
    def init(self):
        self = objc.super(StatusBarController, self).init()
        if self is None:
            return None

        self.settings = load_settings()
        self.monitor = self.build_monitor()
        self.event_server = None
        self.status_item = None
        self.timer = None
        self.lid_timer = None
        self.device_timer = None
        self.settings_window = None
        self.setup_window = None
        self.settings_fields = {}
        self.settings_buttons = {}
        self.setup_fields = {}
        self.setup_buttons = {}
        self.last_snapshot = None
        self.last_battery_snapshot = None
        self.last_battery_error = None
        self.last_power_connected = None
        self.battery_preview_until = 0.0
        self.current_state = STATE_IDLE
        self.led_controller = AgentLedController()
        self.battery_led_controller = BatteryLedController()
        self.agent_led_controllers_by_device = {}
        self.battery_led_controllers_by_device = {}
        self.last_led_display_kind_by_device = {}
        self.device_errors = {}
        self.leds_enabled = True
        self.led_sync_in_flight = False
        self.last_led_error = None
        self.last_led_display_kind = LED_DISPLAY_AGENT
        self.last_connected_device_signature = None
        self.keep_awake = build_keep_awake_controller(self.settings)
        self.closed_lid_awake = ClosedLidAwakeController(
            use_system_disable=sleep_helper_installed(),
        )
        self.last_keep_awake_error = None
        self.last_closed_lid_awake_error = None
        self.last_status_read_error = None
        self.event_refresh_pending = False
        self.last_lid_closed = None
        self.last_lid_error = None
        self.led_animation_until_monotonic = 0.0
        self.led_animation_token = 0
        self.virtual_status_device = VirtualStatusDevice.alloc().init()
        return self

    def applicationDidFinishLaunching_(self, _notification):
        NSApp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        log_status_bar("launching status item")
        self.start_event_server()
        self.replay_debug_logs()

        self.status_item = NSStatusBar.systemStatusBar().statusItemWithLength_(
            NSVariableStatusItemLength
        )
        button = self.status_item.button()
        button.setTitle_(" Idle")
        button.setImage_(image_for_symbol(STATE_IDLE.symbol, STATE_IDLE.label))
        button.setToolTip_("SidePulse Agent Monitor: Idle")
        log_status_bar("status item created")

        self.refresh_(None)
        self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            STATUS_BAR_REFRESH_SECONDS,
            self,
            "refresh:",
            None,
            True,
        )
        self.lid_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            LID_POLL_SECONDS,
            self,
            "pollLid:",
            None,
            True,
        )
        self.device_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            STATUS_BAR_DEVICE_POLL_SECONDS,
            self,
            "pollDevices:",
            None,
            True,
        )
        self.show_setup_window_if_needed()
        if SCREEN_BAR_FEATURE_ENABLED and self.settings.virtual_status_device_enabled:
            self.virtual_status_device.show()
        else:
            self.virtual_status_device.hide()

    @objc.IBAction
    def refresh_(self, _sender):
        try:
            snapshot = self.monitor.snapshot(include_stale=False)
        except Exception as exc:
            log_status_bar(f"refresh error: {exc}")
            self.set_status(STATE_ASK)
            self.sync_keep_awake(AgentMode.BLOCKED_ERROR)
            self.sync_leds(AgentMode.BLOCKED_ERROR, None, LED_DISPLAY_AGENT)
            self.status_item.setMenu_(build_error_menu(exc))
            return

        self.last_snapshot = snapshot
        battery_snapshot = self.read_battery_snapshot()
        state = state_for_mode(snapshot.aggregate.mode)
        self.observe_connected_devices()
        self.set_status(state)
        self.sync_keep_awake(snapshot.aggregate.mode)
        self.sync_leds(
            snapshot.aggregate.mode,
            battery_snapshot,
            self.active_led_display_kind(battery_snapshot),
        )
        self.status_item.setMenu_(build_menu(snapshot, state, self))

    @objc.IBAction
    def forceRefresh_(self, _sender):
        self.refresh_(None)

    @objc.IBAction
    def openDeepLink_(self, sender):
        url = sender.representedObject()
        if not url:
            return
        open_url(str(url))

    @objc.IBAction
    def resumeSession_(self, sender):
        command = sender.representedObject()
        if command:
            open_terminal_command(
                str(command),
                terminal_app=self.settings.session_terminal_app,
                custom_terminal_path=self.settings.custom_terminal_path,
            )

    @objc.IBAction
    def openSession_(self, sender):
        self.open_session(sender.representedObject(), None, remember=False)

    @objc.IBAction
    def openSessionPrimary_(self, sender):
        self.open_session(
            sender.representedObject(),
            None,
            remember=False,
        )
        self.close_status_menu()

    @objc.IBAction
    def openSessionOptions_(self, sender):
        status = sender.representedObject()
        if not isinstance(status, AgentStatus):
            return
        menu = build_session_options_menu(status, datetime.now().astimezone(), self)
        try:
            height = sender.bounds().size.height
        except Exception:
            try:
                height = sender.bounds()[1][1]
            except Exception:
                height = 0
        menu.popUpMenuPositioningItem_atLocation_inView_(None, (0, height), sender)

    @objc.IBAction
    def openSessionWithAction_(self, sender):
        payload = sender.representedObject()
        if not isinstance(payload, dict):
            return
        self.open_session(payload.get("status"), payload.get("action"), remember=True)
        self.close_status_menu()

    @objc.IBAction
    def setProviderOpenPreference_(self, sender):
        selected = sender.selectedItem()
        payload = selected.representedObject() if selected is not None else None
        if not isinstance(payload, dict):
            return
        provider = payload.get("provider")
        action = payload.get("action")
        if not isinstance(provider, str) or not isinstance(action, str):
            return
        try:
            self.settings = self.settings.with_provider_session_open_action(provider, action)
            save_settings(self.settings)
        except Exception as exc:
            self.set_settings_message(f"Could not save {provider.title()} opener: {exc}")
            self.settings = load_settings()
            self.refresh_settings_window()
            return
        self.set_settings_message(
            f"{provider.title()} sessions: "
            f"{provider_open_action_label(provider, action, self.settings)}."
        )

    @objc.IBAction
    def setSessionTerminal_(self, sender):
        selected = sender.selectedItem()
        terminal = selected.representedObject() if selected is not None else None
        if not isinstance(terminal, str):
            return
        terminal = normalize_terminal_app(terminal)
        if terminal == TERMINAL_APP_CUSTOM and not self.settings.custom_terminal_path:
            self.choose_session_terminal_app()
            return
        if terminal != TERMINAL_APP_CUSTOM and not terminal_app_installed(terminal):
            self.set_settings_message(f"{terminal_app_label(terminal)} is not installed.")
            self.refresh_settings_window()
            return
        self.set_session_terminal(terminal)

    @objc.IBAction
    def chooseSessionTerminal_(self, _sender):
        self.choose_session_terminal_app()

    @objc.IBAction
    def toggleDeviceConnection_(self, _sender):
        if self.device_connected():
            self.disconnect_device()
        else:
            self.connect_device()
        self.refresh_(None)

    @objc.IBAction
    def toggleKeepAwake_(self, _sender):
        self.set_keep_awake_enabled(not self.keep_awake.enabled)

    @objc.IBAction
    def setClosedLidAwakePolicy_(self, sender):
        self.set_closed_lid_awake_policy(sender.representedObject())

    @objc.IBAction
    def openSettings_(self, _sender):
        self.show_settings_window()

    @objc.IBAction
    def openSetup_(self, _sender):
        self.show_setup_window()

    @objc.IBAction
    def runFirstLaunchSetup_(self, _sender):
        self.run_first_launch_setup()

    @objc.IBAction
    def skipFirstLaunchSetup_(self, _sender):
        self.complete_first_launch_setup("Setup skipped.")

    @objc.IBAction
    def uninstallSdEjectGuard_(self, _sender):
        self.uninstall_sd_eject_guard_from_setup()

    @objc.IBAction
    def installCodexHooks_(self, _sender):
        self.update_hooks("codex", install=True)

    @objc.IBAction
    def uninstallCodexHooks_(self, _sender):
        self.update_hooks("codex", install=False)

    @objc.IBAction
    def installClaudeHooks_(self, _sender):
        self.update_hooks("claude", install=True)

    @objc.IBAction
    def uninstallClaudeHooks_(self, _sender):
        self.update_hooks("claude", install=False)

    @objc.IBAction
    def installGrokHooks_(self, _sender):
        self.update_hooks("grok", install=True)

    @objc.IBAction
    def uninstallGrokHooks_(self, _sender):
        self.update_hooks("grok", install=False)

    @objc.IBAction
    def toggleCodexTranscripts_(self, sender):
        self.set_transcript_monitoring("codex", sender.state() == NSOnState)

    @objc.IBAction
    def toggleClaudeTranscripts_(self, sender):
        self.set_transcript_monitoring("claude", sender.state() == NSOnState)

    @objc.IBAction
    def toggleBatteryLedDisplay_(self, _sender):
        self.set_battery_led_display(self.settings.led_display != LED_DISPLAY_BATTERY)

    @objc.IBAction
    def setBatteryLedDisplayFromCheckbox_(self, sender):
        self.set_battery_led_display(sender.state() == NSOnState)

    @objc.IBAction
    def toggleBatteryPowerPreview_(self, _sender):
        self.set_battery_power_preview(not self.settings.battery_show_on_power_change)

    @objc.IBAction
    def setBatteryPowerPreviewFromCheckbox_(self, sender):
        self.set_battery_power_preview(sender.state() == NSOnState)

    @objc.IBAction
    def saveAgentListTiming_(self, _sender):
        self.save_agent_list_timing_from_fields()

    @objc.IBAction
    def setDeviceDisplayAgent_(self, sender):
        self.set_device_display(sender.representedObject(), LED_DISPLAY_AGENT)

    @objc.IBAction
    def setDeviceDisplayBattery_(self, sender):
        self.set_device_display(sender.representedObject(), LED_DISPLAY_BATTERY)

    @objc.IBAction
    def setDeviceDisplayCustom_(self, sender):
        self.set_device_display(sender.representedObject(), LED_DISPLAY_CUSTOM)

    @objc.IBAction
    def setDeviceBrightness_(self, sender):
        device_id = sender.identifier()
        if device_id is None:
            return
        self.set_device_brightness(str(device_id), sender.doubleValue())

    @objc.IBAction
    def toggleVirtualStatusDevice_(self, _sender):
        if not SCREEN_BAR_FEATURE_ENABLED:
            self.set_virtual_status_device(False)
            return
        self.set_virtual_status_device(not self.settings.virtual_status_device_enabled)

    @objc.IBAction
    def saveLidAnimations_(self, _sender):
        self.save_lid_animations_from_fields()

    @objc.IBAction
    def previewLidClosedAnimation_(self, _sender):
        animation = self.lid_animation_from_fields(LID_ANIMATION_CLOSED)
        if animation is not None:
            self.play_lid_animation(LID_ANIMATION_CLOSED, animation=animation)

    @objc.IBAction
    def previewLidOpenAnimation_(self, _sender):
        animation = self.lid_animation_from_fields(LID_ANIMATION_OPEN)
        if animation is not None:
            self.play_lid_animation(LID_ANIMATION_OPEN, animation=animation)

    @objc.IBAction
    def resetLidClosedAnimation_(self, _sender):
        self.reset_lid_animation(LID_ANIMATION_CLOSED)

    @objc.IBAction
    def resetLidOpenAnimation_(self, _sender):
        self.reset_lid_animation(LID_ANIMATION_OPEN)

    @objc.IBAction
    def removeRememberedDevice_(self, sender):
        self.remove_remembered_device(sender.representedObject())

    @objc.IBAction
    def quit_(self, _sender):
        self.closed_lid_awake.release()
        self.keep_awake.release()
        NSApp.terminate_(self)

    def applicationWillTerminate_(self, _notification):
        self.stop_event_server()
        self.closed_lid_awake.release()
        self.keep_awake.release()

    def set_status(self, state: StatusBarState) -> None:
        previous = self.current_state
        self.current_state = state
        if self.status_item is None:
            return
        button = self.status_item.button()
        if button is None:
            return
        button.setTitle_(f" {state.label}")
        button.setImage_(image_for_symbol(state.symbol, state.label))
        button.setToolTip_(f"SidePulse Agent Monitor: {state.label}")
        if previous != state:
            log_status_bar(f"state={state.label}")

    def build_monitor(self) -> LiveAgentMonitor:
        socket_path = default_event_socket_path()
        return LiveAgentMonitor(
            sources=(SourceSpec("event-bus", socket_path),),
            stale_after_seconds=self.settings.idle_timeout_seconds,
            latest_state_path=default_latest_state_path(),
        )

    def reload_monitor(self) -> None:
        self.monitor = self.build_monitor()

    def start_event_server(self) -> None:
        self.stop_event_server()
        self.event_server = HookEventServer(self.handle_hook_event_message)
        try:
            socket_path = self.event_server.start()
            log_status_bar(f"event_server listening={socket_path}")
        except Exception as exc:
            self.event_server = None
            log_status_bar(f"event_server error: {exc}")

    def stop_event_server(self) -> None:
        if self.event_server is not None:
            self.event_server.stop()
            self.event_server = None

    def handle_hook_event_message(self, provider: str, line: dict) -> None:
        try:
            record = parse_log_line(
                provider,
                json.dumps(line, separators=(",", ":"), ensure_ascii=False),
            )
            if record is not None:
                self.monitor.ingest_record(record)
                self.schedule_event_refresh()
        except Exception as exc:
            log_status_bar(f"event_server ingest error: {exc}")

    def schedule_event_refresh(self) -> None:
        if self.event_refresh_pending:
            return
        self.event_refresh_pending = True
        self.performSelectorOnMainThread_withObject_waitUntilDone_(
            "refreshFromEvent:",
            None,
            False,
        )

    @objc.IBAction
    def refreshFromEvent_(self, _sender):
        self.event_refresh_pending = False
        self.refresh_(None)

    def replay_debug_logs(self) -> None:
        replayed = replay_recent_debug_logs(self.monitor)
        if replayed:
            log_status_bar(f"startup_replay events={replayed}")

    def show_settings_window(self) -> None:
        if self.settings_window is None:
            self.settings_window = build_settings_window(self)
        self.refresh_settings_window()
        self.settings_window.makeKeyAndOrderFront_(None)
        NSApp.activateIgnoringOtherApps_(True)

    def show_setup_window_if_needed(self) -> None:
        if should_show_setup_window(self.settings):
            self.show_setup_window()

    def show_setup_window(self) -> None:
        if self.setup_window is None:
            self.setup_window = build_setup_window(self)
        self.refresh_setup_window()
        self.setup_window.makeKeyAndOrderFront_(None)
        NSApp.activateIgnoringOtherApps_(True)

    def refresh_setup_window(self) -> None:
        if self.setup_window is None:
            return

        launch_installed = launch_agent_installed()
        eject_installed = sd_eject_guard_installed()
        sleep_installed = sleep_helper_installed()

        set_field_value(
            self.setup_fields.get("launch_status"),
            "Installed" if launch_installed else "Not installed",
        )
        set_field_value(
            self.setup_fields.get("eject_status"),
            "Installed" if eject_installed else "Not installed",
        )
        set_field_value(
            self.setup_fields.get("sleep_status"),
            "Installed" if sleep_installed else "Needs administrator setup",
        )
        self.set_setup_checkbox("launch", True, enabled=not launch_installed)
        self.set_setup_checkbox("eject_guard", True, enabled=not eject_installed)
        self.set_setup_checkbox("sleep_helper", True, enabled=not sleep_installed)
        eject_uninstall = self.setup_buttons.get("eject_guard_uninstall")
        if eject_uninstall is not None:
            eject_uninstall.setEnabled_(eject_installed)

    def set_setup_checkbox(self, key: str, checked: bool, *, enabled: bool) -> None:
        button = self.setup_buttons.get(key)
        if button is None:
            return
        set_checkbox_state(button, checked)
        button.setEnabled_(enabled)

    def run_first_launch_setup(self) -> None:
        messages: list[str] = []
        errors: list[str] = []
        opened_sleep_installer = False

        if checkbox_is_on(self.setup_buttons.get("launch")) and not launch_agent_installed():
            try:
                result = install_launch_agent(start=False)
                messages.append("Run at Login installed." if result.changed else "Run at Login already installed.")
            except Exception as exc:
                errors.append(f"Run at Login failed: {exc}")

        if checkbox_is_on(self.setup_buttons.get("eject_guard")) and not sd_eject_guard_installed():
            try:
                result = install_sd_eject_guard(scope="auto", start=True)
                scope_label = "system" if result.scope == "system" else "user"
                messages.append(f"{SD_EJECT_GUARD_DISPLAY_NAME} installed ({scope_label}).")
            except Exception as exc:
                errors.append(f"{SD_EJECT_GUARD_DISPLAY_NAME} failed: {exc}")

        if checkbox_is_on(self.setup_buttons.get("sleep_helper")) and not sleep_helper_installed():
            try:
                path = open_terminal_setup_command(sleep_helper_install_command())
                messages.append(f"Sleep prevention installer opened: {path}")
                opened_sleep_installer = True
            except Exception as exc:
                errors.append(f"Sleep prevention installer failed: {exc}")

        if errors:
            set_field_value(self.setup_fields.get("message"), "  ".join(errors))
            log_status_bar(f"setup errors: {'; '.join(errors)}")
            self.refresh_setup_window()
            return

        if opened_sleep_installer:
            message = "Finish the Terminal setup, then click Set Up again."
            set_field_value(self.setup_fields.get("message"), message)
            log_status_bar(f"setup waiting: {message}")
            self.refresh_setup_window()
            return

        if not messages:
            messages.append("Nothing to install.")
        self.complete_first_launch_setup("  ".join(messages))

    def uninstall_sd_eject_guard_from_setup(self) -> None:
        try:
            results = uninstall_sd_eject_guard(scope="auto")
        except Exception as exc:
            set_field_value(
                self.setup_fields.get("message"),
                f"Could not uninstall {SD_EJECT_GUARD_DISPLAY_NAME}: {exc}",
            )
            self.refresh_setup_window()
            return

        removed = [path for result in results for path in result.removed_paths]
        skipped = [result.skipped for result in results if result.skipped]
        if skipped:
            message = "  ".join(str(item) for item in skipped)
        elif removed:
            message = f"{SD_EJECT_GUARD_DISPLAY_NAME} uninstalled."
        else:
            message = f"{SD_EJECT_GUARD_DISPLAY_NAME} is not installed."
        set_field_value(self.setup_fields.get("message"), message)
        log_status_bar(f"setup: {message}")
        self.refresh_setup_window()

    def complete_first_launch_setup(self, message: str) -> None:
        try:
            self.settings = self.settings.with_setup_screen_completed(True)
            save_settings(self.settings)
        except Exception as exc:
            set_field_value(self.setup_fields.get("message"), f"Could not save setup: {exc}")
            return

        log_status_bar(f"setup complete: {message}")
        set_field_value(self.setup_fields.get("message"), message)
        if self.setup_window is not None:
            self.setup_window.performClose_(None)
        self.refresh_(None)

    def refresh_settings_window(self) -> None:
        if self.settings_window is None:
            return

        codex = detect_codex_config()
        claude = detect_claude_config()
        grok = detect_grok_config()
        set_field_value(
            self.settings_fields.get("codex_hook_status"),
            hook_status_text(codex),
        )
        set_field_value(
            self.settings_fields.get("claude_hook_status"),
            hook_status_text(claude),
        )
        set_field_value(
            self.settings_fields.get("grok_hook_status"),
            hook_status_text(grok),
        )
        set_field_value(
            self.settings_fields.get("settings_path"),
            f"Settings: {default_settings_path()}",
        )
        set_field_value(
            self.settings_fields.get("debug_log_status"),
            debug_log_status_text(),
        )
        set_checkbox_state(
            self.settings_buttons.get("codex_transcripts"),
            self.settings.codex_transcripts_enabled,
        )
        set_checkbox_state(
            self.settings_buttons.get("claude_transcripts"),
            self.settings.claude_transcripts_enabled,
        )
        set_checkbox_state(
            self.settings_buttons.get("battery_leds"),
            self.settings.led_display == LED_DISPLAY_BATTERY,
        )
        set_checkbox_state(
            self.settings_buttons.get("battery_power_preview"),
            self.settings.battery_show_on_power_change,
        )
        for provider in ("codex", "claude", "grok"):
            popup = self.settings_fields.get(f"{provider}_session_opener")
            if popup is not None:
                refresh_provider_opener_popup(
                    popup,
                    provider,
                    self.settings.session_open_action(provider)
                    or default_provider_open_action(provider),
                    self.settings,
                )
        terminal_popup = self.settings_fields.get("session_terminal")
        if terminal_popup is not None:
            refresh_terminal_popup(
                terminal_popup,
                self.settings.session_terminal_app,
            )
        set_field_value(
            self.settings_fields.get("custom_terminal_path"),
            terminal_settings_detail(self.settings),
        )
        closed = self.settings.lid_closed_animation
        opened = self.settings.lid_open_animation
        set_text_control_value(
            self.settings_fields.get("closed_animation_program"),
            closed.program,
        )
        set_text_control_value(
            self.settings_fields.get("closed_animation_duration"),
            f"{closed.duration_seconds:g}",
        )
        set_text_control_value(
            self.settings_fields.get("open_animation_program"),
            opened.program,
        )
        set_text_control_value(
            self.settings_fields.get("open_animation_duration"),
            f"{opened.duration_seconds:g}",
        )
        set_text_control_value(
            self.settings_fields.get("recent_session_retention_hours"),
            f"{self.settings.recent_session_retention_seconds / 3600:g}",
        )
        set_text_control_value(
            self.settings_fields.get("idle_timeout_minutes"),
            f"{self.settings.idle_timeout_seconds / 60:g}",
        )

    def set_settings_message(self, message: str) -> None:
        set_field_value(self.settings_fields.get("message"), message)
        if message:
            log_status_bar(f"settings: {message}")

    def set_session_terminal(
        self,
        terminal_app: str,
        *,
        custom_path: str | None = None,
    ) -> None:
        try:
            self.settings = self.settings.with_session_terminal(
                terminal_app,
                custom_path,
            )
            save_settings(self.settings)
        except Exception as exc:
            self.set_settings_message(f"Could not save terminal: {exc}")
            self.settings = load_settings()
            self.refresh_settings_window()
            return

        self.refresh_settings_window()
        self.set_settings_message(
            f"Session resumes open in {terminal_app_label(self.settings.session_terminal_app)}."
        )

    def choose_session_terminal_app(self) -> None:
        path = choose_terminal_app()
        if path is None:
            self.refresh_settings_window()
            return
        self.set_session_terminal(TERMINAL_APP_CUSTOM, custom_path=str(path))

    @objc.IBAction
    def exportDebugCsv_(self, _sender):
        self.export_debug_log("csv")

    @objc.IBAction
    def exportDebugHtml_(self, _sender):
        self.export_debug_log("html")

    def export_debug_log(self, format_name: str) -> None:
        path = choose_debug_export_path(format_name)
        if path is None:
            return
        try:
            if format_name == "csv":
                count = export_status_audit_csv(path)
            else:
                count = export_status_audit_html(path)
        except Exception as exc:
            self.set_settings_message(f"Debug export failed: {exc}")
            return
        self.set_settings_message(f"Exported {count} debug events to {path}.")

    def update_hooks(self, provider: str, *, install: bool) -> None:
        try:
            if provider == "codex" and install:
                result = install_codex_hooks()
            elif provider == "codex":
                result = uninstall_codex_hooks()
            elif provider == "claude" and install:
                result = install_claude_hooks()
            elif provider == "claude":
                result = uninstall_claude_hooks()
            elif install:
                result = install_grok_hooks()
            else:
                result = uninstall_grok_hooks()
        except Exception as exc:
            self.set_settings_message(f"{provider.title()} hooks failed: {exc}")
            self.refresh_settings_window()
            return

        action = "installed" if install else "removed"
        if not result.changed:
            action = "already installed" if install else "already removed"
        self.set_settings_message(f"{provider.title()} hooks {action}.")
        self.reload_monitor()
        self.refresh_settings_window()
        self.refresh_(None)

    def set_transcript_monitoring(self, provider: str, enabled: bool) -> None:
        try:
            self.settings = self.settings.with_transcript_provider(provider, enabled)
            save_settings(self.settings)
        except Exception as exc:
            self.set_settings_message(f"Could not save settings: {exc}")
            self.settings = load_settings()
            self.refresh_settings_window()
            return

        self.reload_monitor()
        self.set_settings_message(
            f"{provider.title()} transcript CLI fallback {'enabled' if enabled else 'disabled'}."
        )
        self.refresh_settings_window()
        self.refresh_(None)

    def save_agent_list_timing_from_fields(self) -> None:
        retention_text = text_control_value(
            self.settings_fields.get("recent_session_retention_hours")
        )
        idle_text = text_control_value(self.settings_fields.get("idle_timeout_minutes"))
        try:
            retention_hours = float(retention_text) if retention_text else (
                DEFAULT_RECENT_SESSION_RETENTION_SECONDS / 3600
            )
            idle_minutes = float(idle_text) if idle_text else (
                DEFAULT_IDLE_TIMEOUT_SECONDS / 60
            )
        except ValueError:
            self.set_settings_message("Agent list timing must be numeric.")
            return

        try:
            self.settings = self.settings.with_agent_list_timing(
                recent_session_retention_seconds=retention_hours * 3600,
                idle_timeout_seconds=idle_minutes * 60,
            )
            save_settings(self.settings)
        except Exception as exc:
            self.set_settings_message(f"Could not save agent list timing: {exc}")
            self.settings = load_settings()
            self.refresh_settings_window()
            return

        self.reload_monitor()
        self.set_settings_message("Agent list timing saved.")
        self.refresh_settings_window()
        self.refresh_(None)

    def set_battery_led_display(self, enabled: bool) -> None:
        try:
            display = LED_DISPLAY_BATTERY if enabled else LED_DISPLAY_AGENT
            self.settings = self.settings.with_led_display(display)
            save_settings(self.settings)
        except Exception as exc:
            self.set_settings_message(f"Could not save settings: {exc}")
            self.settings = load_settings()
            self.refresh_settings_window()
            return

        self.reset_led_controllers_for_display_change()
        self.set_settings_message(f"LED display set to {self.settings.led_display}.")
        self.refresh_settings_window()
        self.refresh_(None)

    def set_device_display(self, device_id: str | None, display: str) -> None:
        if not device_id:
            return
        device = next(
            (
                entry
                for entry in self.status_bar_devices(remember=False)
                if entry.device_id == str(device_id)
            ),
            None,
        )
        try:
            self.settings = self.settings.with_device_display(
                str(device_id),
                display,
                name=device.name if device else None,
                path=str(device.root) if device else None,
            )
            save_settings(self.settings)
        except Exception as exc:
            self.set_settings_message(f"Could not save device display: {exc}")
            self.settings = load_settings()
            return

        self.reset_led_controllers_for_device(str(device_id))
        label = device_display_label(display)
        clear_error = None
        if display == LED_DISPLAY_CUSTOM:
            clear_error = self.clear_manual_device_display(device)
        name = device.name if device else device_id
        if clear_error:
            self.set_settings_message(f"{name}: {label}, clear failed: {clear_error}")
        elif display == LED_DISPLAY_CUSTOM and device is not None and device.connected:
            self.set_settings_message(f"{name}: {label}, LEDs cleared.")
        else:
            self.set_settings_message(f"{name}: {label}.")
        self.refresh_settings_window()
        self.refresh_(None)

    def clear_manual_device_display(self, device: StatusBarDevice | None) -> str | None:
        if device is None or not device.connected:
            return None
        if device.device_id == VIRTUAL_DEVICE_ID:
            self.virtual_status_device.hide()
            return None
        try:
            target = write_led_program("off", device_path=device.target)
        except Exception as exc:
            error = str(exc)
            self.device_errors[device.device_id] = error
            self.last_led_error = error
            log_status_bar(f"manual clear error {device.name}: {error}")
            return error

        self.device_errors.pop(device.device_id, None)
        self.last_led_error = next(iter(self.device_errors.values()), None)
        log_status_bar(f"manual clear device={device.name} target={target}")
        return None

    def set_device_brightness(self, device_id: str | None, brightness: int | float) -> None:
        if not device_id:
            return
        device = next(
            (
                entry
                for entry in self.status_bar_devices(remember=False)
                if entry.device_id == str(device_id)
            ),
            None,
        )
        value = normalize_brightness(brightness)
        try:
            self.settings = self.settings.with_device_brightness(
                str(device_id),
                value,
                name=device.name if device else None,
                path=str(device.root) if device else None,
            )
            save_settings(self.settings)
        except Exception as exc:
            self.set_settings_message(f"Could not save brightness: {exc}")
            self.settings = load_settings()
            return

        self.reset_led_controllers_for_device(str(device_id))
        self.set_settings_message(
            f"{device.name if device else device_id}: brightness {brightness_percent(value)}%."
        )
        self.refresh_settings_window()
        if self.last_snapshot is not None:
            self.sync_leds(
                self.last_snapshot.aggregate.mode,
                self.last_battery_snapshot,
                self.active_led_display_kind(self.last_battery_snapshot),
            )

    def set_virtual_status_device(self, enabled: bool) -> None:
        if not SCREEN_BAR_FEATURE_ENABLED:
            try:
                self.settings = self.settings.with_virtual_status_device(False)
                save_settings(self.settings)
            except Exception as exc:
                self.set_settings_message(f"Could not disable Screen Bar: {exc}")
                return
            self.virtual_status_device.hide()
            self.set_settings_message("Screen Bar is disabled for now.")
            self.refresh_(None)
            return

        try:
            self.settings = self.settings.with_virtual_status_device(enabled)
            if enabled:
                self.settings = self.settings.with_remembered_device(
                    device_id=VIRTUAL_DEVICE_ID,
                    name=VIRTUAL_DEVICE_NAME,
                    path=VIRTUAL_DEVICE_ID,
                )
            save_settings(self.settings)
        except Exception as exc:
            self.set_settings_message(f"Could not save Screen Bar: {exc}")
            return
        if enabled:
            self.virtual_status_device.show()
        else:
            self.virtual_status_device.hide()
        self.refresh_(None)

    def set_keep_awake_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        previous = self.settings
        try:
            self.settings = self.settings.with_keep_awake_enabled(enabled)
            save_settings(self.settings)
        except Exception as exc:
            self.set_settings_message(f"Could not save keep awake setting: {exc}")
            self.settings = previous
            return

        self.keep_awake.set_enabled(enabled)
        self.set_settings_message(f"Keep awake: {'on' if enabled else 'off'}.")
        log_status_bar(f"keep_awake={'on' if enabled else 'off'}")
        self.refresh_settings_window()
        self.refresh_(None)

    def set_closed_lid_awake_policy(self, policy: str | None) -> None:
        if policy not in CLOSED_LID_AWAKE_CHOICES:
            return
        try:
            self.settings = self.settings.with_closed_lid_awake_policy(str(policy))
            save_settings(self.settings)
        except Exception as exc:
            self.set_settings_message(f"Could not save lid sleep setting: {exc}")
            self.settings = load_settings()
            return

        self.set_settings_message(
            f"Closed-lid awake: {CLOSED_LID_AWAKE_LABELS[self.settings.closed_lid_awake_policy]}."
        )
        self.sync_closed_lid_awake()
        self.refresh_settings_window()
        self.refresh_(None)

    def lid_animation_from_fields(self, kind: str) -> LedAnimationSetting | None:
        program_field = self.settings_fields.get(f"{kind}_animation_program")
        duration_field = self.settings_fields.get(f"{kind}_animation_duration")
        current = self.settings.lid_animation(kind)
        program = text_control_value(program_field) or current.program
        duration_text = text_control_value(duration_field)
        try:
            duration = float(duration_text) if duration_text else current.duration_seconds
        except ValueError:
            self.set_settings_message(f"{LID_ANIMATION_LABELS[kind]} duration is not a number.")
            return None

        animation = LedAnimationSetting(
            program=normalize_led_text(program),
            duration_seconds=normalize_animation_duration(duration),
        )
        try:
            validate_lid_animation(animation)
        except DeviceWriteError as exc:
            self.set_settings_message(f"{LID_ANIMATION_LABELS[kind]} animation invalid: {exc}")
            return None
        return animation

    def save_lid_animations_from_fields(self) -> None:
        closed = self.lid_animation_from_fields(LID_ANIMATION_CLOSED)
        if closed is None:
            return
        opened = self.lid_animation_from_fields(LID_ANIMATION_OPEN)
        if opened is None:
            return
        try:
            self.settings = self.settings.with_lid_animation(
                LID_ANIMATION_CLOSED,
                program=closed.program,
                duration_seconds=closed.duration_seconds,
            )
            self.settings = self.settings.with_lid_animation(
                LID_ANIMATION_OPEN,
                program=opened.program,
                duration_seconds=opened.duration_seconds,
            )
            save_settings(self.settings)
        except Exception as exc:
            self.set_settings_message(f"Could not save lid animations: {exc}")
            self.settings = load_settings()
            self.refresh_settings_window()
            return

        self.set_settings_message("Lid animations saved.")
        self.refresh_settings_window()

    def reset_lid_animation(self, kind: str) -> None:
        animation = default_lid_animation(kind)
        try:
            self.settings = self.settings.with_lid_animation(
                kind,
                program=animation.program,
                duration_seconds=animation.duration_seconds,
            )
            save_settings(self.settings)
        except Exception as exc:
            self.set_settings_message(f"Could not reset {LID_ANIMATION_LABELS[kind]}: {exc}")
            self.settings = load_settings()
            self.refresh_settings_window()
            return

        self.set_settings_message(f"{LID_ANIMATION_LABELS[kind]} reset.")
        self.refresh_settings_window()

    def remove_remembered_device(self, device_id: str | None) -> None:
        if not device_id:
            return
        device = next(
            (
                entry
                for entry in self.status_bar_devices(remember=False)
                if entry.device_id == str(device_id)
            ),
            None,
        )
        try:
            self.settings = self.settings.without_device(str(device_id))
            save_settings(self.settings)
        except Exception as exc:
            self.set_settings_message(f"Could not remove device: {exc}")
            self.settings = load_settings()
            return

        self.reset_led_controllers_for_device(str(device_id))
        self.set_settings_message(f"{device.name if device else device_id}: removed.")
        self.refresh_settings_window()
        self.refresh_(None)

    def open_session(self, status: AgentStatus | object, action: str | None, *, remember: bool) -> None:
        if not isinstance(status, AgentStatus):
            return
        provider = status.provider.lower()
        requested_action = (
            action
            or self.settings.session_open_action(provider, status.origin)
            or default_session_open_action(status)
        )
        target = session_open_target(status, requested_action)
        if target is None:
            requested_action = default_session_open_action(status)
            target = session_open_target(status, requested_action)
        if target is None:
            self.set_settings_message(f"No open action available for {status.display_name}.")
            return

        kind, value = target
        if kind == "url":
            open_url(value)
        elif kind == "terminal":
            open_terminal_command(
                value,
                terminal_app=self.settings.session_terminal_app,
                custom_terminal_path=self.settings.custom_terminal_path,
                session_hints=terminal_session_hints(status),
            )
        else:
            self.set_settings_message(f"Unknown open action for {status.display_name}.")
            return

        if remember:
            try:
                self.settings = self.settings.with_session_open_action(
                    provider,
                    requested_action,
                    status.origin,
                )
                save_settings(self.settings)
            except Exception as exc:
                self.set_settings_message(f"Could not save open preference: {exc}")
                self.settings = load_settings()

    def close_status_menu(self) -> None:
        try:
            menu = self.status_item.menu()
            if menu is not None:
                menu.cancelTracking()
        except Exception:
            pass

    def set_battery_power_preview(self, enabled: bool) -> None:
        try:
            self.settings = self.settings.with_battery_power_change_preview(enabled=enabled)
            save_settings(self.settings)
        except Exception as exc:
            self.set_settings_message(f"Could not save settings: {exc}")
            self.settings = load_settings()
            self.refresh_settings_window()
            return

        self.set_settings_message(
            f"Battery power-change preview {'enabled' if enabled else 'disabled'}."
        )
        self.refresh_settings_window()
        self.refresh_(None)

    def read_battery_snapshot(self) -> BatterySnapshot | None:
        try:
            snapshot = read_battery_snapshot(
                full_charge_watts=self.settings.battery_full_charge_watts,
            )
        except Exception as exc:
            error = str(exc)
            if error != self.last_battery_error:
                log_status_bar(f"battery error: {error}")
            self.last_battery_error = error
            return None

        self.last_battery_error = None
        self.last_battery_snapshot = snapshot
        self.update_battery_power_preview(snapshot)
        return snapshot

    def update_battery_power_preview(self, snapshot: BatterySnapshot) -> None:
        plugged = snapshot.is_plugged
        if self.last_power_connected is not None and self.last_power_connected != plugged:
            if self.settings.battery_show_on_power_change:
                self.battery_preview_until = (
                    time.monotonic()
                    + self.settings.battery_power_change_preview_seconds
                )
                log_status_bar(
                    f"battery preview power={'plugged' if plugged else 'unplugged'}"
                )
        self.last_power_connected = plugged

    def active_led_display_kind(self, snapshot: BatterySnapshot | None) -> str:
        if self.settings.led_display == LED_DISPLAY_CUSTOM:
            return LED_DISPLAY_CUSTOM
        if self.settings.led_display == LED_DISPLAY_BATTERY:
            return LED_DISPLAY_BATTERY
        if snapshot is not None and time.monotonic() < self.battery_preview_until:
            return LED_DISPLAY_BATTERY
        return LED_DISPLAY_AGENT

    def reset_led_controllers_for_display_change(self) -> None:
        self.led_controller.reset()
        self.battery_led_controller.reset()
        for controller in self.agent_led_controllers_by_device.values():
            controller.reset()
        for controller in self.battery_led_controllers_by_device.values():
            controller.reset()
        self.last_led_display_kind_by_device.clear()
        self.last_led_error = None

    def reset_led_controllers_for_device(self, device_id: str) -> None:
        agent_controller = self.agent_led_controllers_by_device.get(device_id)
        if agent_controller is not None:
            agent_controller.reset()
        battery_controller = self.battery_led_controllers_by_device.get(device_id)
        if battery_controller is not None:
            battery_controller.reset()
        self.last_led_display_kind_by_device.pop(device_id, None)
        self.device_errors.pop(device_id, None)
        self.last_led_error = None

    def agent_controller_for_device(self, device: StatusBarDevice) -> AgentLedController:
        controller = self.agent_led_controllers_by_device.get(device.device_id)
        if controller is None:
            controller = AgentLedController(device_path=device.target)
            self.agent_led_controllers_by_device[device.device_id] = controller
        controller.device_path = device.target
        controller.brightness = device.brightness
        return controller

    def battery_controller_for_device(self, device: StatusBarDevice) -> BatteryLedController:
        controller = self.battery_led_controllers_by_device.get(device.device_id)
        if controller is None:
            controller = BatteryLedController(device_path=device.target)
            self.battery_led_controllers_by_device[device.device_id] = controller
        controller.device_path = device.target
        controller.brightness = device.brightness
        return controller

    def status_bar_devices(self, *, remember: bool = True) -> list[StatusBarDevice]:
        entries_by_id: dict[str, StatusBarDevice] = {}
        try:
            candidates = discover_devices()
        except Exception as exc:
            log_status_bar(f"device discovery error: {exc}")
            candidates = []

        for candidate in candidates:
            device_id = device_id_for_root(candidate.root)
            name = device_display_name(candidate.root.name)
            entries_by_id[device_id] = StatusBarDevice(
                device_id=device_id,
                name=name,
                root=candidate.root,
                target=candidate.target,
                connected=True,
                display=self.settings.display_for_device(device_id),
                brightness=self.settings.brightness_for_device(device_id),
                reason=candidate.reason,
            )

        for device in self.settings.devices:
            if device.device_id == VIRTUAL_DEVICE_ID:
                if (
                    SCREEN_BAR_FEATURE_ENABLED
                    and self.settings.virtual_status_device_enabled
                ):
                    entries_by_id[device.device_id] = StatusBarDevice(
                        device_id=device.device_id,
                        name=VIRTUAL_DEVICE_NAME,
                        root=Path(VIRTUAL_DEVICE_ID),
                        target=Path(VIRTUAL_DEVICE_ID),
                        connected=True,
                        display=device.led_display,
                        brightness=device.brightness,
                        reason="on-screen device",
                    )
                continue
            if device.device_id in entries_by_id:
                continue
            root = Path(device.path).expanduser()
            entries_by_id[device.device_id] = StatusBarDevice(
                device_id=device.device_id,
                name=device.name,
                root=root,
                target=target_from_device_path(root, DEFAULT_FILE_NAME),
                connected=False,
                display=device.led_display,
                brightness=device.brightness,
                reason="previously connected",
            )

        entries = sorted(
            entries_by_id.values(),
            key=lambda item: (not item.connected, normalized_device_name(item.name), str(item.root)),
        )
        entries = disambiguate_device_names(entries)
        if remember:
            self.remember_connected_devices(entries)
        return entries

    def observe_connected_devices(self) -> bool:
        devices = self.status_bar_devices()
        signature = device_connection_signature(devices)
        previous = self.last_connected_device_signature
        self.last_connected_device_signature = signature
        if previous is None or previous == signature:
            return False

        previous_by_id = {entry[0]: entry for entry in previous}
        current_by_id = {entry[0]: entry for entry in signature}
        reset_ids = {
            device_id
            for device_id, entry in current_by_id.items()
            if previous_by_id.get(device_id) != entry
        }
        reset_ids.update(set(previous_by_id) - set(current_by_id))
        for device_id in sorted(reset_ids):
            self.reset_led_controllers_for_device(device_id)

        connected_names = [
            device.name
            for device in devices
            if device.connected and device.device_id in reset_ids
        ]
        disconnected_ids = sorted(set(previous_by_id) - set(current_by_id))
        if connected_names:
            log_status_bar(f"device connected: {', '.join(connected_names)}")
        if disconnected_ids:
            log_status_bar(f"device disconnected: {', '.join(disconnected_ids)}")
        return True

    def remember_connected_devices(self, devices: list[StatusBarDevice]) -> None:
        settings = self.settings
        for device in devices:
            if not device.connected:
                continue
            settings = settings.with_remembered_device(
                device_id=device.device_id,
                name=device.name,
                path=str(device.root),
            )
        if settings != self.settings:
            self.settings = settings
            try:
                save_settings(self.settings)
            except Exception as exc:
                log_status_bar(f"device remember error: {exc}")

    def active_led_display_kind_for_device(
        self,
        device: StatusBarDevice,
        battery_snapshot: BatterySnapshot | None,
    ) -> str:
        if device.display == LED_DISPLAY_CUSTOM:
            return LED_DISPLAY_CUSTOM
        if device.display == LED_DISPLAY_BATTERY:
            return LED_DISPLAY_BATTERY
        if battery_snapshot is not None and time.monotonic() < self.battery_preview_until:
            return LED_DISPLAY_BATTERY
        return LED_DISPLAY_AGENT

    def sync_leds(
        self,
        mode: AgentMode,
        battery_snapshot: BatterySnapshot | None,
        display_kind: str,
    ) -> None:
        if not self.leds_enabled:
            return

        self.sync_virtual_status_device(mode, battery_snapshot)

        if time.monotonic() < self.led_animation_until_monotonic:
            return

        if self.led_sync_in_flight:
            return
        self.led_sync_in_flight = True
        thread = threading.Thread(
            target=self.sync_leds_worker,
            args=(mode, battery_snapshot, display_kind),
            daemon=True,
        )
        thread.start()

    def sync_virtual_status_device(
        self,
        mode: AgentMode,
        battery_snapshot: BatterySnapshot | None,
    ) -> None:
        if not SCREEN_BAR_FEATURE_ENABLED:
            self.virtual_status_device.hide()
            return
        if not self.settings.virtual_status_device_enabled:
            return
        device = next(
            (
                item for item in self.status_bar_devices(remember=False)
                if item.device_id == VIRTUAL_DEVICE_ID
            ),
            None,
        )
        if device is None:
            return
        display = self.active_led_display_kind_for_device(device, battery_snapshot)
        if display == LED_DISPLAY_CUSTOM:
            self.virtual_status_device.hide()
            return
        if display == LED_DISPLAY_BATTERY and battery_snapshot is not None:
            self.virtual_status_device.set_program(
                program_for_battery(
                    battery_snapshot,
                    led_count=8,
                    brightness=device.brightness,
                )
            )
        else:
            self.virtual_status_device.set_program(
                program_for_display_state(
                    display_state_for_mode(mode),
                    led_count=8,
                    brightness=device.brightness,
                )
            )

    def sync_leds_worker(
        self,
        mode: AgentMode,
        battery_snapshot: BatterySnapshot | None,
        display_kind: str,
    ) -> None:
        try:
            self.sync_leds_now(mode, battery_snapshot, display_kind)
        finally:
            self.led_sync_in_flight = False

    def sync_leds_now(
        self,
        mode: AgentMode,
        battery_snapshot: BatterySnapshot | None,
        display_kind: str,
    ) -> None:
        devices = [
            device for device in self.status_bar_devices()
            if device.connected and device.device_id != VIRTUAL_DEVICE_ID
        ]
        if not devices:
            self.ensure_device_selection()
            devices = [
                device
                for device in self.status_bar_devices(remember=False)
                if device.connected and device.device_id != VIRTUAL_DEVICE_ID
            ]
        if not devices:
            self.last_led_error = None
            return

        active_errors: dict[str, str] = {}
        for device in devices:
            device_display_kind = self.active_led_display_kind_for_device(
                device,
                battery_snapshot,
            )
            if self.last_led_display_kind_by_device.get(device.device_id) != device_display_kind:
                self.reset_led_controllers_for_device(device.device_id)
                self.last_led_display_kind_by_device[device.device_id] = device_display_kind

            if device_display_kind == LED_DISPLAY_CUSTOM:
                self.device_errors.pop(device.device_id, None)
                continue

            if device_display_kind == LED_DISPLAY_BATTERY and battery_snapshot is not None:
                result = self.battery_controller_for_device(device).sync_snapshot(battery_snapshot)
                label = (
                    f"{device.name} Battery {battery_snapshot.percent}% "
                    f"{format_watts(battery_snapshot.adapter_power)}"
                )
            else:
                result = self.agent_controller_for_device(device).sync_mode(mode)
                label = f"{device.name} {result.label}"

            if result.error:
                active_errors[device.device_id] = result.error
                previous_error = self.device_errors.get(device.device_id)
                if result.error != previous_error:
                    log_status_bar(f"led error {device.name}: {result.error}")
                continue

            self.device_errors.pop(device.device_id, None)
            if result.changed:
                target = result.target if result.target is not None else "-"
                log_status_bar(f"leds={label} target={target}")

        self.device_errors.update(active_errors)
        self.last_led_error = next(iter(self.device_errors.values()), None)

    def play_lid_animation(
        self,
        kind: str,
        *,
        animation: LedAnimationSetting | None = None,
    ) -> None:
        if not self.leds_enabled:
            return
        animation = animation or self.settings.lid_animation(kind)
        try:
            validate_lid_animation(animation)
        except DeviceWriteError as exc:
            self.set_settings_message(f"{LID_ANIMATION_LABELS[kind]} animation invalid: {exc}")
            return

        devices = [
            device for device in self.status_bar_devices()
            if (
                device.connected
                and device.device_id != VIRTUAL_DEVICE_ID
                and device.display != LED_DISPLAY_CUSTOM
            )
        ]
        if not devices:
            return

        self.led_animation_token += 1
        token = self.led_animation_token
        duration = animation.duration_seconds + LID_ANIMATION_RESTORE_FUDGE_SECONDS
        self.led_animation_until_monotonic = time.monotonic() + duration
        thread = threading.Thread(
            target=self.play_lid_animation_worker,
            args=(kind, animation, devices, token),
            daemon=True,
        )
        thread.start()

    def play_lid_animation_worker(
        self,
        kind: str,
        animation: LedAnimationSetting,
        devices: list[StatusBarDevice],
        token: int,
    ) -> None:
        label = LID_ANIMATION_LABELS[kind]
        for device in devices:
            try:
                program = program_for_lid_animation(animation, brightness=device.brightness)
                target = write_led_program(program, device_path=device.target)
                log_status_bar(f"animation={label} device={device.name} target={target}")
            except Exception as exc:
                log_status_bar(f"animation error {label} {device.name}: {exc}")

        time.sleep(animation.duration_seconds + LID_ANIMATION_RESTORE_FUDGE_SECONDS)
        self.performSelectorOnMainThread_withObject_waitUntilDone_(
            "restoreLedDisplay:",
            str(token),
            False,
        )

    @objc.IBAction
    def restoreLedDisplay_(self, token_value):
        restore_led_display(self, token_value)

    def connect_device(self) -> None:
        self.leds_enabled = True
        self.status_bar_devices()
        self.reset_led_controllers_for_display_change()
        self.last_led_error = None
        self.last_status_read_error = None
        log_status_bar("device connect requested")

    def disconnect_device(self) -> None:
        self.leds_enabled = False
        targets = self.current_led_targets()
        if not targets:
            targets = [
                device.target for device in self.status_bar_devices()
                if device.connected and device.device_id != VIRTUAL_DEVICE_ID
            ]
        for target in targets:
            try:
                result = write_mode_to_leds(AgentMode.IDLE_READY, device_path=target)
                log_status_bar(f"device disconnected target={result.target}")
            except Exception as exc:
                log_status_bar(f"device disconnect error: {exc}")
        self.reset_led_controllers_for_display_change()
        self.last_led_error = None
        self.last_status_read_error = None

    def device_connected(self) -> bool:
        connected = [
            device
            for device in self.status_bar_devices(remember=False)
            if device.connected
        ]
        return (
            self.leds_enabled
            and bool(self.current_led_targets() or connected)
            and self.last_led_error is None
            and not self.device_errors
        )

    def current_led_target(self) -> Path | None:
        targets = self.current_led_targets()
        return targets[0] if targets else None

    def current_led_targets(self) -> list[Path]:
        targets: list[Path] = []
        seen: set[str] = set()
        for controller in (
            *self.battery_led_controllers_by_device.values(),
            *self.agent_led_controllers_by_device.values(),
            self.battery_led_controller,
            self.led_controller,
        ):
            target = controller.last_target or controller.device_path
            if target is None:
                continue
            if not path_exists(Path(target).parent):
                continue
            key = str(target)
            if key in seen:
                continue
            targets.append(Path(target))
            seen.add(key)
        return targets

    def ensure_device_selection(self) -> None:
        selected = self.led_controller.device_path or self.battery_led_controller.device_path
        if selected is not None:
            target = target_from_device_path(
                Path(selected),
                DEFAULT_FILE_NAME,
            )
            if path_exists(target.parent):
                self.led_controller.device_path = target
                self.battery_led_controller.device_path = target
                return
            self.led_controller.device_path = None
            self.battery_led_controller.device_path = None
            self.reset_led_controllers_for_display_change()
            self.last_led_error = None

        try:
            candidates = discover_devices()
        except Exception as exc:
            log_status_bar(f"device selection error: {exc}")
            self.last_led_error = str(exc)
            return
        if not candidates:
            return
        target = preferred_status_bar_device(candidates).target
        self.led_controller.device_path = target
        self.battery_led_controller.device_path = target

    @objc.IBAction
    def pollLid_(self, _sender):
        try:
            closed = read_lid_closed()
        except Exception as exc:
            error = str(exc)
            if error != self.last_lid_error:
                self.last_lid_error = error
                log_status_bar(f"lid_state error: {error}")
            return

        if closed is None:
            return
        self.last_lid_error = None
        if self.last_lid_closed is None:
            self.last_lid_closed = closed
            return
        if closed == self.last_lid_closed:
            return

        self.last_lid_closed = closed
        kind = LID_ANIMATION_CLOSED if closed else LID_ANIMATION_OPEN
        log_status_bar(f"lid_state={'closed' if closed else 'open'}")
        self.play_lid_animation(kind)

    @objc.IBAction
    def pollDevices_(self, _sender):
        self.poll_devices_once()

    def poll_devices_once(self) -> None:
        if not self.observe_connected_devices():
            return
        if self.last_snapshot is not None:
            self.refresh_(None)

    def sync_keep_awake(self, mode: AgentMode) -> None:
        was_running = self.keep_awake.process_running()
        self.keep_awake.update(mode)
        is_running = self.keep_awake.process_running()
        if was_running != is_running:
            log_status_bar(f"keep_awake={'active' if is_running else 'released'}")
        if self.keep_awake.last_error != self.last_keep_awake_error:
            self.last_keep_awake_error = self.keep_awake.last_error
            if self.last_keep_awake_error:
                log_status_bar(f"keep_awake error: {self.last_keep_awake_error}")

        self.sync_closed_lid_awake()

        if not self.leds_enabled:
            return
        read_any = False
        for target in self.status_keepalive_targets():
            status_path = self.keep_awake.poke_status_file(target)
            if status_path is not None:
                read_any = True
                log_status_bar(f"sd_keepalive touch={status_path}")
        if not read_any and self.keep_awake.last_status_error != self.last_status_read_error:
            self.last_status_read_error = self.keep_awake.last_status_error
            if self.last_status_read_error:
                log_status_bar(f"sd_keepalive error: {self.last_status_read_error}")

    def sync_closed_lid_awake(self) -> None:
        was_active = self.closed_lid_awake.active()
        self.closed_lid_awake.set_use_system_disable(sleep_helper_installed())
        self.closed_lid_awake.update(
            self.settings.closed_lid_awake_policy,
            agents_active=self.keep_awake.holding_requested,
        )
        is_active = self.closed_lid_awake.active()
        if was_active != is_active:
            log_status_bar(
                f"closed_lid_awake={'active' if is_active else 'released'} "
                f"policy={self.settings.closed_lid_awake_policy}"
            )
        if self.closed_lid_awake.last_error != self.last_closed_lid_awake_error:
            self.last_closed_lid_awake_error = self.closed_lid_awake.last_error
            if self.last_closed_lid_awake_error:
                log_status_bar(
                    f"closed_lid_awake error: {self.last_closed_lid_awake_error}"
                )

    def status_keepalive_targets(self) -> list[Path]:
        targets = self.current_led_targets()
        if targets:
            return targets
        connected_targets = [
            device.target
            for device in self.status_bar_devices(remember=False)
            if device.connected and device.device_id != VIRTUAL_DEVICE_ID
        ]
        if connected_targets:
            return connected_targets
        return [MOUNT_ROOT / name / KEEPALIVE_FILE_NAME for name in STATUS_BAR_KEEPALIVE_VOLUME_NAMES]


def build_menu(snapshot, state: StatusBarState, target: StatusBarController) -> NSMenu:
    menu = NSMenu.alloc().init()

    menu.addItem_(disabled_menu_item("SidePulse"))
    menu.addItem_(NSMenuItem.separatorItem())

    menu.addItem_(disabled_menu_item("Agents"))

    statuses = recent_statuses(snapshot, target.settings)
    if not statuses:
        menu.addItem_(disabled_menu_item("No recent sessions"))
    else:
        collision_keys = session_title_collision_keys(statuses)
        for status in statuses:
            menu.addItem_(
                build_session_menu_item(
                    status,
                    snapshot.collected_at,
                    target,
                    disambiguate_title=session_title_collision_key(status) in collision_keys,
                )
            )

    menu.addItem_(NSMenuItem.separatorItem())
    menu.addItem_(disabled_menu_item("Devices"))
    devices = target.status_bar_devices()
    if devices:
        for device in devices:
            menu.addItem_(build_device_menu_item(device, target))
    else:
        menu.addItem_(disabled_menu_item("No devices"))
    if SCREEN_BAR_FEATURE_ENABLED and not target.settings.virtual_status_device_enabled:
        virtual_toggle = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Add Screen Bar",
            "toggleVirtualStatusDevice:",
            "",
        )
        virtual_toggle.setTarget_(target)
        menu.addItem_(virtual_toggle)

    menu.addItem_(NSMenuItem.separatorItem())
    keep_awake_toggle = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "Keep Awake While Agents Run",
        "toggleKeepAwake:",
        "",
    )
    keep_awake_toggle.setTarget_(target)
    keep_awake_toggle.setState_(1 if target.settings.keep_awake_enabled else 0)
    menu.addItem_(keep_awake_toggle)

    menu.addItem_(disabled_menu_item("Keep Awake With Lid Closed"))
    for policy in CLOSED_LID_AWAKE_CHOICES:
        menu.addItem_(build_closed_lid_awake_policy_item(policy, target))
    if target.closed_lid_awake.last_error:
        menu.addItem_(disabled_menu_item(f"Sleep warning: {target.closed_lid_awake.last_error}"))

    menu.addItem_(NSMenuItem.separatorItem())
    setup = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "Setup...",
        "openSetup:",
        "",
    )
    setup.setTarget_(target)
    menu.addItem_(setup)

    settings = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "Settings...",
        "openSettings:",
        ",",
    )
    settings.setTarget_(target)
    menu.addItem_(settings)

    quit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "Quit",
        "quit:",
        "q",
    )
    quit_item.setTarget_(target)
    menu.addItem_(quit_item)

    return menu


def build_closed_lid_awake_policy_item(policy: str, target: StatusBarController) -> NSMenuItem:
    item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        CLOSED_LID_AWAKE_LABELS[policy],
        "setClosedLidAwakePolicy:",
        "",
    )
    item.setTarget_(target)
    item.setRepresentedObject_(policy)
    item.setState_(1 if target.settings.closed_lid_awake_policy == policy else 0)
    return item


def build_device_menu_item(device: StatusBarDevice, target: StatusBarController) -> NSMenuItem:
    item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(device.name, None, "")
    item.setState_(1 if device.connected else 0)
    submenu = NSMenu.alloc().init()

    agent = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "Agent Status",
        "setDeviceDisplayAgent:",
        "",
    )
    agent.setTarget_(target)
    agent.setRepresentedObject_(device.device_id)
    agent.setState_(1 if device.display == LED_DISPLAY_AGENT else 0)
    submenu.addItem_(agent)

    battery = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "Battery Level",
        "setDeviceDisplayBattery:",
        "",
    )
    battery.setTarget_(target)
    battery.setRepresentedObject_(device.device_id)
    battery.setState_(1 if device.display == LED_DISPLAY_BATTERY else 0)
    submenu.addItem_(battery)

    custom = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "Manual",
        "setDeviceDisplayCustom:",
        "",
    )
    custom.setTarget_(target)
    custom.setRepresentedObject_(device.device_id)
    custom.setState_(1 if device.display == LED_DISPLAY_CUSTOM else 0)
    submenu.addItem_(custom)

    if device.device_id != VIRTUAL_DEVICE_ID:
        submenu.addItem_(NSMenuItem.separatorItem())
        submenu.addItem_(disabled_menu_item(f"Brightness {brightness_percent(device.brightness)}%"))
        submenu.addItem_(build_brightness_slider_item(device, target))

    if device.device_id == VIRTUAL_DEVICE_ID:
        submenu.addItem_(NSMenuItem.separatorItem())
        remove_screen_bar = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Remove Screen Bar",
            "toggleVirtualStatusDevice:",
            "",
        )
        remove_screen_bar.setTarget_(target)
        submenu.addItem_(remove_screen_bar)

    if not device.connected:
        submenu.addItem_(NSMenuItem.separatorItem())
        submenu.addItem_(disabled_menu_item("Not connected"))
        remove = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Remove",
            "removeRememberedDevice:",
            "",
        )
        remove.setTarget_(target)
        remove.setRepresentedObject_(device.device_id)
        submenu.addItem_(remove)

    item.setSubmenu_(submenu)
    return item


def build_brightness_slider_item(
    device: StatusBarDevice,
    target: StatusBarController,
) -> NSMenuItem:
    item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("", None, "")
    view = NSView.alloc().initWithFrame_(((0, 0), (230, 34)))
    slider = NSSlider.alloc().initWithFrame_(((14, 6), (202, 22)))
    slider.setMinValue_(0.0)
    slider.setMaxValue_(255.0)
    slider.setDoubleValue_(float(normalize_brightness(device.brightness)))
    slider.setContinuous_(False)
    slider.setTarget_(target)
    slider.setAction_("setDeviceBrightness:")
    slider.setIdentifier_(device.device_id)
    view.addSubview_(slider)
    item.setView_(view)
    return item


def build_setup_window(target: StatusBarController) -> NSWindow:
    width = 620
    height = 330
    style = (
        NSWindowStyleMaskTitled
        | NSWindowStyleMaskClosable
        | NSWindowStyleMaskMiniaturizable
    )
    window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        ((0, 0), (width, height)),
        style,
        NSBackingStoreBuffered,
        False,
    )
    window.setTitle_("SidePulse Setup")
    window.setReleasedWhenClosed_(False)
    window.center()
    content = window.contentView()

    add_label(content, "SidePulse", 24, 282, 180, 28)
    add_label(content, "Finish setup for this Mac.", 24, 254, 340, 22)

    launch = add_checkbox(
        content,
        "Run at Login",
        32,
        206,
        190,
        24,
        target,
        "",
    )
    add_label(content, "Start the menu-bar app automatically.", 56, 184, 300, 20)
    launch_status = add_label(content, "", 380, 206, 140, 22)

    eject_guard = add_checkbox(
        content,
        SD_EJECT_GUARD_DISPLAY_NAME,
        32,
        146,
        300,
        24,
        target,
        "",
    )
    add_label(content, "Keep SidePulse Pro/SidePulse Dot available after sleep.", 56, 124, 390, 20)
    eject_status = add_label(content, "", 398, 146, 88, 22)
    eject_uninstall = add_button(content, "Uninstall", 498, 142, 92, 28, target, "uninstallSdEjectGuard:")

    sleep_helper = add_checkbox(
        content,
        "Closed-Lid Sleep Prevention",
        32,
        86,
        260,
        24,
        target,
        "",
    )
    add_label(content, "Open a one-time administrator setup in Terminal.", 56, 64, 360, 20)
    sleep_status = add_label(content, "", 398, 86, 190, 22)

    message = add_label(content, "", 24, 36, width - 48, 20)

    add_button(content, "Skip", 392, 8, 84, 28, target, "skipFirstLaunchSetup:")
    add_button(content, "Set Up", 490, 8, 100, 28, target, "runFirstLaunchSetup:")

    target.setup_fields = {
        "launch_status": launch_status,
        "eject_status": eject_status,
        "sleep_status": sleep_status,
        "message": message,
    }
    target.setup_buttons = {
        "launch": launch,
        "eject_guard": eject_guard,
        "eject_guard_uninstall": eject_uninstall,
        "sleep_helper": sleep_helper,
    }
    return window


def choose_debug_export_path(format_name: str) -> Path | None:
    extension = "csv" if format_name == "csv" else "html"
    panel = NSSavePanel.savePanel()
    panel.setTitle_("Export SidePulse Debug Log")
    panel.setNameFieldStringValue_(f"sidepulse-agent-debug.{extension}")
    if hasattr(panel, "setAllowedFileTypes_"):
        panel.setAllowedFileTypes_([extension])
    if panel.runModal() != 1:
        return None
    url = panel.URL()
    if url is None:
        return None
    return Path(str(url.path()))


def choose_terminal_app() -> Path | None:
    panel = NSOpenPanel.openPanel()
    panel.setTitle_("Choose Terminal App")
    panel.setCanChooseFiles_(True)
    panel.setCanChooseDirectories_(False)
    panel.setAllowsMultipleSelection_(False)
    if hasattr(panel, "setAllowedFileTypes_"):
        panel.setAllowedFileTypes_(["app"])
    if panel.runModal() != 1:
        return None
    url = panel.URL()
    if url is None:
        return None
    return Path(str(url.path()))


def debug_log_status_text() -> str:
    path = default_status_audit_log_path()
    try:
        size = path.stat().st_size
    except OSError:
        return f"Log: {path} (empty)"
    return f"Log: {path} ({format_byte_count(size)})"


def format_byte_count(size: int) -> str:
    units = ("B", "KB", "MB", "GB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024


def build_settings_window(target: StatusBarController) -> NSWindow:
    width = 680
    height = 560
    style = (
        NSWindowStyleMaskTitled
        | NSWindowStyleMaskClosable
        | NSWindowStyleMaskMiniaturizable
    )
    window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        ((0, 0), (width, height)),
        style,
        NSBackingStoreBuffered,
        False,
    )
    window.setTitle_("SidePulse Settings")
    window.setReleasedWhenClosed_(False)
    window.center()
    content = window.contentView()

    tab_width = width - 40
    tab_height = height - 84
    tab_view = NSTabView.alloc().initWithFrame_(((20, 54), (tab_width, tab_height)))
    agents_tab = add_settings_tab(tab_view, "agents", "Agents", tab_width, tab_height)
    devices_tab = add_settings_tab(
        tab_view,
        "devices",
        "Devices & LEDs",
        tab_width,
        tab_height,
    )
    sleep_tab = add_settings_tab(
        tab_view,
        "lid",
        "Lid & Sleep",
        tab_width,
        tab_height,
    )
    behavior_tab = add_settings_tab(
        tab_view,
        "behavior",
        "Behavior",
        tab_width,
        tab_height,
    )
    diagnostics_tab = add_settings_tab(
        tab_view,
        "diagnostics",
        "Diagnostics",
        tab_width,
        tab_height,
    )
    content.addSubview_(tab_view)

    add_label(agents_tab, "Agent Hooks", 24, 398, 200, 24)
    add_label(agents_tab, "Codex", 32, 360, 80, 22)
    codex_status = add_label(agents_tab, "", 130, 360, 240, 22)
    add_button(agents_tab, "Install", 400, 356, 90, 28, target, "installCodexHooks:")
    add_button(agents_tab, "Uninstall", 500, 356, 100, 28, target, "uninstallCodexHooks:")

    add_label(agents_tab, "Claude", 32, 326, 80, 22)
    claude_status = add_label(agents_tab, "", 130, 326, 240, 22)
    add_button(agents_tab, "Install", 400, 322, 90, 28, target, "installClaudeHooks:")
    add_button(agents_tab, "Uninstall", 500, 322, 100, 28, target, "uninstallClaudeHooks:")

    add_label(agents_tab, "Grok", 32, 292, 80, 22)
    grok_status = add_label(agents_tab, "", 130, 292, 240, 22)
    add_button(agents_tab, "Install", 400, 288, 90, 28, target, "installGrokHooks:")
    add_button(agents_tab, "Uninstall", 500, 288, 100, 28, target, "uninstallGrokHooks:")

    add_separator(agents_tab, 24, 258, tab_width - 48)
    add_label(agents_tab, "Session Opening", 24, 224, 240, 24)
    add_label(agents_tab, "Codex", 32, 188, 100, 22)
    codex_opener = add_provider_opener_popup(agents_tab, "codex", 160, 186, target)
    add_label(agents_tab, "Claude", 32, 154, 100, 22)
    claude_opener = add_provider_opener_popup(agents_tab, "claude", 160, 152, target)
    add_label(agents_tab, "Grok Sessions", 32, 120, 120, 22)
    grok_opener = add_provider_opener_popup(agents_tab, "grok", 160, 118, target)

    add_label(agents_tab, "Terminal App", 376, 188, 120, 22)
    terminal_popup = add_terminal_popup(agents_tab, 376, 156, target)
    custom_terminal_path = add_label(agents_tab, "", 376, 128, 170, 22)
    add_button(agents_tab, "Choose...", 500, 92, 100, 28, target, "chooseSessionTerminal:")

    add_separator(agents_tab, 24, 80, tab_width - 48)
    add_label(agents_tab, "Transcript Monitoring", 24, 46, 240, 24)
    codex_transcripts = add_checkbox(
        agents_tab,
        "CLI fallback: Codex transcripts",
        32,
        14,
        260,
        24,
        target,
        "toggleCodexTranscripts:",
    )
    claude_transcripts = add_checkbox(
        agents_tab,
        "CLI fallback: Claude transcripts",
        312,
        14,
        260,
        24,
        target,
        "toggleClaudeTranscripts:",
    )

    add_label(devices_tab, "LED Display", 24, 398, 240, 24)
    battery_leds = add_checkbox(
        devices_tab,
        "Show battery on LEDs",
        32,
        356,
        260,
        24,
        target,
        "setBatteryLedDisplayFromCheckbox:",
    )
    battery_power_preview = add_checkbox(
        devices_tab,
        "Show battery for 7s on plug/unplug",
        32,
        318,
        320,
        24,
        target,
        "setBatteryPowerPreviewFromCheckbox:",
    )

    add_label(sleep_tab, "Lid Closed", 24, 398, 120, 22)
    add_label(sleep_tab, "Duration", 516, 398, 70, 22)
    closed_duration = add_editable_field(sleep_tab, "", 588, 396, 48, 24)
    closed_program = add_text_view(sleep_tab, "", 24, 276, 612, 110)
    add_button(sleep_tab, "Preview", 24, 236, 90, 28, target, "previewLidClosedAnimation:")
    add_button(sleep_tab, "Reset", 124, 236, 90, 28, target, "resetLidClosedAnimation:")

    add_separator(sleep_tab, 24, 210, tab_width - 48)
    add_label(sleep_tab, "Lid Open", 24, 176, 120, 22)
    add_label(sleep_tab, "Duration", 516, 176, 70, 22)
    open_duration = add_editable_field(sleep_tab, "", 588, 174, 48, 24)
    open_program = add_text_view(sleep_tab, "", 24, 54, 612, 110)
    add_button(sleep_tab, "Preview", 24, 14, 90, 28, target, "previewLidOpenAnimation:")
    add_button(sleep_tab, "Reset", 124, 14, 90, 28, target, "resetLidOpenAnimation:")
    add_button(sleep_tab, "Save Animations", 490, 14, 146, 28, target, "saveLidAnimations:")

    add_label(behavior_tab, "Agent List", 24, 398, 240, 24)
    add_label(behavior_tab, "Keep last 10 sessions for", 32, 356, 180, 22)
    retention_hours = add_editable_field(behavior_tab, "", 224, 354, 58, 24)
    add_label(behavior_tab, "hours", 292, 356, 60, 22)
    add_label(behavior_tab, "Idle timeout", 32, 316, 120, 22)
    idle_minutes = add_editable_field(behavior_tab, "", 224, 314, 58, 24)
    add_label(behavior_tab, "minutes", 292, 316, 80, 22)
    add_button(behavior_tab, "Save", 32, 266, 90, 28, target, "saveAgentListTiming:")

    add_label(diagnostics_tab, "Debug Log", 24, 398, 240, 24)
    debug_log_status = add_label(diagnostics_tab, "", 32, 360, 588, 22)
    add_button(diagnostics_tab, "Export CSV", 32, 318, 110, 28, target, "exportDebugCsv:")
    add_button(diagnostics_tab, "Export HTML", 152, 318, 120, 28, target, "exportDebugHtml:")
    add_separator(diagnostics_tab, 24, 280, tab_width - 48)
    add_label(diagnostics_tab, "Settings File", 24, 246, 240, 24)
    settings_path = add_label(diagnostics_tab, "", 32, 208, 588, 22)

    message = add_label(content, "", 24, 22, width - 48, 22)

    target.settings_fields = {
        "codex_hook_status": codex_status,
        "claude_hook_status": claude_status,
        "grok_hook_status": grok_status,
        "debug_log_status": debug_log_status,
        "codex_session_opener": codex_opener,
        "claude_session_opener": claude_opener,
        "grok_session_opener": grok_opener,
        "session_terminal": terminal_popup,
        "custom_terminal_path": custom_terminal_path,
        "closed_animation_program": closed_program,
        "closed_animation_duration": closed_duration,
        "open_animation_program": open_program,
        "open_animation_duration": open_duration,
        "recent_session_retention_hours": retention_hours,
        "idle_timeout_minutes": idle_minutes,
        "message": message,
        "settings_path": settings_path,
    }
    target.settings_buttons = {
        "codex_transcripts": codex_transcripts,
        "claude_transcripts": claude_transcripts,
        "battery_leds": battery_leds,
        "battery_power_preview": battery_power_preview,
    }
    return window


def add_settings_tab(tab_view, identifier: str, title: str, width: int, height: int):
    item = NSTabViewItem.alloc().initWithIdentifier_(identifier)
    item.setLabel_(title)
    view = NSView.alloc().initWithFrame_(((0, 0), (width, height - 34)))
    item.setView_(view)
    tab_view.addTabViewItem_(item)
    return view


def add_label(parent, text: str, x: int, y: int, width: int, height: int):
    label = NSTextField.alloc().initWithFrame_(((x, y), (width, height)))
    label.setStringValue_(text)
    label.setBezeled_(False)
    label.setDrawsBackground_(False)
    label.setEditable_(False)
    label.setSelectable_(False)
    parent.addSubview_(label)
    return label


def add_button(
    parent,
    title: str,
    x: int,
    y: int,
    width: int,
    height: int,
    target: StatusBarController,
    selector: str,
):
    button = NSButton.alloc().initWithFrame_(((x, y), (width, height)))
    button.setTitle_(title)
    button.setBezelStyle_(NSBezelStyleRounded)
    button.setTarget_(target)
    button.setAction_(selector)
    parent.addSubview_(button)
    return button


def add_checkbox(
    parent,
    title: str,
    x: int,
    y: int,
    width: int,
    height: int,
    target: StatusBarController | None,
    selector: str,
):
    checkbox = NSButton.alloc().initWithFrame_(((x, y), (width, height)))
    checkbox.setButtonType_(NSButtonTypeSwitch)
    checkbox.setTitle_(title)
    if target is not None:
        checkbox.setTarget_(target)
    if selector:
        checkbox.setAction_(selector)
    parent.addSubview_(checkbox)
    return checkbox


def add_provider_opener_popup(parent, provider: str, x: int, y: int, target):
    popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
        ((x, y), (180, 26)), False
    )
    popup.setTarget_(target)
    popup.setAction_("setProviderOpenPreference:")
    for action in provider_open_actions(provider):
        popup.addItemWithTitle_(provider_open_action_label(provider, action))
        popup.lastItem().setRepresentedObject_(
            {"provider": provider, "action": action}
        )
    parent.addSubview_(popup)
    return popup


def add_terminal_popup(parent, x: int, y: int, target):
    popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
        ((x, y), (150, 26)), False
    )
    popup.setTarget_(target)
    popup.setAction_("setSessionTerminal:")
    for terminal_app in TERMINAL_APP_CHOICES:
        popup.addItemWithTitle_(terminal_app_menu_label(terminal_app))
        item = popup.lastItem()
        item.setRepresentedObject_(terminal_app)
        item.setEnabled_(terminal_app_selectable(terminal_app))
    parent.addSubview_(popup)
    return popup


def provider_open_actions(provider: str) -> tuple[str, ...]:
    if provider == "claude":
        return (SESSION_OPEN_VSCODE, SESSION_OPEN_APP, SESSION_OPEN_TERMINAL)
    if provider == "codex":
        return (SESSION_OPEN_APP, SESSION_OPEN_TERMINAL)
    return (SESSION_OPEN_TERMINAL,)


def default_provider_open_action(provider: str) -> str:
    if provider == "claude":
        return SESSION_OPEN_VSCODE
    if provider == "codex":
        return SESSION_OPEN_APP
    return SESSION_OPEN_TERMINAL


def provider_open_action_label(provider: str, action: str, settings=None) -> str:
    if action == SESSION_OPEN_VSCODE:
        return "VS Code"
    if action == SESSION_OPEN_TERMINAL:
        return "Resume in Terminal"
    return {"codex": "Codex", "claude": "Claude"}.get(provider, "App")


def refresh_provider_opener_popup(popup, provider: str, action: str, settings) -> None:
    for index in range(popup.numberOfItems()):
        item = popup.itemAtIndex_(index)
        payload = item.representedObject()
        if isinstance(payload, dict) and payload.get("action") == action:
            item.setTitle_(
                provider_open_action_label(provider, str(payload.get("action")), settings)
            )
            popup.selectItemAtIndex_(index)
        elif isinstance(payload, dict):
            item.setTitle_(
                provider_open_action_label(
                    provider,
                    str(payload.get("action")),
                    settings,
                )
            )


def refresh_terminal_popup(popup, terminal_app: str) -> None:
    selected = normalize_terminal_app(terminal_app)
    for index in range(popup.numberOfItems()):
        item = popup.itemAtIndex_(index)
        item_terminal = item.representedObject()
        if not isinstance(item_terminal, str):
            continue
        item.setTitle_(terminal_app_menu_label(item_terminal))
        item.setEnabled_(terminal_app_selectable(item_terminal) or item_terminal == selected)
        if item_terminal == selected:
            popup.selectItemAtIndex_(index)


def terminal_app_label(terminal_app: str) -> str:
    return TERMINAL_APP_LABELS.get(normalize_terminal_app(terminal_app), "Terminal")


def resume_terminal_label(settings=None) -> str:
    return f"Resume in {resume_terminal_app_label(settings)}"


def resume_terminal_app_label(settings=None) -> str:
    if settings is None:
        return "Terminal"
    terminal = normalize_terminal_app(getattr(settings, "session_terminal_app", None))
    if terminal == TERMINAL_APP_CUSTOM:
        path = str(getattr(settings, "custom_terminal_path", "") or "").strip()
        if path:
            return custom_terminal_app_label(path)
        return "Custom Terminal"
    return terminal_app_label(terminal)


def custom_terminal_app_label(path: str) -> str:
    name = Path(path).name
    if name.endswith(".app"):
        return name[:-4] or "Custom Terminal"
    return name or "Custom Terminal"


def terminal_app_menu_label(terminal_app: str) -> str:
    terminal = normalize_terminal_app(terminal_app)
    label = terminal_app_label(terminal)
    if terminal == TERMINAL_APP_CUSTOM:
        return label
    if terminal_app_installed(terminal):
        return f"{label} (Installed)"
    return f"{label} (Not Installed)"


def terminal_app_selectable(terminal_app: str) -> bool:
    terminal = normalize_terminal_app(terminal_app)
    return terminal == TERMINAL_APP_CUSTOM or terminal_app_installed(terminal)


def terminal_settings_detail(settings) -> str:
    if settings.session_terminal_app == TERMINAL_APP_CUSTOM:
        return settings.custom_terminal_path or "No custom terminal selected"
    installed = [
        terminal_app_label(spec.key)
        for spec in TERMINAL_APP_SPECS
        if terminal_app_installed(spec.key)
    ]
    return "Installed: " + ", ".join(installed or ["none detected"])


def terminal_app_installed(
    terminal_app: str,
    *,
    app_dirs: tuple[Path, ...] | None = None,
) -> bool:
    if normalize_terminal_app(terminal_app) == TERMINAL_APP_TERMINAL:
        return True
    return installed_terminal_app_path(terminal_app, app_dirs=app_dirs) is not None


def installed_terminal_app_path(
    terminal_app: str,
    *,
    app_dirs: tuple[Path, ...] | None = None,
) -> Path | None:
    terminal = normalize_terminal_app(terminal_app)
    spec = terminal_app_spec(terminal)
    if spec is None:
        return None

    for path_text in spec.system_paths:
        path = Path(path_text).expanduser()
        if path.exists():
            return path

    for directory in app_dirs or default_terminal_app_dirs():
        for app_name in spec.app_names:
            path = directory.expanduser() / app_name
            if path.exists():
                return path
    return None


def terminal_app_spec(terminal_app: str) -> TerminalAppSpec | None:
    terminal = normalize_terminal_app(terminal_app)
    for spec in TERMINAL_APP_SPECS:
        if spec.key == terminal:
            return spec
    return None


def default_terminal_app_dirs() -> tuple[Path, ...]:
    return (Path("/Applications"), Path.home() / "Applications")


def add_editable_field(parent, text: str, x: int, y: int, width: int, height: int):
    field = NSTextField.alloc().initWithFrame_(((x, y), (width, height)))
    field.setStringValue_(text)
    field.setEditable_(True)
    field.setSelectable_(True)
    parent.addSubview_(field)
    return field


def add_text_view(parent, text: str, x: int, y: int, width: int, height: int):
    scroll = NSScrollView.alloc().initWithFrame_(((x, y), (width, height)))
    scroll.setHasVerticalScroller_(True)
    scroll.setHasHorizontalScroller_(False)
    text_view = NSTextView.alloc().initWithFrame_(((0, 0), (width, height)))
    text_view.setString_(text)
    text_view.setVerticallyResizable_(True)
    text_view.setHorizontallyResizable_(False)
    try:
        text_view.setFont_(NSFont.monospacedSystemFontOfSize_weight_(11.0, 0.0))
    except Exception:
        pass
    scroll.setDocumentView_(text_view)
    parent.addSubview_(scroll)
    return text_view


def add_separator(parent, x: int, y: int, width: int):
    separator = NSTextField.alloc().initWithFrame_(((x, y), (width, 1)))
    separator.setStringValue_("")
    separator.setBezeled_(False)
    separator.setEditable_(False)
    separator.setDrawsBackground_(True)
    parent.addSubview_(separator)
    return separator


def set_field_value(field, value: str) -> None:
    if field is not None:
        field.setStringValue_(value)


def set_text_control_value(control, value: str) -> None:
    if control is None:
        return
    if hasattr(control, "setString_"):
        control.setString_(value)
    else:
        control.setStringValue_(value)


def text_control_value(control) -> str:
    if control is None:
        return ""
    if hasattr(control, "string"):
        return str(control.string())
    return str(control.stringValue())


def set_checkbox_state(button, enabled: bool) -> None:
    if button is not None:
        button.setState_(NSOnState if enabled else NSOffState)


def checkbox_is_on(button) -> bool:
    return button is not None and button.state() == NSOnState


def should_show_setup_window(settings) -> bool:
    return not getattr(settings, "setup_screen_completed", False)


def open_terminal_setup_command(command: str, *, filename: str = "install-sleep-helper.command") -> Path:
    state_dir = default_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    script_path = state_dir / filename
    script = "\n".join(
        [
            "#!/bin/zsh",
            "clear",
            'echo "SidePulse Sleep Prevention Setup"',
            'echo ""',
            'echo "macOS may ask for your administrator password."',
            'echo ""',
            command,
            "status=$?",
            'echo ""',
            'if [ "$status" -eq 0 ]; then',
            '  echo "Done. You can close this window."',
            "else",
            '  echo "Setup failed. Leave this window open if you want to inspect it."',
            "fi",
            'echo ""',
            'read -k 1 "?Press any key to close this window. "',
            "",
        ]
    )
    script_path.write_text(script, encoding="utf-8")
    script_path.chmod(0o700)
    subprocess.Popen(
        ["/usr/bin/open", str(script_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return script_path


def validate_lid_animation(animation: LedAnimationSetting) -> None:
    program = normalize_led_text(animation.program)
    validate_led_text(program)
    validate_led_text(apply_brightness(program, 1))
    normalize_animation_duration(animation.duration_seconds)


def program_for_lid_animation(
    animation: LedAnimationSetting,
    *,
    brightness: int | float = 255,
) -> str:
    validate_lid_animation(animation)
    return apply_brightness(normalize_led_text(animation.program), brightness)


def restore_led_display(target, token_value) -> None:
    try:
        token = int(str(token_value))
    except ValueError:
        token = target.led_animation_token
    if token != target.led_animation_token:
        return
    target.led_animation_until_monotonic = 0.0
    target.reset_led_controllers_for_display_change()
    if target.last_snapshot is not None:
        target.sync_leds(
            target.last_snapshot.aggregate.mode,
            target.last_battery_snapshot,
            target.active_led_display_kind(target.last_battery_snapshot),
        )
    else:
        target.refresh_(None)


def hook_status_text(config: ProviderConfig) -> str:
    if not config.exists:
        return f"Not installed - config will be created at {config.config_path}"
    if config.hook_events:
        event_count = len(config.hook_events)
        suffix = "event" if event_count == 1 else "events"
        return f"Installed ({event_count} {suffix})"
    return "Not installed"


def device_id_for_root(root: Path) -> str:
    return str(root.expanduser())


def device_connection_signature(
    devices: list[StatusBarDevice],
) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        sorted(
            (
                device.device_id,
                str(device.target),
                device_mount_key(device.root),
            )
            for device in devices
            if device.connected
        )
    )


def device_mount_key(root: Path) -> str:
    try:
        stat = root.stat()
    except OSError:
        return "missing"
    return f"{stat.st_dev}:{stat.st_ino}"


def device_display_name(name: str) -> str:
    normalized = normalized_device_name(name)
    if "sidepulsedot" in normalized:
        return "SidePulse Dot"
    if "sidepulsepro" in normalized:
        return "SidePulse Pro"
    return name or "SidePulse Device"


def device_display_label(display: str) -> str:
    if display == LED_DISPLAY_BATTERY:
        return "Battery Level"
    if display == LED_DISPLAY_CUSTOM:
        return "Manual"
    return "Agent Status"


def disabled_menu_item(title: str) -> NSMenuItem:
    item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
    item.setEnabled_(False)
    return item


def disambiguate_device_names(devices: list[StatusBarDevice]) -> list[StatusBarDevice]:
    counts: dict[str, int] = {}
    for device in devices:
        counts[device.name] = counts.get(device.name, 0) + 1
    if all(count == 1 for count in counts.values()):
        return devices

    result: list[StatusBarDevice] = []
    for device in devices:
        if counts.get(device.name, 0) <= 1:
            result.append(device)
            continue
        suffix = duplicate_device_suffix(device)
        result.append(
            StatusBarDevice(
                device_id=device.device_id,
                name=f"{device.name} {suffix}" if suffix else device.name,
                root=device.root,
                target=device.target,
                connected=device.connected,
                display=device.display,
                brightness=device.brightness,
                reason=device.reason,
            )
        )
    return result


def duplicate_device_suffix(device: StatusBarDevice) -> str:
    root_name = device.root.name
    normalized_root = normalized_device_name(root_name)
    normalized_name = normalized_device_name(device.name)
    if normalized_root.startswith(normalized_name):
        suffix = root_name[len(device.name) :].strip()
        return suffix
    return root_name


def preferred_status_bar_device(candidates: list[DeviceCandidate]) -> DeviceCandidate:
    return sorted(candidates, key=status_bar_device_sort_key)[0]


def status_bar_device_sort_key(candidate: DeviceCandidate) -> tuple[int, str]:
    name = normalized_device_name(candidate.root.name)
    for index, hint in enumerate(STATUS_BAR_DEVICE_PRIORITY):
        if hint in name:
            return (index, candidate.root.name.lower())
    return (len(STATUS_BAR_DEVICE_PRIORITY), candidate.root.name.lower())


def build_session_menu_item(
    status: AgentStatus,
    now: datetime,
    target: StatusBarController,
    *,
    width: float | None = None,
    disambiguate_title: bool = False,
) -> NSMenuItem:
    item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        native_session_menu_title(status, disambiguate=disambiguate_title),
        "openSessionPrimary:",
        "",
    )
    item.setTarget_(target)
    item.setRepresentedObject_(status)
    image = session_row_icon_for_status(status)
    if image is not None:
        item.setImage_(image)
    return item


def native_session_menu_title(status: AgentStatus, *, disambiguate: bool = False) -> str:
    title, project = session_title_parts(status)
    if disambiguate and status.session_id:
        title = f"{title} ({status.session_id[:8]})"
    parts = [title]
    if project:
        parts.append(project)
    return "  ".join(parts)


def build_session_options_menu(
    status: AgentStatus,
    now: datetime,
    target: StatusBarController,
) -> NSMenu:
    menu = NSMenu.alloc().init()
    menu.addItem_(disabled_menu_item(flatten_menu_title(menu_title_for_status(status, now))))
    menu.addItem_(disabled_menu_item(session_detail_for_status(status, now)))
    menu.addItem_(NSMenuItem.separatorItem())

    if getattr(target, "settings", None) is not None:
        selected = target.settings.session_open_action(status.provider, status.origin)
    else:
        selected = None
    selected = selected or default_session_open_action(status)
    for action in available_session_open_actions(status):
        add_session_open_action_item(
            menu,
            session_open_action_title(status, action, getattr(target, "settings", None)),
            status,
            action,
            target,
            selected=action == selected,
        )
    return menu


def add_session_open_action_item(
    menu: NSMenu,
    title: str,
    status: AgentStatus,
    action: str,
    target: StatusBarController,
    *,
    selected: bool,
) -> None:
    item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        title,
        "openSessionWithAction:",
        "",
    )
    item.setTarget_(target)
    item.setRepresentedObject_({"status": status, "action": action})
    item.setState_(1 if selected else 0)
    menu.addItem_(item)


def session_open_action_title(status: AgentStatus, action: str, settings=None) -> str:
    if action == SESSION_OPEN_TERMINAL:
        return resume_terminal_label(settings)
    return session_open_action_label(status, action)


def session_row_icon_for_status(status: AgentStatus):
    status_icon = status_icon_for_status(status)
    origin_icon = session_origin_icon_for_status(status)
    if origin_icon is None:
        return status_icon
    if status_icon is None:
        return origin_icon
    return horizontal_icon_pair(status_icon, origin_icon)


def status_icon_for_status(status: AgentStatus):
    state = state_for_mode(status.mode)
    return image_for_symbol(state.symbol, state.label)


def session_origin_icon_for_status(status: AgentStatus):
    provider_icon = provider_icon_for_status(status)
    host_icon = host_icon_for_origin(status.origin)
    if host_icon is None:
        return provider_icon
    return composite_app_icons(host_icon, provider_icon)


def provider_icon_for_status(status: AgentStatus):
    return provider_icon_for_provider(status.provider)


def provider_icon_for_provider(provider: str):
    provider = provider.lower()
    if provider == "codex":
        for path in ("/Applications/Codex.app", "/Applications/ChatGPT.app"):
            image = app_icon(path)
            if image is not None:
                return image
        return image_for_symbol("sparkles", "Codex")
    if provider == "claude":
        image = app_icon("/Applications/Claude.app")
        if image is not None:
            return image
        return image_for_symbol("brain.head.profile", "Claude")
    if provider == "grok":
        image = app_icon("/Applications/Grok.app")
        if image is not None:
            return image
        return grok_badge_icon()
    return image_for_symbol("terminal", provider.title() or "Agent")


def host_icon_for_origin(origin: str | None):
    normalized = normalized_origin_text(origin)
    if not normalized:
        return None
    if "vs code" in normalized or "vscode" in normalized or "visual studio code" in normalized:
        return first_app_icon(
            (
                "/Applications/Visual Studio Code.app",
                "/Applications/Visual Studio Code - Insiders.app",
            )
        ) or image_for_symbol("chevron.left.forwardslash.chevron.right", "VS Code")
    if "cursor" in normalized:
        return first_app_icon(("/Applications/Cursor.app",)) or image_for_symbol(
            "cursorarrow",
            "Cursor",
        )
    if "windsurf" in normalized:
        return first_app_icon(("/Applications/Windsurf.app",)) or image_for_symbol(
            "wind",
            "Windsurf",
        )
    if any(token in normalized for token in ("cli", "terminal", "command line")):
        return first_app_icon(
            (
                "/System/Applications/Utilities/Terminal.app",
                "/Applications/iTerm.app",
                "/Applications/iTerm2.app",
            )
        ) or image_for_symbol("terminal", "Terminal")
    if "transcript" in normalized:
        return image_for_symbol("doc.text", "Transcript")
    return None


def first_app_icon(paths: tuple[str, ...]):
    for path in paths:
        image = app_icon(path)
        if image is not None:
            return image
    return None


def composite_app_icons(host_icon, provider_icon):
    if provider_icon is None:
        return host_icon
    if host_icon is None:
        return provider_icon

    image = NSImage.alloc().initWithSize_((24.0, 18.0))
    try:
        image.lockFocus()
        host_icon.drawInRect_fromRect_operation_fraction_(
            ((0.0, 1.0), (15.5, 15.5)),
            image_source_rect(host_icon),
            NSCompositingOperationSourceOver,
            1.0,
        )
        provider_icon.drawInRect_fromRect_operation_fraction_(
            ((8.0, 1.0), (15.5, 15.5)),
            image_source_rect(provider_icon),
            NSCompositingOperationSourceOver,
            1.0,
        )
    finally:
        image.unlockFocus()
    image.setSize_((24.0, 18.0))
    return image


def horizontal_icon_pair(left_icon, right_icon):
    left_width = 15.5
    right_width = float(right_icon.size().width)
    width = left_width + 3.0 + right_width
    height = 18.0
    image = NSImage.alloc().initWithSize_((width, height))
    try:
        image.lockFocus()
        left_icon.drawInRect_fromRect_operation_fraction_(
            ((0.0, 1.25), (left_width, left_width)),
            image_source_rect(left_icon),
            NSCompositingOperationSourceOver,
            1.0,
        )
        right_icon.drawInRect_fromRect_operation_fraction_(
            ((left_width + 3.0, 0.0), (right_width, height)),
            image_source_rect(right_icon),
            NSCompositingOperationSourceOver,
            1.0,
        )
    finally:
        image.unlockFocus()
    image.setSize_((width, height))
    return image


def image_source_rect(image) -> tuple[tuple[float, float], tuple[float, float]]:
    size = image.size()
    return ((0.0, 0.0), (float(size.width), float(size.height)))


def normalized_origin_text(origin: str | None) -> str:
    return " ".join(str(origin or "").strip().lower().replace("-", " ").split())


_grok_badge_icon = None


def grok_badge_icon():
    global _grok_badge_icon
    if _grok_badge_icon is not None:
        return _grok_badge_icon

    image = NSImage.alloc().initWithSize_((18, 18))
    try:
        image.lockFocus()
        NSColor.colorWithCalibratedRed_green_blue_alpha_(0.05, 0.055, 0.065, 1.0).set()
        badge = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            ((1.0, 1.0), (16.0, 16.0)),
            4.0,
            4.0,
        )
        badge.fill()

        attrs = {
            NSFontAttributeName: NSFont.boldSystemFontOfSize_(12.0),
            NSForegroundColorAttributeName: NSColor.whiteColor(),
        }
        NSString.stringWithString_("G").drawInRect_withAttributes_(
            ((4.6, 1.8), (10.0, 14.0)),
            attrs,
        )
    finally:
        image.unlockFocus()
    image.setSize_((18, 18))
    _grok_badge_icon = image
    return image


def app_icon(path: str):
    if not Path(path).exists():
        return None
    image = NSWorkspace.sharedWorkspace().iconForFile_(path)
    if image is not None:
        image.setSize_((18, 18))
    return image


def flatten_menu_title(title: str) -> str:
    return " · ".join(part.strip() for part in title.splitlines() if part.strip())


def add_action_item(
    menu: NSMenu,
    title: str,
    selector: str,
    represented_object: str | None,
    target: StatusBarController,
) -> None:
    item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        title if represented_object else f"{title} unavailable",
        selector,
        "",
    )
    item.setTarget_(target)
    item.setEnabled_(represented_object is not None)
    if represented_object is not None:
        item.setRepresentedObject_(represented_object)
    menu.addItem_(item)


def build_error_menu(exc: Exception) -> NSMenu:
    menu = NSMenu.alloc().init()
    item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        f"Agent monitor error: {exc}",
        None,
        "",
    )
    item.setEnabled_(False)
    menu.addItem_(item)
    return menu


def recent_statuses(
    snapshot,
    settings=None,
    *,
    limit: int = STATUS_BAR_SESSION_HISTORY_LIMIT,
) -> list[AgentStatus]:
    statuses = coalesced_menu_statuses(menu_statuses(snapshot, settings))
    statuses.sort(key=lambda status: (status.priority, -status.updated_at.timestamp()))
    return statuses[:limit]


def menu_statuses(snapshot, settings=None) -> tuple[AgentStatus, ...]:
    statuses = list(snapshot.statuses)
    now = snapshot.collected_at
    retention_seconds = (
        settings.recent_session_retention_seconds
        if settings is not None
        else DEFAULT_RECENT_SESSION_RETENTION_SECONDS
    )
    statuses.extend(
        status
        for status in getattr(snapshot, "stale_statuses", ())
        if status.mode == AgentMode.COMPLETED
        and status.age_seconds(now) <= retention_seconds
    )
    return tuple(statuses)


def session_title_collision_keys(statuses: list[AgentStatus]) -> set[tuple[str, str]]:
    counts: dict[tuple[str, str], int] = {}
    for status in statuses:
        key = session_title_collision_key(status)
        counts[key] = counts.get(key, 0) + 1
    return {key for key, count in counts.items() if count > 1}


def session_title_collision_key(status: AgentStatus) -> tuple[str, str]:
    return (
        status.provider.lower(),
        normalized_menu_part(native_session_menu_title(status)),
    )


def coalesced_menu_statuses(statuses) -> list[AgentStatus]:
    return coalesce_statuses_by_key(statuses, menu_session_coalesce_key)


def coalesce_statuses_by_key(statuses, key_fn) -> list[AgentStatus]:
    by_key: dict[tuple[str, ...], AgentStatus] = {}
    passthrough: list[AgentStatus] = []
    for status in statuses:
        key = key_fn(status)
        if key is None:
            passthrough.append(status)
            continue
        previous = by_key.get(key)
        if previous is None or menu_status_sort_key(status) < menu_status_sort_key(previous):
            by_key[key] = status
    return [*by_key.values(), *passthrough]


def menu_session_coalesce_key(status: AgentStatus) -> tuple[str, ...] | None:
    if not status.session_id:
        return None
    return ("session", status.provider.lower(), status.session_id)


def menu_status_sort_key(status: AgentStatus) -> tuple[int, int, float]:
    subagent_penalty = 1 if ":agent:" in status.agent_id else 0
    return (status.priority, subagent_penalty, -status.updated_at.timestamp())


def menu_title_for_status(status: AgentStatus, now: datetime) -> str:
    state = state_for_mode(status.mode)
    title, project = session_title_parts(status)
    origin = menu_origin_label(status)
    if origin:
        first_line = f"{state.label}  {origin}  {title}"
    else:
        first_line = f"{state.label}  {title}"
    if project:
        return f"{first_line}\n{project}"
    return first_line


def session_detail_for_status(status: AgentStatus, now: datetime) -> str:
    state = state_for_mode(status.mode)
    age = format_age(status.age_seconds(now))
    details = [state.label, age]
    if status.origin:
        details.append(status.origin)
    if status.tool_name:
        details.append(status.tool_name)
    return " · ".join(details)


def menu_origin_label(status: AgentStatus) -> str | None:
    if not status.origin:
        return None
    return status.origin


def primary_session_open_action(status: AgentStatus | object) -> str | None:
    if not isinstance(status, AgentStatus):
        return None
    return default_session_open_action(status)


def session_title_parts(status: AgentStatus) -> tuple[str, str | None]:
    project = project_name_from_cwd(status.cwd)
    title = strip_session_short_id(status.display_name, status.session_id)
    if project and title.startswith(f"{project}: "):
        title = title[len(project) + 2 :]
    elif ": " in title:
        maybe_project, maybe_title = title.split(": ", 1)
        if not project:
            project = maybe_project
        title = maybe_title
    if project and normalized_menu_part(project) == normalized_menu_part(title):
        project = None
    return title or status.display_name, project


def normalized_menu_part(text: str) -> str:
    return " ".join(text.replace("_", " ").replace("-", " ").split()).casefold()


def strip_session_short_id(display_name: str, session_id: str | None) -> str:
    text = display_name.strip()
    if session_id:
        suffix = f" ({session_id[:8]})"
        if text.endswith(suffix):
            return text[: -len(suffix)].strip()
    if text.endswith(")") and " (" in text:
        prefix, suffix = text.rsplit(" (", 1)
        token = suffix[:-1]
        if 6 <= len(token) <= 12 and all(char.isalnum() or char == "-" for char in token):
            return prefix.strip()
    return text


def project_name_from_cwd(cwd: str | None) -> str | None:
    if not cwd:
        return None
    path = Path(cwd)
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return candidate.name or str(candidate)
    return path.name or cwd


def image_for_symbol(symbol: str, description: str):
    try:
        image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            symbol,
            description,
        )
        if image is not None:
            image.setTemplate_(True)
    except Exception:
        image = None
    return image


def log_status_bar(message: str) -> None:
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    print(f"{timestamp} {message}", flush=True)


def compact_path(path: str, max_len: int = 48) -> str:
    if len(path) <= max_len:
        return path
    keep = max_len - 1
    return "." + path[-keep:]


def format_age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{rest:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def open_url(url: str) -> None:
    ns_url = NSURL.URLWithString_(url)
    if ns_url is not None:
        NSWorkspace.sharedWorkspace().openURL_(ns_url)


def open_terminal_command(
    command: str,
    *,
    terminal_app: str = TERMINAL_APP_TERMINAL,
    custom_terminal_path: str = "",
    session_hints: TerminalSessionHints | None = None,
) -> None:
    terminal = normalize_terminal_app(terminal_app)
    if session_hints is not None and focus_existing_terminal_session(
        terminal,
        custom_terminal_path,
        session_hints,
    ):
        return

    launch_command = command_for_terminal_session(command, session_hints)
    if (
        terminal not in {TERMINAL_APP_TERMINAL, TERMINAL_APP_CUSTOM}
        and not terminal_app_installed(terminal)
    ):
        open_macos_terminal_command(launch_command)
        return
    if terminal == TERMINAL_APP_ITERM:
        open_iterm_command(launch_command)
        return
    if terminal == TERMINAL_APP_GHOSTTY:
        open_ghostty_command(launch_command, session_hints)
        return
    if terminal == TERMINAL_APP_WARP:
        open_command_script_in_terminal_app(launch_command, terminal_open_target(terminal))
        return
    if terminal == TERMINAL_APP_KITTY:
        open_exec_terminal_command(launch_command, terminal_open_target(terminal))
        return
    if terminal == TERMINAL_APP_WEZTERM:
        open_wezterm_command(launch_command, terminal_open_target(terminal))
        return
    if terminal == TERMINAL_APP_ALACRITTY:
        open_exec_terminal_command(launch_command, terminal_open_target(terminal))
        return
    if terminal == TERMINAL_APP_CUSTOM:
        open_custom_terminal_command(launch_command, custom_terminal_path)
        return
    open_macos_terminal_command(launch_command)


def terminal_session_hints(status: AgentStatus) -> TerminalSessionHints:
    title, project = session_title_parts(status)
    label_parts = [provider_label(status.provider)]
    if title:
        label_parts.append(title)
    if project:
        label_parts.append(project)
    return TerminalSessionHints(
        provider=status.provider.lower(),
        session_id=status.session_id or "",
        cwd=status.cwd or "",
        title=" ".join(part for part in label_parts if part).strip(),
        match_title=title,
    )


def command_for_terminal_session(
    command: str,
    hints: TerminalSessionHints | None,
) -> str:
    if hints is None:
        return command
    title = terminal_session_title(hints)
    if not title:
        return command
    return f"{terminal_title_command(title)}; {command}"


def terminal_session_title(hints: TerminalSessionHints) -> str:
    parts = ["SidePulse"]
    if hints.title:
        parts.append(hints.title)
    if hints.session_id:
        parts.append(f"({hints.session_id[:8]})")
    return " ".join(parts)


def terminal_title_command(title: str) -> str:
    return "printf '\\033]0;%s\\007' " + shlex.quote(title)


def terminal_session_match_terms(hints: TerminalSessionHints) -> tuple[str, ...]:
    terms: list[str] = []
    for term in (
        hints.session_id,
        hints.cwd,
        hints.title,
        terminal_session_title(hints),
    ):
        term = str(term or "").strip()
        if term and term not in terms:
            terms.append(term)
    return tuple(terms)


def focus_existing_terminal_session(
    terminal_app: str,
    custom_terminal_path: str,
    hints: TerminalSessionHints,
) -> bool:
    terminal = normalize_terminal_app(terminal_app)
    if terminal == TERMINAL_APP_CUSTOM:
        terminal = terminal_app_from_custom_path(custom_terminal_path)
    terms = terminal_session_match_terms(hints)
    if not terms:
        return False
    if terminal == TERMINAL_APP_TERMINAL:
        return focus_macos_terminal_session(terms)
    if terminal == TERMINAL_APP_ITERM:
        return focus_iterm_session(terms)
    if terminal == TERMINAL_APP_GHOSTTY:
        return focus_ghostty_session(hints)
    return False


def terminal_app_from_custom_path(path: str) -> str:
    app_name = Path(path.strip()).name.casefold()
    if app_name == "terminal.app":
        return TERMINAL_APP_TERMINAL
    if app_name in {"iterm.app", "iterm2.app"} or "iterm" in app_name:
        return TERMINAL_APP_ITERM
    if "ghost" in app_name:
        return TERMINAL_APP_GHOSTTY
    if "warp" in app_name:
        return TERMINAL_APP_WARP
    if "kitty" in app_name:
        return TERMINAL_APP_KITTY
    if "wezterm" in app_name:
        return TERMINAL_APP_WEZTERM
    if "alacritty" in app_name:
        return TERMINAL_APP_ALACRITTY
    return TERMINAL_APP_CUSTOM


def focus_macos_terminal_session(terms: tuple[str, ...]) -> bool:
    condition = applescript_contains_any(("tabTitle", "tabText"), terms)
    script = "\n".join(
        [
            'tell application "Terminal"',
            "  repeat with windowRef in windows",
            "    repeat with tabRef in tabs of windowRef",
            '      set tabTitle to ""',
            '      set tabText to ""',
            "      try",
            "        set tabTitle to custom title of tabRef",
            "      end try",
            "      try",
            "        set tabText to contents of tabRef",
            "      end try",
            f"      if {condition} then",
            "        set selected tab of windowRef to tabRef",
            "        set index of windowRef to 1",
            "        activate",
            '        return "1"',
            "      end if",
            "    end repeat",
            "  end repeat",
            "end tell",
            'return "0"',
        ]
    )
    return run_osascript_bool(script)


def focus_iterm_session(terms: tuple[str, ...]) -> bool:
    condition = applescript_contains_any(("sessionName", "sessionText"), terms)
    script = "\n".join(
        [
            'tell application "iTerm"',
            "  repeat with windowRef in windows",
            "    repeat with tabRef in tabs of windowRef",
            "      repeat with sessionRef in sessions of tabRef",
            '        set sessionName to ""',
            '        set sessionText to ""',
            "        try",
            "          set sessionName to name of sessionRef",
            "        end try",
            "        try",
            "          set sessionText to contents of sessionRef",
            "        end try",
            f"        if {condition} then",
            "          tell windowRef to select tabRef",
            "          select sessionRef",
            "          set index of windowRef to 1",
            "          activate",
            '          return "1"',
            "        end if",
            "      end repeat",
            "    end repeat",
            "  end repeat",
            "end tell",
            'return "0"',
        ]
    )
    return run_osascript_bool(script)


def focus_ghostty_session(hints: TerminalSessionHints) -> bool:
    app = ghostty_application()
    if app is None or not sb_bool(app, "isRunning"):
        return False
    marker_terms = terminal_session_marker_match_terms(hints)
    if marker_terms and focus_matching_ghostty_terminal(
        app,
        marker_terms,
        include_working_directory=False,
    ):
        return True
    title_terms = terminal_session_title_match_terms(hints)
    return bool(
        title_terms
        and focus_matching_ghostty_terminal(
            app,
            title_terms,
            include_working_directory=False,
            require_unique=True,
        )
    )


def terminal_session_marker_match_terms(hints: TerminalSessionHints) -> tuple[str, ...]:
    terms: list[str] = []
    for term in (
        hints.session_id,
        hints.session_id[:8] if hints.session_id else "",
        terminal_session_title(hints),
        hints.title,
    ):
        term = str(term or "").strip()
        if term and term not in terms:
            terms.append(term)
    return tuple(terms)


def terminal_session_title_match_terms(hints: TerminalSessionHints) -> tuple[str, ...]:
    title = str(hints.match_title or "").strip()
    return (title,) if title else ()


def focus_matching_ghostty_terminal(
    app,
    terms: tuple[str, ...],
    *,
    include_working_directory: bool,
    require_unique: bool = False,
) -> bool:
    normalized_terms = tuple(normalize_match_text(term) for term in terms if term)
    if not normalized_terms:
        return False
    matches: list[tuple[object, object, object]] = []
    for window in sb_elements(sb_call(app, "windows")):
        window_name = sb_text(window, "name")
        for tab in sb_elements(sb_call(window, "tabs")):
            tab_name = sb_text(tab, "name")
            for terminal in sb_elements(sb_call(tab, "terminals")):
                values = [window_name, tab_name, sb_text(terminal, "name")]
                if include_working_directory:
                    values.append(sb_text(terminal, "workingDirectory"))
                if values_match_terms(values, normalized_terms):
                    matches.append((window, tab, terminal))
    if require_unique and len(matches) != 1:
        return False
    if not matches:
        return False
    window, tab, terminal = matches[0]
    focus_ghostty_terminal(window, tab, terminal, app)
    return True


def focus_ghostty_terminal(window, tab, terminal, app) -> None:
    sb_call(tab, "selectTab")
    sb_call(terminal, "focus")
    sb_call(window, "activateWindow")
    sb_call(app, "activate")


def values_match_terms(values: list[str], normalized_terms: tuple[str, ...]) -> bool:
    normalized_values = [normalize_match_text(value) for value in values if value]
    return any(
        term in value
        for term in normalized_terms
        for value in normalized_values
    )


def normalize_match_text(value: str) -> str:
    return str(value or "").strip().casefold()


def applescript_contains_any(variable_names: tuple[str, ...], terms: tuple[str, ...]) -> str:
    clauses = [
        f"{variable_name} contains {applescript_quote(term)}"
        for variable_name in variable_names
        for term in terms
    ]
    return " or ".join(clauses) if clauses else "false"


def open_macos_terminal_command(command: str) -> None:
    script = "\n".join(
        [
            'tell application "Terminal"',
            "  activate",
            f"  do script {applescript_quote(command)}",
            "end tell",
        ]
    )
    run_osascript(script)


def open_iterm_command(command: str) -> None:
    script = "\n".join(
        [
            'tell application "iTerm"',
            "  activate",
            "  if (count of windows) = 0 then",
            f"    create window with default profile command {applescript_quote(command)}",
            "  else",
            "    tell current window",
            f"      create tab with default profile command {applescript_quote(command)}",
            "    end tell",
            "  end if",
            "end tell",
        ]
    )
    run_osascript(script)


def open_ghostty_command(
    command: str,
    hints: TerminalSessionHints | None = None,
) -> None:
    if open_ghostty_command_with_scripting_bridge(command, hints):
        return
    open_exec_terminal_command(command, terminal_open_target(TERMINAL_APP_GHOSTTY))


def open_ghostty_command_with_scripting_bridge(
    command: str,
    hints: TerminalSessionHints | None = None,
) -> bool:
    app = ghostty_application()
    if app is None:
        return False
    try:
        config = {
            "command": f"/bin/zsh -lc {shlex.quote(command)}",
            "waitAfterCommand": True,
        }
        if hints is not None and hints.cwd:
            config["workingDirectory"] = hints.cwd
        surface_config = app.newSurfaceConfigurationFrom_(config)
        if sb_bool(app, "isRunning"):
            if hints is not None:
                app.newWindowWithConfiguration_(surface_config)
            else:
                window = sb_call(app, "frontWindow")
                if window is None:
                    app.newWindowWithConfiguration_(surface_config)
                else:
                    app.newTabIn_withConfiguration_(window, surface_config)
        else:
            app.newWindowWithConfiguration_(surface_config)
        sb_call(app, "activate")
    except Exception:
        return False
    return True


def ghostty_application():
    if SBApplication is None:
        return None
    app_path = installed_terminal_app_path(TERMINAL_APP_GHOSTTY)
    if app_path is not None:
        try:
            return SBApplication.applicationWithURL_(
                NSURL.fileURLWithPath_(str(app_path))
            )
        except Exception:
            pass
    try:
        return SBApplication.applicationWithBundleIdentifier_("com.mitchellh.ghostty")
    except Exception:
        return None


def sb_elements(collection) -> tuple[object, ...]:
    if collection is None:
        return ()
    try:
        return tuple(collection)
    except TypeError:
        pass
    try:
        return tuple(collection.objectAtIndex_(index) for index in range(collection.count()))
    except Exception:
        return ()


def sb_call(obj, method: str):
    try:
        return getattr(obj, method)()
    except Exception:
        return None


def sb_text(obj, method: str) -> str:
    value = sb_call(obj, method)
    return str(value or "")


def sb_bool(obj, method: str) -> bool:
    return bool(sb_call(obj, method))


def open_custom_terminal_command(command: str, custom_terminal_path: str) -> None:
    path = custom_terminal_path.strip()
    if not path:
        open_macos_terminal_command(command)
        return
    if path.startswith("/") and not Path(path).expanduser().exists():
        open_macos_terminal_command(command)
        return

    app_name = Path(path).name.casefold()
    if app_name == "terminal.app":
        open_macos_terminal_command(command)
        return
    if app_name in {"iterm.app", "iterm2.app"} or "iterm" in app_name:
        open_iterm_command(command)
        return
    if "warp" in app_name:
        open_command_script_in_terminal_app(command, path)
        return
    if "wezterm" in app_name:
        open_wezterm_command(command, path)
        return
    open_exec_terminal_command(command, path)


def terminal_open_target(terminal_app: str) -> str:
    terminal = normalize_terminal_app(terminal_app)
    installed = installed_terminal_app_path(terminal)
    if installed is not None:
        return str(installed)
    return terminal_app_label(terminal)


def open_wezterm_command(command: str, app: str) -> None:
    open_terminal_app_with_args(
        app,
        ["start", "--new-tab", "--", "/bin/zsh", "-lc", command],
    )


def open_exec_terminal_command(command: str, app: str) -> None:
    open_terminal_app_with_args(app, ["-e", "/bin/zsh", "-lc", command])


def open_terminal_app_with_args(app: str, app_args: list[str]) -> None:
    args = ["/usr/bin/open", "-n"]
    if app.startswith("/") or app.endswith(".app"):
        args.append(app)
    else:
        args.extend(["-a", app])
    args.extend(["--args", *app_args])
    subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def open_command_script_in_terminal_app(command: str, app: str) -> None:
    script_path = write_resume_command_script(command)
    args = ["/usr/bin/open"]
    if app.startswith("/") or app.endswith(".app"):
        args.extend(["-a", app, str(script_path)])
    else:
        args.extend(["-a", app, str(script_path)])
    subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def write_resume_command_script(command: str) -> Path:
    state_dir = default_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    script_path = state_dir / "resume-session.command"
    script = "\n".join(
        [
            "#!/bin/zsh",
            command,
            "",
        ]
    )
    script_path.write_text(script, encoding="utf-8")
    script_path.chmod(0o700)
    return script_path


def run_osascript(script: str) -> None:
    subprocess.Popen(
        ["/usr/bin/osascript", "-e", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def run_osascript_bool(script: str) -> bool:
    try:
        result = subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1.5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip() == "1"


def applescript_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def run_status_bar() -> None:
    app = NSApplication.sharedApplication()
    controller = StatusBarController.alloc().init()
    app.setDelegate_(controller)
    app.run()


def main() -> int:
    run_status_bar()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
