from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .device_writer import (
    DEFAULT_FILE_NAME,
    DeviceWriteError,
    resolve_target_path,
    write_led_program,
)
from .models import AgentMode


class LedDisplayState(str, Enum):
    IDLE = "idle"
    WORKING = "working"
    DONE = "done"
    ASK = "ask"


LED_STATE_LABELS: dict[LedDisplayState, str] = {
    LedDisplayState.IDLE: "Idle",
    LedDisplayState.WORKING: "Working",
    LedDisplayState.DONE: "Done",
    LedDisplayState.ASK: "Ask",
}


ASK_AMBER = "#FF3A00"
WORKING_CYAN = "#00E5FF"
DONE_GREEN = "#00FF66"
DEVICE_LED_COUNTS = {
    "sidepulsedot": 2,
    "sidepulsepro": 8,
}


@dataclass(frozen=True)
class LedStatusWrite:
    state: LedDisplayState
    target: Path | None
    program: str
    changed: bool
    error: str | None = None

    @property
    def label(self) -> str:
        return LED_STATE_LABELS[self.state]


def display_state_for_mode(mode: AgentMode) -> LedDisplayState:
    if mode in {AgentMode.WAITING_FOR_INPUT, AgentMode.BLOCKED_ERROR}:
        return LedDisplayState.ASK
    if mode in {
        AgentMode.WORKING,
        AgentMode.TOOL_RUNNING,
        AgentMode.LONG_TASK_PROGRESS,
    }:
        return LedDisplayState.WORKING
    if mode == AgentMode.COMPLETED:
        return LedDisplayState.DONE
    return LedDisplayState.IDLE


def program_for_display_state(
    state: LedDisplayState,
    *,
    led_count: int = 8,
    brightness: int | float = 255,
    kitt_mode: bool = False,
) -> str:
    if state == LedDisplayState.IDLE:
        return "off"
    if state == LedDisplayState.ASK:
        return apply_brightness(
            "\n".join(
                [
                    "off",
                    f"{ASK_AMBER} 1.6s pulse",
                    "repeat",
                ]
            ),
            brightness,
        )
    if state == LedDisplayState.DONE:
        return apply_brightness(DONE_GREEN, brightness)
    if state == LedDisplayState.WORKING:
        program = (
            kitt_scanner_program(WORKING_CYAN, led_count=led_count)
            if kitt_mode
            else rolling_program(WORKING_CYAN, led_count=led_count)
        )
        return apply_brightness(program, brightness)
    raise ValueError(f"Unknown LED display state: {state}")


def rolling_program(color: str, *, led_count: int = 8) -> str:
    count = max(2, min(8, int(led_count)))
    delay_ms = 260 if count == 2 else 95
    duration_ms = 760
    segments: list[str] = []
    for active_index in range(count):
        delay = active_index * delay_ms
        segments.append(f"{active_index}:{color} {duration_ms}ms pulse {delay}ms")
    return "\n".join(
        [
            "off 160ms cosine",
            "; ".join(segments),
            "repeat",
        ]
    )


def kitt_scanner_program(color: str, *, led_count: int = 8) -> str:
    """Build a compact KITT-style pulse that scans out and back."""
    count = max(2, min(8, int(led_count)))
    delay_ms = 240 if count == 2 else 85
    duration_ms = 320
    directions = (range(count), range(count - 2, -1, -1))
    scan_lines = [
        "; ".join(
            f"{index}:{color} {duration_ms}ms pulse {step * delay_ms}ms"
            for step, index in enumerate(indexes)
        )
        for indexes in directions
    ]
    return "\n".join(
        [
            "off 80ms cosine",
            *scan_lines,
            "repeat",
        ]
    )


