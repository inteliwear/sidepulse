"""Daemon-owned Mac sleep policy, power observations and status history.

This module has no AppKit dependency and never needs a running tray.
"""
from __future__ import annotations

import sys
import time
from dataclasses import asdict

from .audit import append_status_history_record, status_history_record
from .battery import BatterySnapshot, read_battery_snapshot
from .device_writer import MOUNT_ROOT, discover_devices
from .keep_awake import KEEPALIVE_FILE_NAME, KeepAwakeController
from .lid_sleep import (ClosedLidAwakeController, MacSleepSnapshot, external_display_is_active,
    read_lid_closed, read_mac_sleep_snapshot, run_pmset_displaysleepnow,
    sleep_helper_installed)
from .models import AgentMode
from .settings import (load_settings, SLEEP_PREVENTION_ALWAYS, SLEEP_PREVENTION_AGENTS,
    SLEEP_PREVENTION_NEVER, LID_ANIMATION_CLOSED, LID_ANIMATION_OPEN)

HISTORY_SAMPLE_SECONDS = 15.0
MAC_SLEEP_POLL_SECONDS = 60.0
SYSTEM_POLL_ERROR_BACKOFF_SECONDS = 30.0


def log_runtime(message):
    print(f"sidepulse service: {message}", file=sys.stderr)


def display_status_for_mode(mode):
    if mode in {AgentMode.WAITING_FOR_INPUT, AgentMode.BLOCKED_ERROR}: return 'Ask'
    if mode in {AgentMode.WORKING, AgentMode.TOOL_RUNNING, AgentMode.LONG_TASK_PROGRESS}: return 'Working'
    if mode == AgentMode.COMPLETED: return 'Done'
    return 'Idle'


def awake_policy_should_hold(policy: str, *, agents_active: bool) -> bool:
    if policy == SLEEP_PREVENTION_ALWAYS:
        return True
    if policy == SLEEP_PREVENTION_AGENTS:
        return agents_active
    return False


def lid_closed_led_state_should_hold(
    *,
    lid_closed: bool | None,
    sleep_prevention_policy: str,
    agents_active: bool | None,
    battery_safeguard_active: bool = False,
) -> bool:
    """Keep the shutdown frame while the closed Mac is allowed to sleep."""
    if lid_closed is not True or agents_active is None:
        return False
    sleep_prevention_requested = awake_policy_should_hold(
        sleep_prevention_policy,
        agents_active=agents_active,
    ) and not battery_safeguard_active
    return not sleep_prevention_requested


def lid_close_animation_should_run(
    *,
    sleep_prevention_policy: str,
    agents_active: bool | None,
) -> bool:
    """Run the physical close animation only when the Mac may go to sleep."""
    if sleep_prevention_policy == SLEEP_PREVENTION_ALWAYS:
        return False
    if sleep_prevention_policy == SLEEP_PREVENTION_AGENTS:
        return agents_active is False
    return True


def closed_lid_display_sleep_should_request(
    *,
    lid_closed: bool | None,
    external_display_active: bool | None,
    system_sleep_disabled: bool,
) -> bool:
    """Only sleep displays when it cannot affect an external monitor."""
    return (
        lid_closed is True
        and external_display_active is False
        and system_sleep_disabled
    )


def sleep_prevention_battery_safeguard(
    snapshot: BatterySnapshot | None,
    threshold_percent: float,
) -> tuple[bool, str]:
    threshold = max(0.0, min(100.0, float(threshold_percent)))
    threshold_text = format_percent_value(threshold)
    if threshold <= 0:
        return False, "disabled"
    if snapshot is None:
        return False, f"battery unknown, threshold {threshold_text}"
    if not snapshot.battery_present:
        return False, f"no battery, threshold {threshold_text}"
    battery_text = format_percent_value(snapshot.percent)
    if snapshot.is_plugged:
        return False, f"battery {battery_text}, plugged in, threshold {threshold_text}"
    if snapshot.percent < threshold:
        return True, f"battery {battery_text}, threshold {threshold_text}"
    return False, f"battery {battery_text}, threshold {threshold_text}"


def format_percent_value(value: float | int) -> str:
    number = float(value)
    if number.is_integer():
        return f"{int(number)}%"
    return f"{number:.1f}%"


