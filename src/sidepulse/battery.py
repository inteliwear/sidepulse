from __future__ import annotations

import json
import math
import plistlib
import re
import subprocess
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from .device_writer import (
    DEFAULT_FILE_NAME,
    DeviceWriteError,
    resolve_target_path,
    write_led_program,
)
from .led_status import led_count_for_target
from .led_status import apply_brightness, normalize_brightness


BATTERY_LOW_RED = "#FF2600"
BATTERY_MID_AMBER = "#FFB000"
BATTERY_HIGH_GREEN = "#00FF66"
BATTERY_CHARGING_MINT = "#80FFC8"
BATTERY_OFF = "#000000"
DEFAULT_POWER_CHANGE_PREVIEW_SECONDS = 7.0
POWER_PREVIEW_RETRIGGER_SECONDS = 30.0


def should_arm_battery_power_preview(
    previous_plugged: bool | None,
    current_plugged: bool,
    *,
    now: float,
    last_armed_at: float | None,
    retrigger_seconds: float = POWER_PREVIEW_RETRIGGER_SECONDS,
) -> bool:
    """Ignore the first reading and rapid plugged/unplugged flaps."""

    if previous_plugged is None or previous_plugged == current_plugged:
        return False
    if last_armed_at is None:
        return True
    return now - last_armed_at >= retrigger_seconds


@dataclass(frozen=True)
class PDProfile:
    voltage: float
    current: float

    @property
    def watts(self) -> float:
        return self.voltage * self.current

    def to_dict(self) -> dict[str, float]:
        return {
            "voltage": self.voltage,
            "current": self.current,
            "watts": self.watts,
        }


@dataclass(frozen=True)
class BatterySnapshot:
    percent: int = 0
    is_charging: bool = False
    is_charged: bool = False
    is_plugged: bool = False
    battery_present: bool = True
    voltage: float = 0.0
    amperage: float = 0.0
    battery_watts: float = 0.0
    time_to_full: int = -1
    time_to_empty: int = -1
    cycle_count: int = -1
    temperature_c: float | None = None
    design_capacity_mah: int = -1
    max_capacity_mah: int = -1
    current_capacity_mah: int = -1
    health_percent: int = -1
    condition: str = ""
    adapter_connected: bool = False
    adapter_watts: int = -1
    adapter_voltage: float = 0.0
    adapter_current: float = 0.0
    adapter_name: str = ""
    adapter_manufacturer: str = ""
    adapter_model: str = ""
    adapter_serial: str = ""
    adapter_family: str = ""
    pd_profiles: tuple[PDProfile, ...] = ()
    full_charge_watts: float = 100.0

    @property
    def adapter_power(self) -> float:
        if self.adapter_watts > 0:
            return float(self.adapter_watts)
        negotiated = self.adapter_voltage * self.adapter_current
        if negotiated > 0:
            return negotiated
        if self.pd_profiles:
            return max(profile.watts for profile in self.pd_profiles)
        return 0.0

    @property
    def battery_power_magnitude(self) -> float:
        return abs(self.battery_watts)

    def charge_speed_ratio(self, full_charge_watts: float | None = None) -> float:
        baseline = full_charge_watts or self.full_charge_watts
        if baseline <= 0:
            return 0.0
        return clamp(self.adapter_power / baseline)

    def to_dict(self) -> dict[str, Any]:
        return {
            "percent": self.percent,
            "is_charging": self.is_charging,
            "is_charged": self.is_charged,
            "is_plugged": self.is_plugged,
            "battery_present": self.battery_present,
            "voltage": self.voltage,
            "amperage": self.amperage,
            "battery_watts": self.battery_watts,
            "time_to_full": self.time_to_full,
            "time_to_empty": self.time_to_empty,
            "cycle_count": self.cycle_count,
            "temperature_c": self.temperature_c,
            "design_capacity_mah": self.design_capacity_mah,
            "max_capacity_mah": self.max_capacity_mah,
            "current_capacity_mah": self.current_capacity_mah,
            "health_percent": self.health_percent,
            "condition": self.condition,
            "adapter_connected": self.adapter_connected,
            "adapter_watts": self.adapter_watts,
            "adapter_voltage": self.adapter_voltage,
            "adapter_current": self.adapter_current,
            "adapter_power": self.adapter_power,
            "adapter_name": self.adapter_name,
            "adapter_manufacturer": self.adapter_manufacturer,
            "adapter_model": self.adapter_model,
            "adapter_serial": self.adapter_serial,
            "adapter_family": self.adapter_family,
            "pd_profiles": [profile.to_dict() for profile in self.pd_profiles],
            "full_charge_watts": self.full_charge_watts,
            "charge_speed_ratio": self.charge_speed_ratio(),
        }