def write_mode_to_leds(
    mode: AgentMode,
    *,
    device_path: Path | None = None,
    file_name: str = DEFAULT_FILE_NAME,
    dry_run: bool = False,
    brightness: int | float = 255,
    kitt_mode: bool = False,
) -> LedStatusWrite:
    target = resolve_target_path(device_path=device_path, file_name=file_name)
    state = display_state_for_mode(mode)
    program = program_for_display_state(
        state,
        led_count=led_count_for_target(target),
        brightness=brightness,
        kitt_mode=kitt_mode,
    )
    written_target = write_led_program(
        program,
        device_path=target,
        file_name=file_name,
        dry_run=dry_run,
    )
    return LedStatusWrite(
        state=state,
        target=written_target,
        program=program,
        changed=True,
    )


def led_count_for_target(target: Path) -> int:
    name = normalized_device_name(target.parent.name)
    for hint, led_count in DEVICE_LED_COUNTS.items():
        if hint in name:
            return led_count
    return 8


def normalized_device_name(name: str) -> str:
    return "".join(char for char in name.lower() if char.isalnum())


def normalize_brightness(value: int | float | None) -> int:
    if value is None:
        return 255
    return max(0, min(255, int(round(float(value)))))


def brightness_percent(value: int | float | None) -> int:
    return round(normalize_brightness(value) / 255 * 100)


def apply_brightness(program: str, brightness: int | float = 255) -> str:
    value = normalize_brightness(brightness)
    if value >= 255:
        return program
    return f"brightness {value}\n{program}"


class AgentLedController:
    def __init__(
        self,
        *,
        device_path: Path | None = None,
        file_name: str = DEFAULT_FILE_NAME,
        dry_run: bool = False,
        error_retry_seconds: float = 10.0,
        brightness: int | float = 255,
    ) -> None:
        self.device_path = device_path
        self.file_name = file_name
        self.dry_run = dry_run
        self.error_retry_seconds = error_retry_seconds
        self.brightness = normalize_brightness(brightness)
        self.last_state: LedDisplayState | None = None
        self.last_brightness: int | None = None
        self.last_kitt_mode: bool | None = None
        self.last_error: str | None = None
        self.last_target: Path | None = None
        self.last_attempt_monotonic = 0.0

    def reset(self) -> None:
        self.last_state = None
        self.last_brightness = None
        self.last_kitt_mode = None
        self.last_error = None
        self.last_target = None
        self.last_attempt_monotonic = 0.0

    def sync_mode(self, mode: AgentMode, *, kitt_mode: bool = False) -> LedStatusWrite:
        state = display_state_for_mode(mode)
        brightness = normalize_brightness(self.brightness)
        kitt_mode = bool(kitt_mode)
        now = time.monotonic()
        if (
            state == self.last_state
            and brightness == self.last_brightness
            and kitt_mode == self.last_kitt_mode
            and self.last_error is None
        ):
            return LedStatusWrite(
                state=state,
                target=self.last_target,
                program="",
                changed=False,
            )
        if (
            state == self.last_state
            and brightness == self.last_brightness
            and kitt_mode == self.last_kitt_mode
            and self.last_error is not None
            and now - self.last_attempt_monotonic < self.error_retry_seconds
        ):
            return LedStatusWrite(
                state=state,
                target=self.last_target,
                program="",
                changed=False,
                error=self.last_error,
            )

        self.last_attempt_monotonic = now
        try:
            result = write_mode_to_leds(
                mode,
                device_path=self.device_path,
                file_name=self.file_name,
                dry_run=self.dry_run,
                brightness=brightness,
                kitt_mode=kitt_mode,
            )
        except (DeviceWriteError, OSError) as exc:
            self.last_state = state
            self.last_brightness = brightness
            self.last_kitt_mode = kitt_mode
            self.last_error = str(exc)
            return LedStatusWrite(
                state=state,
                target=self.last_target,
                program="",
                changed=False,
                error=self.last_error,
            )

        self.last_state = state
        self.last_brightness = brightness
        self.last_kitt_mode = kitt_mode
        self.last_error = None
        self.last_target = result.target
        return result
