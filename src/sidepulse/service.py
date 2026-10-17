from __future__ import annotations

import signal
import time
import math
from pathlib import Path
import sys
import threading

from .battery import program_for_battery, read_battery_snapshot
from .device_writer import DeviceWriteError, discover_devices, write_led_program
from .led_status import led_count_for_target, program_for_agent_mode
from .relay import (
    RelayConfig,
    listen_for_status_updates,
    load_relay_config,
)
from .settings import LED_DISPLAY_BATTERY, LED_DISPLAY_CUSTOM, load_settings


SERVICE_POLL_SECONDS = 1.0


class SidePulseService:
    def __init__(self, *, poll_seconds: float = SERVICE_POLL_SECONDS) -> None:
        self.poll_seconds = poll_seconds
        self.stop_event = threading.Event()
        from .program_status import ProgramState
        from .program_transport import ProgramServer, ProgramRelay
        from .program_presentation import ProgramMonitor
        self.program_state = ProgramState()
        self.program_server = ProgramServer(self.program_state, on_command=self.handle_command)
        self.monitor = ProgramMonitor(self.program_state)
        self.program_relay = ProgramRelay(self.program_state)
        self.receiver_stop: threading.Event | None = None
        self.receiver_thread: threading.Thread | None = None
        self.receiver_signature: tuple[str, str] | None = None
        self.last_program_by_target: dict[str, str] = {}
        self.output_errors: dict[str, str] = {}
        self.output_lock = threading.RLock()
        self.outputs_enabled = True
        self.ui_pause_until = 0.0
        self.manual_pause_until = 0.0
        self.battery_preview_until = 0.0

    def run(self) -> int:
        self.install_signal_handlers()
        try:
            from .program_status import StatusError
            while not self.stop_event.is_set():
                try:
                    self.program_server.start()
                    break
                except (OSError, StatusError) as exc:
                    self.program_server.stop()
                    print(f"sidepulse service: status socket unavailable, retrying: {exc}", file=sys.stderr)
                    self.stop_event.wait(1)
            if self.stop_event.is_set():
                return 0
            self.program_relay.start()
            while not self.stop_event.is_set():
                config = load_relay_config()
                self.sync_receiver(config)
                self.sync_outputs()
                self.stop_event.wait(self.poll_seconds)
        finally:
            self.stop_receiver()
            self.program_server.stop()
            self.program_relay.stop()
        return 0

    def stop(self) -> None:
        self.stop_event.set()

    def install_signal_handlers(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda _signum, _frame: self.stop())

    def sync_receiver(self, config: RelayConfig) -> None:
        signature = (
            config.server,
            config.receiver_channel,
        ) if config.receives_events else None
        if signature == self.receiver_signature and (signature is None or
                self.receiver_thread is not None and self.receiver_thread.is_alive()):
            return
        self.stop_receiver()
        self.receiver_signature = signature
        if signature is None:
            return
        stop = threading.Event()
        thread = threading.Thread(
            target=listen_for_status_updates,
            args=(config, stop, lambda update: self.handle_remote_status(config, update)),
            daemon=True,
        )
        self.receiver_stop = stop
        self.receiver_thread = thread
        thread.start()

    def stop_receiver(self) -> None:
        if self.receiver_stop is not None:
            self.receiver_stop.set()
        self.receiver_stop = None
        self.receiver_thread = None
        self.receiver_signature = None

    def handle_remote_status(self, config: RelayConfig, snapshot: dict) -> None:
        from .program_transport import sender_identity
        # A retiring listener must not restore state after the link is changed.
        if self.receiver_signature != (config.server, config.receiver_channel):
            return
        self.program_state.receive_update(sender_identity(config.server, config.receiver_channel), snapshot)

    def sync_outputs(self) -> None:
        with self.output_lock:
            self._sync_outputs()

    def _sync_outputs(self) -> None:
        now = time.monotonic()
        if not self.outputs_enabled or now < max(self.ui_pause_until, self.manual_pause_until):
            return
        settings = load_settings()
        snapshot = self.monitor.snapshot(include_stale=False)
        mode = snapshot.aggregate.mode
        battery = None
        try:
            battery = read_battery_snapshot(
                full_charge_watts=settings.battery_full_charge_watts,
            )
        except Exception:
            pass

        active_targets: set[str] = set()
        for candidate in discover_devices():
            device_id = candidate.device_id or str(candidate.root.expanduser())
            display = settings.display_for_device(device_id)
            if display != LED_DISPLAY_CUSTOM and now < self.battery_preview_until:
                display = LED_DISPLAY_BATTERY
            if display == LED_DISPLAY_CUSTOM:
                continue
            if display == LED_DISPLAY_BATTERY and battery is not None:
                program = program_for_battery(
                    battery,
                    led_count=led_count_for_target(candidate.target),
                    brightness=settings.brightness_for_device(device_id),
                )
            else:
                animation = settings.agent_animation(mode)
                try:
                    program = program_for_agent_mode(
                        mode,
                        led_count=led_count_for_target(candidate.target),
                        brightness=settings.brightness_for_device(device_id),
                        animation_style=animation.style,
                        custom_program=animation.custom_program,
                    )
                except DeviceWriteError as exc:
                    self.output_errors[device_id] = str(exc)
                    continue
            key = f"file:{candidate.target}"
            active_targets.add(key)
            if self.last_program_by_target.get(key) == program:
                continue
            try:
                write_led_program(program, device_path=candidate.target)
            except Exception as exc:
                self.output_errors[device_id] = str(exc)
                print(f"sidepulse service: device write failed: {exc}", file=sys.stderr)
                continue
            self.output_errors.pop(device_id, None)
            self.last_program_by_target[key] = program

        for key in set(self.last_program_by_target) - active_targets:
            self.last_program_by_target.pop(key, None)


    def handle_command(self, command):
        from .program_status import StatusError
        if type(command.get('v')) is not int or command['v'] != 1:
            raise StatusError('Unsupported protocol version')
        def duration(name, default=0.0):
            value = command.get(name, default)
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 3600:
                raise StatusError('Invalid output control duration')
            return value
        kind = command.get('type')
        if kind == 'output_info':
            return {'output_errors': dict(self.output_errors), 'outputs_enabled': self.outputs_enabled}
        with self.output_lock:
            if kind == 'output_control':
                enabled = command.get('enabled', self.outputs_enabled)
                if type(enabled) is not bool:
                    raise StatusError('Invalid output enabled value')
                pause = duration('pause_seconds')
                preview = duration('battery_preview_seconds')
                self.outputs_enabled = enabled
                self.ui_pause_until = time.monotonic() + pause
                self.battery_preview_until = time.monotonic() + preview
                return {'ok': True, 'output_errors': dict(self.output_errors)}
            if kind == 'write_led':
                hold = duration('hold_seconds', 1.5)
                program, target = command.get('program'), command.get('target')
                if not isinstance(program, str) or not isinstance(target, str) or not target:
                    raise StatusError('Invalid LED write command')
                result = write_led_program(program, device_path=Path(target))
                self.manual_pause_until = time.monotonic() + hold
                # Automatic mode must restore even if it matches an older write.
                self.last_program_by_target.pop(f'file:{target}', None)
                return {'ok': True, 'target': str(result)}
            raise StatusError('Unsupported daemon command')


def run_service() -> int:
    return SidePulseService().run()