@dataclass(frozen=True)
class BatteryLedWrite:
    target: Path | None
    program: str
    changed: bool
    error: str | None = None

    @property
    def label(self) -> str:
        return "Battery"


CommandRunner = Callable[..., subprocess.CompletedProcess]


def read_battery_snapshot(
    *,
    full_charge_watts: float | None = None,
    runner: CommandRunner = subprocess.run,
) -> BatterySnapshot:
    result = runner(
        ["ioreg", "-r", "-n", "AppleSmartBattery", "-a"],
        check=True,
        capture_output=True,
        timeout=2,
    )
    data = result.stdout
    if isinstance(data, str):
        data = data.encode("utf-8")
    snapshot = parse_ioreg_battery_plist(data)
    return snapshot_with_full_charge_watts(snapshot, full_charge_watts)


def parse_ioreg_battery_plist(data: bytes | str) -> BatterySnapshot:
    if isinstance(data, str):
        data = data.encode("utf-8")
    parsed = plistlib.loads(data)
    if not isinstance(parsed, list) or not parsed:
        return BatterySnapshot(full_charge_watts=default_full_charge_watts())
    props = parsed[0]
    if not isinstance(props, dict):
        return BatterySnapshot(full_charge_watts=default_full_charge_watts())
    return snapshot_from_ioreg_props(props)


def snapshot_from_ioreg_props(props: dict[str, Any]) -> BatterySnapshot:
    voltage = int_value(props.get("Voltage")) / 1000.0
    amperage_ma = int_value(props.get("Amperage"))
    if amperage_ma == 0:
        amperage_ma = int_value(props.get("InstantAmperage"))
    amperage = amperage_ma / 1000.0

    raw_max = int_value(props.get("AppleRawMaxCapacity"), default=-1)
    raw_current = int_value(props.get("AppleRawCurrentCapacity"), default=-1)
    design = int_value(props.get("DesignCapacity"), default=-1)
    max_capacity = raw_max if raw_max > 0 else int_value(props.get("MaxCapacity"), default=-1)
    current_capacity = (
        raw_current if raw_current >= 0 else int_value(props.get("CurrentCapacity"), default=-1)
    )
    health_percent = -1
    if raw_max > 0 and design > 0:
        health_percent = round(raw_max / design * 100)

    adapter_details = props.get("AdapterDetails")
    adapter = parse_adapter_details(adapter_details if isinstance(adapter_details, dict) else {})
    percent = int_value(props.get("CurrentCapacity"), default=0)
    if percent < 0 or percent > 100:
        percent = int_value(props.get("StateOfCharge"), default=0)

    condition = ""
    permanent_failure = int_value(props.get("PermanentFailureStatus"), default=0)
    if permanent_failure:
        condition = "Service Recommended"
    elif props.get("BatteryInstalled") is not False:
        condition = "Normal"

    return BatterySnapshot(
        percent=max(0, min(100, percent)),
        is_charging=bool(props.get("IsCharging", False)),
        is_charged=bool(props.get("FullyCharged", False)),
        is_plugged=bool(props.get("ExternalConnected", False)),
        battery_present=bool(props.get("BatteryInstalled", True)),
        voltage=voltage,
        amperage=amperage,
        battery_watts=voltage * amperage,
        time_to_full=int_value(props.get("AvgTimeToFull"), default=-1),
        time_to_empty=int_value(props.get("AvgTimeToEmpty"), default=-1),
        cycle_count=int_value(props.get("CycleCount"), default=-1),
        temperature_c=temperature_celsius(props.get("Temperature")),
        design_capacity_mah=design,
        max_capacity_mah=max_capacity,
        current_capacity_mah=current_capacity,
        health_percent=health_percent,
        condition=condition,
        adapter_connected=adapter["connected"],
        adapter_watts=adapter["watts"],
        adapter_voltage=adapter["voltage"],
        adapter_current=adapter["current"],
        adapter_name=adapter["name"],
        adapter_manufacturer=adapter["manufacturer"],
        adapter_model=adapter["model"],
        adapter_serial=adapter["serial"],
        adapter_family=adapter["family"],
        pd_profiles=tuple(adapter["pd_profiles"]),
        full_charge_watts=default_full_charge_watts(),
    )


