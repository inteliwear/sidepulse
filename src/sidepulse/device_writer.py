from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

DEFAULT_FILE_NAME = "LEDS.LED"
LED_FILE_NAMES = (DEFAULT_FILE_NAME,)
KNOWN_LED_FILE_NAMES = frozenset(name.upper() for name in LED_FILE_NAMES)
MAX_LED_BYTES = 512
MAX_LED_LINES = 20
LED_RECORD_PADDING_BYTE = b" "
MOUNT_ROOT = Path("/Volumes")
DEVICE_NAME_HINTS = (
    "sidepulsepro",
    "sidepulsedot",
    "pulsedot",
)


class DeviceWriteError(RuntimeError):
    pass


@dataclass(frozen=True)
class DeviceCandidate:
    root: Path
    target: Path
    reason: str


def write_led_program(
    text: str,
    *,
    device_path: Path | None = None,
    file_name: str = DEFAULT_FILE_NAME,
    dry_run: bool = False,
) -> Path:
    program = normalize_led_text(text)
    validate_led_text(program)
    target = resolve_target_path(device_path=device_path, file_name=file_name)

    if dry_run:
        return target

    target.parent.mkdir(parents=True, exist_ok=True)
    _write_led_record_in_place(target, program.encode("utf-8"))
    return target


def _write_led_record_in_place(target: Path, program: bytes) -> None:
    """Overwrite the fixed-size LED record without truncating the file first."""
    if len(program) > MAX_LED_BYTES:
        raise DeviceWriteError(
            f"LED program is {len(program)} bytes; max is {MAX_LED_BYTES}."
        )
    record = program.ljust(MAX_LED_BYTES, LED_RECORD_PADDING_BYTE)

    try:
        handle = target.open("r+b", buffering=0)
    except FileNotFoundError:
        # Creation is only a recovery path. Firmware normally provides LEDS.LED.
        handle = target.open("x+b", buffering=0)

    with handle:
        handle.seek(0)
        written = handle.write(record)
        if written != len(record):
            raise DeviceWriteError(
                f"Only wrote {written} of {len(record)} bytes to {target}."
            )

        # Repair an unexpectedly oversized file once. Normal writes stay at the
        # fixed size and therefore never release/reallocate the data cluster.
        handle.seek(0, 2)
        if handle.tell() > MAX_LED_BYTES:
            handle.truncate(MAX_LED_BYTES)


def read_led_program(path: Path) -> str:
    """Read a fixed-size LED record as its logical, unpadded program text."""
    data = path.read_bytes().rstrip(b" \x00\xff")
    return data.decode("utf-8")


def normalize_led_text(text: str) -> str:
    return decode_simple_escapes(text)


def decode_simple_escapes(text: str) -> str:
    output: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char != "\\" or index + 1 >= len(text):
            output.append(char)
            index += 1
            continue

        next_char = text[index + 1]
        if next_char == "n":
            output.append("\n")
            index += 2
        elif next_char == "r":
            output.append("\r")
            index += 2
        elif next_char == "t":
            output.append("\t")
            index += 2
        elif next_char == "\\":
            output.append("\\")
            index += 2
        else:
            output.append(char)
            index += 1
    return "".join(output)


def validate_led_text(text: str) -> None:
    if not text:
        raise DeviceWriteError("LED program is empty.")

    byte_count = len(text.encode("utf-8"))
    if byte_count > MAX_LED_BYTES:
        raise DeviceWriteError(
            f"LED program is {byte_count} bytes; max is {MAX_LED_BYTES}."
        )

    line_count = len(text.splitlines()) or 1
    if line_count > MAX_LED_LINES:
        raise DeviceWriteError(
            f"LED program has {line_count} lines; max is {MAX_LED_LINES}."
        )


def resolve_target_path(
    *,
    device_path: Path | None = None,
    file_name: str = DEFAULT_FILE_NAME,
) -> Path:
    if device_path is not None:
        return target_from_device_path(device_path.expanduser(), file_name)

    candidates = discover_devices(file_name=file_name)
    if not candidates:
        raise DeviceWriteError(
            "No SidePulse Pro or SidePulse Dot device found. "
            "Mount the device, or pass --device /Volumes/SidePulseDot."
        )
    if len(candidates) > 1:
        lines = "\n".join(f"  {candidate.root}" for candidate in candidates)
        raise DeviceWriteError(
            "Multiple possible devices found. Pass --device with one of:\n" + lines
        )
    return candidates[0].target


def target_from_device_path(path: Path, file_name: str) -> Path:
    if path.name.upper() in KNOWN_LED_FILE_NAMES:
        return path
    return path / file_name


def discover_devices(
    *,
    mount_root: Path = MOUNT_ROOT,
    file_name: str = DEFAULT_FILE_NAME,
) -> list[DeviceCandidate]:
    if not mount_root.exists():
        return []

    candidates: list[DeviceCandidate] = []
    for volume in sorted(iter_mounts(mount_root), key=lambda path: path.name.lower()):
        target = target_from_device_path(volume, file_name)
        name_matches = is_device_name(volume.name)
        file_exists = path_exists(target)
        if file_exists:
            candidates.append(DeviceCandidate(volume, target, f"contains {target.name}"))
        elif name_matches:
            candidates.append(DeviceCandidate(volume, target, "name matches device"))
    return candidates

def iter_mounts(mount_root: Path) -> Iterable[Path]:
    try:
        children = list(mount_root.iterdir())
    except OSError:
        return ()

    mounts: list[Path] = []
    for child in children:
        if child.name in {".timemachine", "Macintosh HD"}:
            continue
        try:
            if child.is_dir():
                mounts.append(child)
        except OSError:
            continue
    return mounts


def path_exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


def is_device_name(name: str) -> bool:
    normalized = "".join(char for char in name.lower() if char.isalnum())
    return any(hint in normalized for hint in DEVICE_NAME_HINTS)
