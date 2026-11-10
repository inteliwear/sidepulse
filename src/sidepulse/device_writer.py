from __future__ import annotations

import errno
import hashlib
import os
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

DEFAULT_FILE_NAME = "LEDS.LED"
LED_FILE_NAMES = (DEFAULT_FILE_NAME,)
KNOWN_LED_FILE_NAMES = frozenset(name.upper() for name in LED_FILE_NAMES)
MAX_LED_BYTES = 512
MAX_LED_LINES = 20
MOUNT_ROOT = Path("/Volumes")
# Parent of the names given to Dots driven over USB control. Nothing is ever
# created here: writes to these names go over USB, never to the filesystem.
USB_ROOT = Path("/sidepulse-usb")
DEVICE_NAME_HINTS = (
    "sidepulsepro",
    "sidepulsedot",
    "pulsedot",
)
_WRITE_LOCK = threading.Lock()


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
    return write_normalized_led_program(
        program,
        device_path=device_path,
        file_name=file_name,
        dry_run=dry_run,
    )


def write_normalized_led_program(
    program: str,
    *,
    device_path: Path | None = None,
    file_name: str = DEFAULT_FILE_NAME,
    dry_run: bool = False,
) -> Path:
    validate_led_text(program)
    target = resolve_target_path(device_path=device_path, file_name=file_name)

    if dry_run:
        return target

    location = usb_location(target.parent)
    if location is not None:
        with device_write_lock(target.parent):
            write_usb_program(location, program)
        return target

    with device_write_lock(target.parent):
        target.parent.mkdir(parents=True, exist_ok=True)
        write_text_synced(target, program)
    return target


@contextmanager
def device_write_lock(root: Path):
    """Serialize this app's file/USB/read-channel writers across threads and processes."""
    import fcntl

    key = hashlib.sha256(os.fsencode(root.resolve())).hexdigest()
    directory = Path.home() / ".cache" / "sidepulse" / "device-locks"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _WRITE_LOCK, (directory / key).open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def write_text_synced(target: Path, text: str) -> None:
    """Write text and force it to the device before returning.

    Removable devices buffer writes, so the LED firmware may not see the
    program until the data reaches the disk. Syncing is best effort: the
    bytes are already handed to the OS once the file is closed, so a
    filesystem that refuses to sync must not fail the write.
    """

    is_new = not path_exists(target)
    try:
        handle = target.open("w", encoding="utf-8")
    except OSError as exc:
        if exc.errno not in (errno.EROFS, errno.EACCES, errno.EPERM):
            raise
        from .readonly_control import write_readonly_program

        write_readonly_program(target, text)
        return
    # Only a denied open may change transports. Never replay a partial write.
    with handle:
        handle.write(text)
        handle.flush()
        sync_fd(handle.fileno())

    if is_new:
        sync_directory(target.parent)


def sync_fd(fd: int) -> None:
    try:
        os.fsync(fd)
    except OSError:
        pass


def sync_directory(directory: Path) -> None:
    """Flush a new file's directory entry, where the platform supports it."""

    try:
        fd = os.open(directory, getattr(os, "O_DIRECTORY", os.O_RDONLY))
    except OSError:
        return
    try:
        sync_fd(fd)
    finally:
        os.close(fd)


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
            "Mount the device, or pass --device /path/to/SidePulseDot."
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
    mount_root: Path | None = None,
    file_name: str = DEFAULT_FILE_NAME,
) -> list[DeviceCandidate]:
    candidates: list[DeviceCandidate] = []
    seen: set[Path] = set()
    roots = (mount_root,) if mount_root is not None else default_mount_roots()
    for root in roots:
        if not root.exists():
            continue
        for volume in sorted(iter_mounts(root), key=lambda path: path.name.lower()):
            if volume in seen:
                continue
            seen.add(volume)
            target = target_from_device_path(volume, file_name)
            name_matches = is_device_name(volume.name)
            file_exists = path_exists(target)
            if file_exists:
                candidates.append(
                    DeviceCandidate(volume, target, f"contains {target.name}")
                )
            elif name_matches:
                candidates.append(DeviceCandidate(volume, target, "name matches device"))
    if not candidates and mount_root is None and usb_discovery_enabled(file_name):
        candidates.extend(discover_usb_dots(file_name=file_name))
    return candidates


