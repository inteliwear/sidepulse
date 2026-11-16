from __future__ import annotations

import os
import subprocess
import time
from typing import Callable, Sequence

from .models import AgentMode


AWAKE_GRACE_SECONDS = 300.0
CAFFEINATE_COMMAND = ("/usr/bin/caffeinate", "-ims")


class KeepAwakeController:
    def __init__(
        self,
        *,
        enabled: bool = True,
        grace_seconds: float = AWAKE_GRACE_SECONDS,
        command: Sequence[str] = CAFFEINATE_COMMAND,
        process_factory: Callable[..., object] | None = None,
        watch_current_process: bool = True,
    ) -> None:
        self.enabled = enabled
        self.grace_seconds = grace_seconds
        self.command = tuple(command)
        self.process_factory = process_factory or subprocess.Popen
        self.watch_current_process = watch_current_process
        self.process = None
        self.last_mode: AgentMode | None = None
        self.holding_requested = False
        self.grace_until_monotonic: float | None = None
        self.last_error: str | None = None

    def set_enabled(self, enabled: bool) -> None:
        if self.enabled == enabled:
            return
        self.enabled = enabled
        if not enabled:
            self.release()
            self.holding_requested = False
            self.grace_until_monotonic = None

    def update(self, mode: AgentMode, *, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        should_hold = self.should_hold_for_mode(mode, current)
        return self.update_requested(should_hold, mode=mode)

    def update_requested(self, should_hold: bool, *, mode: AgentMode | None = None) -> bool:
        self.holding_requested = should_hold
        if mode is not None:
            self.last_mode = mode

        if not self.enabled or not should_hold:
            self.release()
            return False

        self.ensure_awake()
        return self.process_running()

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

    def caffeinate_command(self) -> list[str]:
        command = list(self.command)
        if self.watch_current_process:
            command.extend(["-w", str(os.getpid())])
        return command

    def detail(self, *, now: float | None = None) -> str:
        if not self.enabled:
            return "Keep awake disabled"
        if self.last_error:
            return f"Keep awake error: {self.last_error}"
        current = time.monotonic() if now is None else now
        if self.grace_until_monotonic is not None and current < self.grace_until_monotonic:
            remaining = int(self.grace_until_monotonic - current)
            return f"Keep awake grace: {format_duration(remaining)}"
        if self.process_running():
            return "Keep awake active"
        return "Keep awake standby"


def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    minutes, rest = divmod(seconds, 60)
    if minutes:
        return f"{minutes}m{rest:02d}s"
    return f"{rest}s"