def parse_adapter_details(details: dict[str, Any]) -> dict[str, Any]:
    profiles: list[PDProfile] = []
    menu = details.get("UsbHvcMenu")
    if isinstance(menu, list):
        for entry in menu:
            if not isinstance(entry, dict):
                continue
            voltage = int_value(entry.get("MaxVoltage")) / 1000.0
            current = int_value(entry.get("MaxCurrent")) / 1000.0
            if voltage > 0 and current > 0:
                profiles.append(PDProfile(voltage=voltage, current=current))
    profiles.sort(key=lambda profile: profile.watts)
    voltage_mv = int_value(details.get("AdapterVoltage", details.get("Voltage")))
    return {
        "connected": bool(details),
        "watts": int_value(details.get("Watts"), default=-1),
        "voltage": voltage_mv / 1000.0,
        "current": int_value(details.get("Current")) / 1000.0,
        "name": str_value(details.get("Name")),
        "manufacturer": str_value(details.get("Manufacturer")),
        "model": str_value(details.get("Model")),
        "serial": str_value(details.get("SerialString", details.get("SerialNumber"))),
        "family": adapter_family_name(int_value(details.get("FamilyCode"), default=-1)),
        "pd_profiles": profiles,
    }


def snapshot_with_full_charge_watts(
    snapshot: BatterySnapshot,
    full_charge_watts: float | None,
) -> BatterySnapshot:
    value = full_charge_watts if full_charge_watts is not None else default_full_charge_watts()
    return BatterySnapshot(
        **{
            **snapshot.__dict__,
            "full_charge_watts": max(1.0, float(value)),
        }
    )


def program_for_battery(
    snapshot: BatterySnapshot,
    *,
    led_count: int = 8,
    full_charge_watts: float | None = None,
    transition_ms: int = 360,
    brightness: int | float = 255,
) -> str:
    count = max(1, min(8, int(led_count)))
    percent = max(0, min(100, int(snapshot.percent)))
    fill = battery_fill(percent, count)
    pulse_index = min(fill.full, count - 1)
    transition = max(0, int(transition_ms))

    if not snapshot.is_plugged or snapshot.is_charged:
        color = battery_color(percent)
        if snapshot.is_charged or percent >= 100:
            return apply_brightness(
                ";".join(
                    battery_segment(index, BATTERY_HIGH_GREEN, transition)
                    for index in range(count)
                ),
                brightness,
            )
        return apply_brightness(
            ";".join(
                battery_segment(index, battery_fill_color(index, fill, color), transition)
                for index in range(count)
            ),
            brightness,
        )

    color = BATTERY_CHARGING_MINT
    ratio = snapshot.charge_speed_ratio(full_charge_watts)
    pulse_ms = charging_pulse_length_ms(ratio)
    lines = [
        ";".join(
            battery_segment(
                index,
                battery_fill_color(index, fill, color),
                transition,
            )
            for index in range(count)
        )
    ]
    lines.extend(
        [
            f"{pulse_index}:{color} {pulse_ms}ms pulse",
        ]
    )
    return apply_brightness("\n".join(lines), brightness)


@dataclass(frozen=True)
class BatteryFill:
    full: int
    partial: float
    count: int


def battery_fill(percent: int, led_count: int) -> BatteryFill:
    count = max(1, min(8, int(led_count)))
    clamped = max(0, min(100, int(percent)))
    raw = count * (clamped / 100.0)
    full = min(count, int(math.floor(raw)))
    partial = 0.0 if full >= count else raw - full
    return BatteryFill(full=full, partial=partial, count=count)


def battery_fill_color(index: int, fill: BatteryFill, color: str) -> str:
    if index < fill.full:
        return color
    if index == fill.full and fill.partial > 0:
        return scale_hex_color(color, fill.partial)
    return BATTERY_OFF