def usb_discovery_enabled(file_name: str) -> bool:
    """Look for a Dot over USB only when no drive was found and nothing opted out.

    USB control carries LED programs alone, so a caller after another file
    (firmware reads STATUS.TXT) never gets a USB candidate, and an explicit
    SIDEPULSE_MOUNT_ROOTS keeps discovery to the folders it names.
    """
    if file_name.upper() not in KNOWN_LED_FILE_NAMES:
        return False
    if os.environ.get("SIDEPULSE_MOUNT_ROOTS") is not None:
        return False
    return os.environ.get("SIDEPULSE_USB_CONTROL", "1").strip() != "0"


def usb_root(bus: int, ports: tuple[int, ...]) -> Path:
    """A stable name for a Dot driven over USB, keyed by its physical port.

    The path never exists on disk. Its name carries "SidePulseDot" so the LED
    count and display name read as a Dot, and settings saved for it survive a
    replug into the same port.
    """
    port_path = ".".join(str(port) for port in ports) or "0"
    return USB_ROOT / f"SidePulseDot-USB-{bus}-{port_path}"


def usb_location(root: Path) -> tuple[int, tuple[int, ...]] | None:
    if root.parent != USB_ROOT:
        return None
    prefix = "SidePulseDot-USB-"
    if not root.name.startswith(prefix):
        return None
    bus, _, port_path = root.name[len(prefix):].partition("-")
    try:
        ports = tuple(int(part) for part in port_path.split(".")) if port_path != "0" else ()
        return int(bus), ports
    except ValueError:
        return None


def discover_usb_dots(*, file_name: str = DEFAULT_FILE_NAME) -> list[DeviceCandidate]:
    try:
        from .usb_control import list_dots

        dots = list_dots()
    except Exception:
        # No libusb, no permission, or no USB stack: discovery stays silent.
        return []
    candidates = []
    for bus, ports, _address in dots:
        root = usb_root(bus, ports)
        candidates.append(
            DeviceCandidate(root, root / file_name, "USB control, drive not mounted")
        )
    return candidates


def write_usb_program(location: tuple[int, tuple[int, ...]], program: str) -> None:
    from .usb_control import Device, list_dots

    bus, ports = location
    try:
        addresses = [
            address for found_bus, found_ports, address in list_dots()
            if (found_bus, found_ports) == (bus, ports)
        ]
        if len(addresses) != 1:
            raise DeviceWriteError("The SidePulse Dot is no longer connected on that USB port.")
        with Device(bus=bus, address=addresses[0]) as device:
            device.write(program.encode("utf-8"))
    except DeviceWriteError:
        raise
    except (OSError, ValueError, RuntimeError) as exc:
        raise DeviceWriteError(f"USB LED control failed: {exc}") from exc


def default_mount_roots(
    *,
    platform: str | None = None,
    home: Path | None = None,
) -> tuple[Path, ...]:
    configured = os.environ.get("SIDEPULSE_MOUNT_ROOTS")
    if configured is not None:
        return tuple(
            Path(value).expanduser()
            for value in configured.split(os.pathsep)
            if value.strip()
        )

    current_platform = platform or sys.platform
    if current_platform == "darwin":
        return (MOUNT_ROOT,)
    if current_platform.startswith("linux"):
        username = (home or Path.home()).name
        return unique_paths(
            (
                Path("/media") / username,
                Path("/run/media") / username,
                Path("/media"),
                Path("/run/media"),
                Path("/mnt"),
            )
        )
    return (MOUNT_ROOT,)


def unique_paths(paths: Iterable[Path]) -> tuple[Path, ...]:
    result: list[Path] = []
    for path in paths:
        if path not in result:
            result.append(path)
    return tuple(result)


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
