from __future__ import annotations

import re
import time
from dataclasses import dataclass
from enum import Enum
from importlib.resources import files
from pathlib import Path

from .device_writer import (
    DEFAULT_FILE_NAME,
    DeviceWriteError,
    normalize_led_text,
    read_led_program,
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
KITT_RED = "#FF1800"
DONE_GREEN = "#00FF66"
IDLE_DIM = "#020204"
DEVICE_LED_COUNTS = {
    "sidepulsedot": 2,
    "pulsedot": 2,
    "sidepulsepro": 8,
}
COUNT_SPECIFIC_ANIMATIONS = frozenset(
    {
        "idle-pulse",
        "cyan-roll",
        "kitt",
        "kitt-red",
        "ember-tide",
        "ember-idle",
        "ember-lid-open",
        "night-rider",
        "purple-idle",
        "purple-tide",
        "purple-lid-open",
        "lid-open",
        "lid-closed",
    }
)


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
) -> str:
    if state == LedDisplayState.IDLE:
        return apply_brightness(builtin_animation_program("idle-pulse", led_count), brightness)
    if state == LedDisplayState.ASK:
        return apply_brightness(builtin_animation_program("amber-pulse", led_count), brightness)
    if state == LedDisplayState.DONE:
        return apply_brightness(builtin_animation_program("solid-green", led_count), brightness)
    if state == LedDisplayState.WORKING:
        return apply_brightness(builtin_animation_program("cyan-roll", led_count), brightness)
    raise ValueError(f"Unknown LED display state: {state}")


def program_for_agent_mode(
    mode: AgentMode,
    *,
    led_count: int = 8,
    brightness: int | float = 255,
    animation_style: str = "default",
    custom_program: str = "",
) -> str:
    state = display_state_for_mode(mode)
    if animation_style == "custom":
        program = normalize_led_text(custom_program)
        if not program:
            raise DeviceWriteError("Custom agent animation is empty.")
        return apply_brightness(program, brightness)
    if animation_style == "idle-pulse":
        return program_for_display_state(
            LedDisplayState.IDLE,
            led_count=led_count,
            brightness=brightness,
        )
    if animation_style == "cyan-roll":
        return program_for_display_state(
            LedDisplayState.WORKING,
            led_count=led_count,
            brightness=brightness,
        )
    if animation_style == "amber-pulse":
        return program_for_display_state(
            LedDisplayState.ASK,
            led_count=led_count,
            brightness=brightness,
        )
    if animation_style == "solid-green":
        return program_for_display_state(
            LedDisplayState.DONE,
            led_count=led_count,
            brightness=brightness,
        )
    if animation_style == "kitt":
        return apply_brightness(
            builtin_animation_program("kitt", led_count),
            brightness,
        )
    if animation_style == "kitt-red":
        return apply_brightness(
            builtin_animation_program("kitt-red", led_count),
            brightness,
        )
    if animation_style in {
        "off",
        "immediate-off",
        "cyan-complete",
        "ember-idle",
        "ember-tide",
        "ember-attention",
        "ember-complete",
        "ember-lid-open",
        "night-rider",
        "purple-idle",
        "purple-tide",
        "purple-attention",
        "purple-complete",
        "purple-lid-open",
        "lid-open",
        "lid-closed",
    }:
        return apply_brightness(
            builtin_animation_program(animation_style, led_count),
            brightness,
        )
    if animation_style != "default":
        raise DeviceWriteError(f"Unknown agent animation style: {animation_style}")
    return program_for_display_state(
        state,
        led_count=led_count,
        brightness=brightness,
    )


def builtin_animation_file_name(animation_id: str, led_count: int = 8) -> str:
    if animation_id in COUNT_SPECIFIC_ANIMATIONS:
        count = 2 if int(led_count) == 2 else 8
        return f"{animation_id}-{count}.LED"
    return f"{animation_id}.LED"


def builtin_animation_program(animation_id: str, led_count: int = 8) -> str:
    file_name = builtin_animation_file_name(animation_id, led_count)
    resource = files("sidepulse.resources").joinpath("animations", file_name)
    try:
        program = normalize_led_text(resource.read_text(encoding="utf-8")).strip()
    except (FileNotFoundError, OSError) as exc:
        raise DeviceWriteError(f"Built-in animation is missing: {file_name}") from exc
    if not program:
        raise DeviceWriteError(f"Built-in animation is empty: {file_name}")
    return program


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
            "off 320ms cosine",
            "; ".join(segments),
            "repeat",
        ]
    )


def kitt_scanner_program(color: str, *, led_count: int = 8) -> str:
    """Build a compact scanner pulse that sweeps out and back."""
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
    animation_style: str = "default",
    custom_program: str = "",
) -> LedStatusWrite:
    target = resolve_target_path(device_path=device_path, file_name=file_name)
    state = display_state_for_mode(mode)
    program = program_for_agent_mode(
        mode,
        led_count=led_count_for_target(target),
        brightness=brightness,
        animation_style=animation_style,
        custom_program=custom_program,
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
    scaled_lines: list[str] = []
    found_brightness = False
    for line in str(program).splitlines():
        match = re.fullmatch(r"\s*brightness\s+(\d+)\s*", line, re.IGNORECASE)
        if match is None:
            scaled_lines.append(line)
            continue
        found_brightness = True
        authored = normalize_brightness(int(match.group(1)))
        scaled = round(authored * value / 255)
        scaled_lines.append(f"brightness {scaled}")
    normalized_program = "\n".join(scaled_lines)
    if found_brightness:
        return normalized_program
    if value >= 255:
        return normalized_program
    return f"brightness {value}\n{normalized_program}"


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
        self.last_animation_signature: tuple[str, str] | None = None
        self.last_program: str | None = None
        self.last_error: str | None = None
        self.last_target: Path | None = None
        self.last_attempt_monotonic = 0.0

    def reset(self) -> None:
        self.last_state = None
        self.last_brightness = None
        self.last_animation_signature = None
        self.last_program = None
        self.last_error = None
        self.last_target = None
        self.last_attempt_monotonic = 0.0

    def _last_program_is_current(self) -> bool:
        if self.dry_run:
            return True
        if self.last_target is None or self.last_program is None:
            return False
        try:
            return read_led_program(self.last_target) == self.last_program
        except (OSError, UnicodeError):
            return False

    def sync_mode(
        self,
        mode: AgentMode,
        *,
        animation_style: str = "default",
        custom_program: str = "",
    ) -> LedStatusWrite:
        state = display_state_for_mode(mode)
        brightness = normalize_brightness(self.brightness)
        animation_signature = (
            animation_style,
            custom_program if animation_style == "custom" else "",
        )
        now = time.monotonic()
        if (
            state == self.last_state
            and brightness == self.last_brightness
            and animation_signature == self.last_animation_signature
            and self.last_error is None
            and self._last_program_is_current()
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
            and animation_signature == self.last_animation_signature
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
                animation_style=animation_style,
                custom_program=custom_program,
            )
        except (DeviceWriteError, OSError) as exc:
            self.last_state = state
            self.last_brightness = brightness
            self.last_animation_signature = animation_signature
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
        self.last_animation_signature = animation_signature
        self.last_program = result.program
        self.last_error = None
        self.last_target = result.target
        return result
