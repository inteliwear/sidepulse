from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Sequence

from .device_writer import KNOWN_LED_FILE_NAMES
from .models import AgentMode


AWAKE_GRACE_SECONDS = 300.0
SD_STATUS_READ_SECONDS = 60.0
POWER_SOURCE_POLL_SECONDS = 30.0
KEEPALIVE_FILE_NAME = "keepalive"
STATUS_FILE_NAME = KEEPALIVE_FILE_NAME
CAFFEINATE_COMMAND = ("/usr/bin/caffeinate", "-dimsu")
PMSET_POWER_SOURCE_COMMAND = ("/usr/bin/pmset", "-g", "ps")


def read_on_battery_power(
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    command: Sequence[str] = PMSET_POWER_SOURCE_COMMAND,
) -> bool | None:
    """Return True on battery, False on AC, None when the source is unknown.

    Unknown is deliberately distinct from "on battery" so that a failure to read
    the power source never silently disables keep-awake.
    """
    try:
        result = runner(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None

    text = result.stdout or ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    if "'Battery Power'" in text:
        return True
    if "'AC Power'" in text:
        return False
    return None


class KeepAwakeController:
    def __init__(
        self,
        *,
        enabled: bool = True,
        grace_seconds: float = AWAKE_GRACE_SECONDS,
        status_read_seconds: float = SD_STATUS_READ_SECONDS,
        command: Sequence[str] = CAFFEINATE_COMMAND,
        process_factory: Callable[..., object] | None = None,
        status_reader: Callable[[Path], None] | None = None,
        status_read_async: bool = True,
        watch_current_process: bool = True,
        skip_on_battery: bool = True,
        on_battery_reader: Callable[[], bool | None] | None = None,
        power_poll_seconds: float = POWER_SOURCE_POLL_SECONDS,
    ) -> None:
        self.enabled = enabled
        self.grace_seconds = grace_seconds
        self.status_read_seconds = status_read_seconds
        self.command = tuple(command)
        self.process_factory = process_factory or subprocess.Popen
        self.status_reader = status_reader or touch_keepalive_file
        self.status_read_async = status_read_async
        self.watch_current_process = watch_current_process
        self.skip_on_battery = skip_on_battery
        self.on_battery_reader = on_battery_reader or read_on_battery_power
        self.power_poll_seconds = power_poll_seconds
        self.process = None
        self.last_mode: AgentMode | None = None
        self.holding_requested = False
        self.grace_until_monotonic: float | None = None
        self.last_error: str | None = None
        self.last_status_read_monotonic_by_path: dict[Path, float] = {}
        self.last_status_error: str | None = None
        self.status_read_in_flight_by_path: set[Path] = set()
        self.on_battery: bool | None = None
        self.last_power_read_monotonic: float | None = None
        self.deferred_for_battery = False

    def set_enabled(self, enabled: bool) -> None:
        if self.enabled == enabled:
            return
        self.enabled = enabled
        if not enabled:
            self.release()
            self.holding_requested = False
            self.grace_until_monotonic = None
            self.last_status_error = None
            self.last_status_read_monotonic_by_path.clear()
            self.status_read_in_flight_by_path.clear()

    def update(self, mode: AgentMode, *, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        should_hold = self.should_hold_for_mode(mode, current)
        self.holding_requested = should_hold
        self.last_mode = mode

        if not self.enabled or not should_hold:
            self.deferred_for_battery = False
            self.release()
            return False

        if self.skip_on_battery and self.read_power_source(current) is True:
            self.deferred_for_battery = True
            self.release()
            return False

        self.deferred_for_battery = False
        self.ensure_awake()
        return self.process_running()

    def read_power_source(self, now: float) -> bool | None:
        """Cached power-source read; the subprocess runs at most once per poll window."""
        last = self.last_power_read_monotonic
        if last is not None and now - last < self.power_poll_seconds:
            return self.on_battery

        self.last_power_read_monotonic = now
        try:
            self.on_battery = self.on_battery_reader()
        except Exception:
            self.on_battery = None
        return self.on_battery

    def should_hold_for_mode(self, mode: AgentMode, now: float) -> bool:
        if mode in {
            AgentMode.WORKING,
            AgentMode.TOOL_RUNNING,
            AgentMode.LONG_TASK_PROGRESS,
        }:
            self.grace_until_monotonic = None
            return True

        if mode in {
            AgentMode.COMPLETED,
            AgentMode.WAITING_FOR_INPUT,
            AgentMode.BLOCKED_ERROR,
        }:
            if self.last_mode != mode or self.grace_until_monotonic is None:
                self.grace_until_monotonic = now + self.grace_seconds
            return now < self.grace_until_monotonic

        return self.grace_until_monotonic is not None and now < self.grace_until_monotonic

    def ensure_awake(self) -> None:
        if self.process_running():
            return

        try:
            self.process = self.process_factory(
                self.caffeinate_command(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.last_error = None
        except Exception as exc:
            self.process = None
            self.last_error = str(exc)

    def release(self) -> None:
        process = self.process
        self.process = None
        if process is None:
            return

        try:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=1)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    def process_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def poke_status_file(self, target: Path | None, *, now: float | None = None) -> Path | None:
        if not self.enabled or target is None:
            return None

        current = time.monotonic() if now is None else now
        status_path = keepalive_file_for_target(target)
        last_read = self.last_status_read_monotonic_by_path.get(status_path)
        if last_read is not None and current - last_read < self.status_read_seconds:
            return None

        self.last_status_read_monotonic_by_path[status_path] = current
        if self.status_read_async:
            if status_path in self.status_read_in_flight_by_path:
                return None
            self.status_read_in_flight_by_path.add(status_path)
            thread = threading.Thread(
                target=self._run_status_reader,
                args=(status_path,),
                daemon=True,
            )
            thread.start()
            return status_path

        return self._run_status_reader(status_path)

    def caffeinate_command(self) -> list[str]:
        command = list(self.command)
        if self.watch_current_process:
            command.extend(["-w", str(os.getpid())])
        return command

    def _run_status_reader(self, status_path: Path) -> Path | None:
        try:
            self.status_reader(status_path)
            self.last_status_error = None
            return status_path
        except Exception as exc:
            self.last_status_error = str(exc)
            return None
        finally:
            self.status_read_in_flight_by_path.discard(status_path)

    def detail(self, *, now: float | None = None) -> str:
        if not self.enabled:
            return "Keep awake disabled"
        if self.last_error:
            return f"Keep awake error: {self.last_error}"
        if self.deferred_for_battery:
            return "Keep awake paused on battery"
        current = time.monotonic() if now is None else now
        if self.grace_until_monotonic is not None and current < self.grace_until_monotonic:
            remaining = int(self.grace_until_monotonic - current)
            return f"Keep awake grace: {format_duration(remaining)}"
        if self.process_running():
            return "Keep awake active"
        return "Keep awake standby"


def status_file_for_target(target: Path) -> Path:
    return keepalive_file_for_target(target)


def keepalive_file_for_target(target: Path) -> Path:
    known_file_names = KNOWN_LED_FILE_NAMES | {KEEPALIVE_FILE_NAME.upper(), "STATUS.TXT"}
    if target.name.upper() in known_file_names:
        return target.parent / KEEPALIVE_FILE_NAME
    return target / KEEPALIVE_FILE_NAME


def read_status_file(path: Path) -> None:
    touch_keepalive_file(path)


def touch_keepalive_file(path: Path) -> None:
    subprocess.run(
        ["/usr/bin/touch", str(path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=2,
        check=True,
    )


def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    minutes, rest = divmod(seconds, 60)
    if minutes:
        return f"{minutes}m{rest:02d}s"
    return f"{rest}s"
