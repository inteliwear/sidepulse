from __future__ import annotations

import json
import signal
import socket
import sys
import threading
import time

from .battery import program_for_battery, read_battery_snapshot
from .collector import LiveAgentMonitor, default_sources
from .device_writer import DeviceWriteError, discover_devices, write_led_program
from .hook import write_hook_line
from .ipc import (
    HookEventServer,
    candidate_event_socket_paths,
    default_latest_state_path,
    send_hook_event,
)
from .led_status import led_count_for_target, program_for_agent_mode
from .providers import detect_log_path, parse_log_line
from .relay import (
    RelayConfig,
    default_relay_event_socket_path,
    listen_for_relay_events,
    load_relay_config,
    publish_relay_event,
)
from .settings import LED_DISPLAY_BATTERY, LED_DISPLAY_CUSTOM, load_settings


SERVICE_POLL_SECONDS = 1.0


class SidePulseService:
    def __init__(self, *, poll_seconds: float = SERVICE_POLL_SECONDS) -> None:
        self.poll_seconds = poll_seconds
        self.stop_event = threading.Event()
        self.local_server = HookEventServer(
            self.handle_local_event,
            socket_path=default_relay_event_socket_path(),
        )
        from .program_status import ProgramStore
        from .program_transport import ProgramServer, ProgramRelay
        from .program_presentation import ProgramMonitor
        self.program_store = ProgramStore()
        self.program_server = ProgramServer(self.program_store)
        self.monitor = LiveAgentMonitor(
            recovery_sources=default_sources(),
            latest_state_path=default_latest_state_path(),
        )
        self.monitor = ProgramMonitor(self.monitor, self.program_store)
        self.program_relay = ProgramRelay(self.program_store)
        self.receiver_stop: threading.Event | None = None
        self.receiver_thread: threading.Thread | None = None
        self.receiver_signature: tuple[str, str] | None = None
        self.seen_event_ids: set[str] = set()
        self.last_program_by_target: dict[str, str] = {}

    def run(self) -> int:
        self.install_signal_handlers()
        self.program_server.start()
        self.program_relay.start()
        try:
            self.local_server.start()
            while not self.stop_event.is_set():
                config = load_relay_config()
                self.sync_receiver(config)
                self.sync_outputs()
                self.stop_event.wait(self.poll_seconds)
        finally:
            self.stop_receiver()
            self.local_server.stop()
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

    def handle_local_event(self, provider: str, line: dict) -> None:
        self.ingest_event(provider, line)
        config = load_relay_config()
        if not config.publishes_events:
            return
        try:
            publish_relay_event(config, provider, line)
        except Exception as exc:
            print(f"sidepulse service: relay send failed: {exc}", file=sys.stderr)

    def sync_receiver(self, config: RelayConfig) -> None:
        signature = (
            config.server,
            config.receiver_channel,
        ) if config.receives_events else None
        if signature == self.receiver_signature:
            return
        self.stop_receiver()
        self.receiver_signature = signature
        if signature is None:
            return
        stop = threading.Event()
        thread = threading.Thread(
            target=listen_for_relay_events,
            args=(config, stop, self.handle_remote_event),
            kwargs={'on_status': lambda snapshot: self.handle_remote_status(config, snapshot)},
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

    def handle_remote_event(self, provider: str, line: dict, event_id: str) -> None:
        if event_id in self.seen_event_ids:
            return
        self.seen_event_ids.add(event_id)
        if len(self.seen_event_ids) > 2048:
            self.seen_event_ids = {event_id}
        try:
            write_hook_line(detect_log_path(provider), line)
        except Exception as exc:
            print(f"sidepulse service: relay receive failed: {exc}", file=sys.stderr)
            return
        self.ingest_event(provider, line)
        send_hook_event(provider, line)

    def handle_remote_status(self, config: RelayConfig, snapshot: dict) -> None:
        from .program_transport import sender_identity
        # A retiring listener must not restore state after the link is changed.
        if self.receiver_signature != (config.server, config.receiver_channel):
            return
        self.program_store.receive(sender_identity(config.server, config.receiver_channel), snapshot)

    def ingest_event(self, provider: str, line: dict) -> None:
        try:
            record = parse_log_line(
                provider,
                json.dumps(line, separators=(",", ":"), ensure_ascii=False),
            )
            if record is not None:
                self.monitor.ingest_record(record)
        except Exception:
            pass

    def sync_outputs(self) -> None:
        if status_bar_monitor_available():
            return
        settings = load_settings()
        getattr(self.monitor, 'monitor', self.monitor).stale_after_seconds = settings.idle_timeout_seconds
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
                except DeviceWriteError:
                    continue
            key = f"file:{candidate.target}"
            active_targets.add(key)
            if self.last_program_by_target.get(key) == program:
                continue
            try:
                write_led_program(program, device_path=candidate.target)
            except Exception as exc:
                print(f"sidepulse service: device write failed: {exc}", file=sys.stderr)
                continue
            self.last_program_by_target[key] = program

        for key in set(self.last_program_by_target) - active_targets:
            self.last_program_by_target.pop(key, None)


def run_service() -> int:
    return SidePulseService().run()


def status_bar_monitor_available() -> bool:
    for path in candidate_event_socket_paths():
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(0.05)
        try:
            client.connect(str(path.expanduser()))
            return True
        except OSError:
            continue
        finally:
            client.close()
    return False