def scale_hex_color(color: str, ratio: float) -> str:
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        return color
    amount = max(0.0, min(1.0, ratio))
    channels = [
        round(int(color[index : index + 2], 16) * amount)
        for index in (1, 3, 5)
    ]
    return "#" + "".join(f"{channel:02X}" for channel in channels)


def battery_segment(index: int, color: str, transition_ms: int) -> str:
    if transition_ms <= 0:
        return f"{index}:{color}"
    return f"{index}:{color} {transition_ms}ms ease"


def write_battery_to_leds(
    snapshot: BatterySnapshot,
    *,
    device_path: Path | None = None,
    file_name: str = DEFAULT_FILE_NAME,
    dry_run: bool = False,
    full_charge_watts: float | None = None,
    brightness: int | float = 255,
) -> BatteryLedWrite:
    target = resolve_target_path(device_path=device_path, file_name=file_name)
    program = program_for_battery(
        snapshot,
        led_count=led_count_for_target(target),
        full_charge_watts=full_charge_watts,
        brightness=brightness,
    )
    written_target = write_led_program(
        program,
        device_path=target,
        file_name=file_name,
        dry_run=dry_run,
    )
    return BatteryLedWrite(
        target=written_target,
        program=program,
        changed=True,
    )


class BatteryLedController:
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
        self.last_program: str | None = None
        self.last_error: str | None = None
        self.last_target: Path | None = None
        self.last_attempt_monotonic = 0.0
        self.last_write_monotonic = 0.0

    def reset(self) -> None:
        self.last_program = None
        self.last_error = None
        self.last_target = None
        self.last_attempt_monotonic = 0.0
        self.last_write_monotonic = 0.0

    def sync_snapshot(self, snapshot: BatterySnapshot) -> BatteryLedWrite:
        now = time.monotonic()
        try:
            target = resolve_target_path(device_path=self.device_path, file_name=self.file_name)
            program = program_for_battery(
                snapshot,
                led_count=led_count_for_target(target),
                brightness=self.brightness,
            )
        except (DeviceWriteError, OSError) as exc:
            self.last_error = str(exc)
            return BatteryLedWrite(
                target=self.last_target,
                program="",
                changed=False,
                error=self.last_error,
            )

        if (
            program == self.last_program
            and self.last_error is None
            and not should_rewrite_battery_program(snapshot, now - self.last_write_monotonic)
        ):
            return BatteryLedWrite(
                target=self.last_target,
                program="",
                changed=False,
            )
        if (
            program == self.last_program
            and self.last_error is not None
            and now - self.last_attempt_monotonic < self.error_retry_seconds
        ):
            return BatteryLedWrite(
                target=self.last_target,
                program="",
                changed=False,
                error=self.last_error,
            )

        self.last_attempt_monotonic = now
        try:
            written_target = write_led_program(
                program,
                device_path=target,
                file_name=self.file_name,
                dry_run=self.dry_run,
            )
        except (DeviceWriteError, OSError) as exc:
            self.last_program = program
            self.last_error = str(exc)
            return BatteryLedWrite(
                target=self.last_target,
                program="",
                changed=False,
                error=self.last_error,
            )

        self.last_program = program
        self.last_error = None
        self.last_target = written_target
        self.last_write_monotonic = now
        return BatteryLedWrite(
            target=written_target,
            program=program,
            changed=True,
        )


def render_battery_snapshot(snapshot: BatterySnapshot) -> str:
    state = battery_state_label(snapshot)
    lines = [f"Battery: {snapshot.percent}% ({state})"]
    if snapshot.is_plugged:
        lines.append(
            f"Adapter: {format_watts(snapshot.adapter_power)} "
            f"of {format_watts(snapshot.full_charge_watts)} "
            f"({snapshot.charge_speed_ratio() * 100:.0f}% speed)"
        )
    if abs(snapshot.battery_watts) >= 0.1:
        label = "Battery charging" if snapshot.battery_watts > 0 else "Battery draining"
        lines.append(f"{label}: {format_watts(abs(snapshot.battery_watts))}")
    if snapshot.time_to_full > 0 and snapshot.is_charging:
        lines.append(f"Time to full: {format_minutes(snapshot.time_to_full)}")
    elif snapshot.time_to_empty > 0 and not snapshot.is_plugged:
        lines.append(f"Time remaining: {format_minutes(snapshot.time_to_empty)}")
    if snapshot.health_percent >= 0:
        lines.append(f"Health: {snapshot.health_percent}%")
    if snapshot.cycle_count >= 0:
        lines.append(f"Cycle count: {snapshot.cycle_count}")
    if snapshot.condition:
        lines.append(f"Condition: {snapshot.condition}")
    return "\n".join(lines)