class DaemonRuntime:
    def __init__(self, service):
        self.service = service
        self.is_mac = sys.platform == 'darwin'
        self.keep_awake = KeepAwakeController()
        self.closed_lid_awake = ClosedLidAwakeController(
            use_system_disable=sleep_helper_installed(), use_caffeinate=False)
        self.settings = None
        self.last_snapshot = None
        self.last_battery_snapshot = None
        self.last_battery_error = None
        self.last_power_connected = None
        self.last_lid_closed = None
        self.last_lid_error = None
        self.last_mac_sleep_snapshot = None
        self.last_mac_sleep_error = None
        self.last_status_history_error = None
        self.last_keep_awake_error = None
        self.last_closed_lid_awake_error = None
        self.last_status_read_error = None
        self.battery_sleep_safeguard_active = False
        self.battery_sleep_safeguard_reason = ''
        self.agent_awake_last_mode = None
        self.agent_awake_grace_until_monotonic = None
        self.agent_awake_requested = False
        self.closed_lid_display_sleep_requested = False
        self.lid_closed_led_hold_active = False
        self.next_lid_poll = self.next_sleep_poll = self.next_history_sample = 0.0
        self.devices = []
        self.view = {}

    @property
    def leds_enabled(self):
        return self.service.outputs_enabled

    @property
    def battery_preview_until(self):
        return self.service.battery_preview_until

    @battery_preview_until.setter
    def battery_preview_until(self, value):
        self.service.battery_preview_until = value
    def poll(self):
        self.settings = load_settings()
        self.last_snapshot = self.service.monitor.snapshot()
        now = time.monotonic()
        battery = None
        if self.is_mac:
            try:
                battery = read_battery_snapshot(full_charge_watts=self.settings.battery_full_charge_watts)
                self.last_battery_error = None
            except Exception as exc:
                self.last_battery_error = str(exc)
        self.last_battery_snapshot = battery
        if battery is not None:
            self.update_battery_power_preview(battery)
        try:
            self.devices = discover_devices()
        except Exception as exc:
            # A missing device must not stop Mac sleep policy or history.
            self.devices = []
            log_runtime(f'device discovery failed: {exc}')
        if self.is_mac:
            # Only the runtime loop mutates sleep controllers, never socket threads.
            self.keep_awake.set_enabled(self.service.keep_awake_enabled)
            self.sync_keep_awake(self.last_snapshot.aggregate.mode, battery)
        if self.is_mac and now >= self.next_lid_poll:
            self.next_lid_poll = now + 1
            try:
                self.accept_lid_state(read_lid_closed())
                self.last_lid_error = None
            except Exception as exc:
                self.last_lid_error = str(exc)
                self.next_lid_poll = now + SYSTEM_POLL_ERROR_BACKOFF_SECONDS
        if self.is_mac and now >= self.next_sleep_poll:
            try:
                self.last_mac_sleep_snapshot = read_mac_sleep_snapshot()
                self.last_mac_sleep_error = self.last_mac_sleep_snapshot.error or None
            except Exception as exc:
                self.last_mac_sleep_error = str(exc)
            self.next_sleep_poll = now + (SYSTEM_POLL_ERROR_BACKOFF_SECONDS
                if self.last_mac_sleep_error else MAC_SLEEP_POLL_SECONDS)
        if now >= self.next_history_sample:
            self.next_history_sample = now + HISTORY_SAMPLE_SECONDS
            mode = self.last_snapshot.aggregate.mode
            self.record_status_history(mode, battery, self.last_mac_sleep_snapshot)
        self.view = self.snapshot()
    def snapshot(self):
        return {
            'battery': asdict(self.last_battery_snapshot) if self.last_battery_snapshot else None,
            'battery_error': self.last_battery_error,
            'battery_preview_until': self.battery_preview_until,
            'lid_closed': self.last_lid_closed,
            'lid_error': self.last_lid_error,
            'sleep_error': self.last_mac_sleep_error,
            'history_error': self.last_status_history_error,
            'keep_awake_enabled': self.keep_awake.enabled,
            'keep_awake_active': self.keep_awake.process_running(),
            'closed_lid_awake_active': self.closed_lid_awake.active(),
            'battery_safeguard_active': self.battery_sleep_safeguard_active,
        }
    def close(self):
        try:
            self.closed_lid_awake.release()
        finally:
            self.keep_awake.release()
    def status_keepalive_targets(self):
        if self.devices:
            return [device.target for device in self.devices]
        return [MOUNT_ROOT / name / KEEPALIVE_FILE_NAME
            for name in ('SidePulsePro', 'SidePulseDot', 'PulseDot')]
    def accept_lid_state(self, closed):
        if closed is None:
            return
        closed = bool(closed)
        previous = self.last_lid_closed
        self.last_lid_closed = closed
        self.sync_closed_lid_awake()
        if previous is None or previous == closed:
            return
        kind = LID_ANIMATION_CLOSED if closed else LID_ANIMATION_OPEN
        if closed:
            if not self.should_run_lid_close_animation():
                self.lid_closed_led_hold_active = False
                return
            self.lid_closed_led_hold_active = self.should_hold_lid_closed_led_state()
        else:
            self.lid_closed_led_hold_active = False
        self.play_lid_animation(kind)
    def play_lid_animation(self, kind):
        self.service.play_lid_animation(kind)
    def update_battery_power_preview(self, snapshot: BatterySnapshot) -> None:
        plugged = snapshot.is_plugged
        if self.last_power_connected is not None and self.last_power_connected != plugged:
            if self.settings.battery_show_on_power_change:
                self.battery_preview_until = (
                    time.monotonic()
                    + self.settings.battery_power_change_preview_seconds
                )
                log_runtime(
                    f"battery preview power={'plugged' if plugged else 'unplugged'}"
                )
        self.last_power_connected = plugged

    def record_status_history(
        self,
        mode: AgentMode,
        battery_snapshot: BatterySnapshot | None,
        mac_sleep_snapshot: MacSleepSnapshot | None,
    ) -> None:
        try:
            record = status_history_record(
                agent_mode=mode.value,
                display_status=display_status_for_mode(mode),
                battery=battery_snapshot,
                mac_sleep=mac_sleep_snapshot,
                lid_closed=self.last_lid_closed,
                keep_awake_requested=bool(self.keep_awake.holding_requested),
                keep_awake_active=bool(self.keep_awake.process_running()),
                sleep_prevention_policy=self.settings.sleep_prevention_policy,
                sleep_prevention_battery_safeguard_active=bool(
                    self.battery_sleep_safeguard_active
                ),
                sleep_prevention_min_battery_percent=(
                    self.settings.sleep_prevention_min_battery_percent
                ),
                closed_lid_awake_requested=bool(self.closed_lid_awake.last_requested),
                closed_lid_awake_active=bool(self.closed_lid_awake.active()),
            )
            append_status_history_record(record)
        except Exception as exc:
            error = str(exc)
            if error != self.last_status_history_error:
                self.last_status_history_error = error
                log_runtime(f"status_history error: {error}")
            return

        self.last_status_history_error = None

    def should_hold_lid_closed_led_state(self) -> bool:
        agents_active = None
        if self.last_snapshot is not None:
            agents_active = self.last_snapshot.aggregate.active_count > 0
        return lid_closed_led_state_should_hold(
            lid_closed=self.last_lid_closed,
            sleep_prevention_policy=self.settings.sleep_prevention_policy,
            agents_active=agents_active,
            battery_safeguard_active=self.battery_sleep_safeguard_active,
        )

    def should_run_lid_close_animation(self) -> bool:
        agents_active = None
        if self.last_snapshot is not None:
            agents_active = self.last_snapshot.aggregate.active_count > 0
        return lid_close_animation_should_run(
            sleep_prevention_policy=self.settings.sleep_prevention_policy,
            agents_active=agents_active,
        )

    def sync_keep_awake(
        self,
        mode: AgentMode,
        battery_snapshot: BatterySnapshot | None = None,
    ) -> None:
        agents_active = self.update_agent_awake_request(mode)
        safeguard_active, safeguard_reason = sleep_prevention_battery_safeguard(
            battery_snapshot,
            self.settings.sleep_prevention_min_battery_percent,
        )
        if safeguard_active != self.battery_sleep_safeguard_active:
            log_runtime(
                "sleep_battery_safeguard="
                f"{'active' if safeguard_active else 'released'} "
                f"{safeguard_reason}"
            )
        self.battery_sleep_safeguard_active = safeguard_active
        self.battery_sleep_safeguard_reason = safeguard_reason
        policy = self.settings.sleep_prevention_policy
        should_hold = awake_policy_should_hold(
            policy,
            agents_active=agents_active,
        ) and not safeguard_active
        was_running = self.keep_awake.process_running()
        self.keep_awake.update_requested(should_hold, mode=mode)
        is_running = self.keep_awake.process_running()
        if was_running != is_running:
            log_runtime(f"keep_awake={'active' if is_running else 'released'}")
        if self.keep_awake.last_error != self.last_keep_awake_error:
            self.last_keep_awake_error = self.keep_awake.last_error
            if self.last_keep_awake_error:
                log_runtime(f"keep_awake error: {self.last_keep_awake_error}")

        self.sync_closed_lid_awake(agents_active=agents_active)

        if not self.leds_enabled:
            return
        read_any = False
        for target in self.status_keepalive_targets():
            status_path = self.keep_awake.poke_status_file(target)
            if status_path is not None:
                read_any = True
                log_runtime(f"sd_keepalive read={status_path}")
        if not read_any and self.keep_awake.last_status_error != self.last_status_read_error:
            self.last_status_read_error = self.keep_awake.last_status_error
            if self.last_status_read_error:
                log_runtime(f"sd_keepalive error: {self.last_status_read_error}")

    def update_agent_awake_request(self, mode: AgentMode) -> bool:
        current = time.monotonic()
        if mode in {
            AgentMode.WORKING,
            AgentMode.TOOL_RUNNING,
            AgentMode.LONG_TASK_PROGRESS,
        }:
            self.agent_awake_grace_until_monotonic = None
            requested = True
        elif mode in {
            AgentMode.COMPLETED,
            AgentMode.WAITING_FOR_INPUT,
            AgentMode.BLOCKED_ERROR,
        }:
            if (
                self.agent_awake_last_mode != mode
                or self.agent_awake_grace_until_monotonic is None
            ):
                self.agent_awake_grace_until_monotonic = (
                    current + self.keep_awake.grace_seconds
                )
            requested = current < self.agent_awake_grace_until_monotonic
        else:
            requested = (
                self.agent_awake_grace_until_monotonic is not None
                and current < self.agent_awake_grace_until_monotonic
            )

        self.agent_awake_last_mode = mode
        self.agent_awake_requested = requested
        return requested

    def sync_closed_lid_awake(self, *, agents_active: bool | None = None) -> None:
        was_active = self.closed_lid_awake.active()
        external_display_active = external_display_is_active()
        battery_snapshot = getattr(self, "last_battery_snapshot", None)
        clamshell_eligible = bool(
            external_display_active is True
            and battery_snapshot is not None
            and battery_snapshot.is_plugged
        )
        self.closed_lid_awake.set_use_system_disable(
            sleep_helper_installed() and not clamshell_eligible
        )
        policy = self.settings.sleep_prevention_policy
        if self.battery_sleep_safeguard_active or clamshell_eligible:
            policy = SLEEP_PREVENTION_NEVER
        self.closed_lid_awake.update(
            policy,
            agents_active=self.agent_awake_requested if agents_active is None else agents_active,
        )
        is_active = self.closed_lid_awake.active()
        if was_active != is_active:
            log_runtime(
                f"closed_lid_awake={'active' if is_active else 'released'} "
                f"policy={self.settings.sleep_prevention_policy}"
            )
        if self.closed_lid_awake.last_error != self.last_closed_lid_awake_error:
            self.last_closed_lid_awake_error = self.closed_lid_awake.last_error
            if self.last_closed_lid_awake_error:
                log_runtime(
                    f"closed_lid_awake error: {self.last_closed_lid_awake_error}"
                )

        should_sleep_display = closed_lid_display_sleep_should_request(
            lid_closed=getattr(self, "last_lid_closed", None),
            external_display_active=external_display_active,
            system_sleep_disabled=self.closed_lid_awake.system_disable_active(),
        )
        if not should_sleep_display:
            self.closed_lid_display_sleep_requested = False
            return
        if getattr(self, "closed_lid_display_sleep_requested", False):
            return
        try:
            run_pmset_displaysleepnow()
            self.closed_lid_display_sleep_requested = True
            log_runtime("lid_display_sleep=requested")
        except Exception as exc:
            self.closed_lid_display_sleep_requested = False
            log_runtime(f"lid_display_sleep error: {exc}")