def battery_state_label(snapshot: BatterySnapshot) -> str:
    if snapshot.is_charged:
        return "charged"
    if snapshot.is_charging:
        return "charging"
    if snapshot.is_plugged:
        return "plugged in"
    return "on battery"


def battery_color(percent: int) -> str:
    if percent <= 20:
        return BATTERY_LOW_RED
    if percent <= 50:
        return BATTERY_MID_AMBER
    return BATTERY_HIGH_GREEN


def charging_pulse_length_ms(ratio: float) -> int:
    speed = clamp(ratio)
    return round(180 + (1220 * speed))


def charging_update_interval_seconds(ratio: float) -> float:
    speed = clamp(ratio)
    return max(0.9, 3.0 - (2.1 * speed))


def should_rewrite_battery_program(snapshot: BatterySnapshot, elapsed_seconds: float) -> bool:
    if not snapshot.is_plugged or snapshot.is_charged:
        return False
    return elapsed_seconds >= charging_update_interval_seconds(snapshot.charge_speed_ratio())


def parse_full_watts(value: str | float | int | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return None if float(value) <= 0 else float(value)
    text = value.strip().lower()
    if text in {"", "auto", "default"}:
        return None
    return max(1.0, float(text))


@lru_cache(maxsize=1)
def default_full_charge_watts() -> float:
    product_text = read_product_tree_text()
    product_lower = product_text.lower()
    chip_match = re.search(r'"product-soc-name"\s*=\s*<"([^"]+)">', product_text)
    chip = chip_match.group(1).lower() if chip_match else ""
    if "macbook pro" in product_lower:
        if "16-inch" in product_lower:
            return 140.0
        if "14-inch" in product_lower:
            if "pro" in chip or "max" in chip:
                return 96.0
            return 70.0

    try:
        result = subprocess.run(
            ["system_profiler", "SPHardwareDataType", "-json"],
            check=True,
            capture_output=True,
            text=True,
        )
        data = json.loads(result.stdout)
        items = data.get("SPHardwareDataType") or []
        hardware = items[0] if items else {}
    except Exception:
        hardware = {}

    chip = str(hardware.get("chip_type", "")).lower()
    model = " ".join(
        str(hardware.get(key, ""))
        for key in ("machine_name", "machine_model", "model_number")
    ).lower()
    if "macbook pro" in model:
        if "16-inch" in model:
            return 140.0
        if "14-inch" in model:
            if "pro" in chip or "max" in chip:
                return 96.0
            return 70.0
    return 100.0


def read_product_tree_text() -> str:
    try:
        result = subprocess.run(
            ["ioreg", "-p", "IODeviceTree", "-r", "-d", "1", "-n", "product"],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout
    except Exception:
        return ""


def int_value(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return default


def str_value(value: object) -> str:
    return value if isinstance(value, str) else ""


def temperature_celsius(value: object) -> float | None:
    raw = int_value(value, default=-1)
    if raw < 0:
        return None
    return raw / 100.0


def adapter_family_name(code: int) -> str:
    if 0xE0004000 <= code <= 0xE0004FFF:
        return "USB-C Power Delivery"
    if code == 0x00000003:
        return "MagSafe"
    if code == 0x00000004:
        return "MagSafe 2"
    if code == 0x0000FF00:
        return "USB-C"
    if code >= 0:
        return f"0x{code:08x}"
    return ""


def clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def format_watts(value: float) -> str:
    if value <= 0:
        return "0W"
    if value >= 10:
        return f"{value:.0f}W"
    return f"{value:.1f}W"


def format_minutes(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes}m"
    hours, rest = divmod(minutes, 60)
    return f"{hours}h{rest:02d}m"
