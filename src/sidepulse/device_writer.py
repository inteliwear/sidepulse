from __future__ import annotations

import errno
import hashlib
import os
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from subprocess import SubprocessError
from typing import Iterable

DEFAULT_FILE_NAME = "LEDS.LED"
LED_FILE_NAMES = (DEFAULT_FILE_NAME,)
KNOWN_LED_FILE_NAMES = frozenset(name.upper() for name in LED_FILE_NAMES)
MAX_LED_BYTES = 512
MAX_LED_LINES = 20
MOUNT_ROOT = Path("/Volumes")
DEVICE_NAME_HINTS = (
    "sidepulsepro",
    "sidepulsedot",
    "pulsedot",
)
_WRITE_LOCK = threading.Lock()
_uncertain_usb_programs: dict[Path, str] = {}
_usb_acknowledgements: dict[Path, tuple[str, dict]] = {}


class DeviceWriteError(RuntimeError):
    pass


@dataclass(frozen=True)
class DeviceCandidate:
    root: Path
    target: Path
    reason: str
    device_id: str | None = None
    model: str = ''
    version: str = 'unknown'
    serial: str = 'unassigned'
    connection: str = 'drive'
    led_count: int | None = None


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

    from .usb_control import Device, UncertainUSBWriteError, address_from_selector

    address = address_from_selector(target)
    if address is not None:
        try:
            with device_write_lock(target):
                if _uncertain_usb_programs.get(target) == program:
                    raise DeviceWriteError('Previous USB LED command has an uncertain result; automatic replay stopped.')
                _uncertain_usb_programs.pop(target, None)
                with Device(bus=address[0], address=address[1]) as device:
                    try:
                        status = device.write(program.encode('utf-8'))
                    except UncertainUSBWriteError:
                        _uncertain_usb_programs[target] = program
                        _usb_acknowledgements.pop(target, None)
                        raise
                    _usb_acknowledgements[target] = (program, status)
        except (ImportError, OSError, ValueError, RuntimeError) as exc:
            raise DeviceWriteError(f'USB LED control failed for {target}: {exc}') from exc
    else:
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
    from .usb_control import address_from_selector

    try:
        address = address_from_selector(path)
    except ValueError as exc:
        raise DeviceWriteError(str(exc)) from exc
    if address is not None:
        if file_name.upper() != DEFAULT_FILE_NAME:
            raise DeviceWriteError('USB control supports live LEDS.LED programs only.')
        return path
    if path.name.upper() in KNOWN_LED_FILE_NAMES:
        return path
    return path / file_name


def discover_mounted_devices(
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
            file_exists = path_exists(target) or (
                file_name.upper() == DEFAULT_FILE_NAME and path_exists(volume / 'STATUS.TXT')
            )
            if file_exists:
                candidates.append(
                    DeviceCandidate(volume, target, f"contains {target.name}")
                )
            elif name_matches:
                candidates.append(DeviceCandidate(volume, target, "name matches device"))
    return candidates


def discover_devices(
    *,
    mount_root: Path | None = None,
    file_name: str = DEFAULT_FILE_NAME,
) -> list[DeviceCandidate]:
    """Shared device discovery for the CLI, tray, service and Python API."""
    from .usb_discovery import dots

    candidates = [describe_mounted_device(candidate) for candidate in discover_mounted_devices(
        mount_root=mount_root, file_name=file_name,
    )]
    if mount_root is not None or 'SIDEPULSE_MOUNT_ROOTS' in os.environ or file_name.upper() != DEFAULT_FILE_NAME:
        return candidates
    mounted_serials = {item.serial for item in candidates if item.serial != 'unassigned'}
    mounted_addresses = set()
    from .readonly_control import usb_address_for_volume

    for item in candidates:
        if item.model == 'SidePulse Dot' and item.serial == 'unassigned':
            try:
                mounted_addresses.add(usb_address_for_volume(item.root))
            except (OSError, ValueError, RuntimeError, SubprocessError):
                pass
    for info in dots():
        if info['serial'] in mounted_serials or (info['bus'], info['address']) in mounted_addresses:
            continue
        root = info['root']
        identity = f"sidepulse-usb:{info['serial']}" if info['serial'] != 'unassigned' else str(root)
        candidates.append(DeviceCandidate(root, root, 'USB control', identity,
                                          'SidePulse Dot', info['version'], info['serial'], 'USB', info['led_count']))
    return candidates


def describe_mounted_device(candidate: DeviceCandidate) -> DeviceCandidate:
    from .status_reader import read_status_bytes

    fields = {}
    try:
        for line in read_status_bytes(candidate.root / 'STATUS.TXT', max_bytes=65536).decode('utf-8', errors='replace').replace('\0', '').splitlines():
            pair = line.strip().split(None, 1)
            if len(pair) == 2:
                fields[pair[0]] = pair[1]
    except OSError:
        pass
    name = ''.join(char for char in candidate.root.name.lower() if char.isalnum())
    dot = 'app_version' in fields or 'pulsedot' in name
    pro = 'release_version' in fields or 'sidepulse' in name and not dot
    model = 'SidePulse Dot' if dot else 'SidePulse Pro' if pro else candidate.root.name
    return replace(candidate, model=model, version=fields.get('app_version' if dot else 'release_version', 'unknown'),
                   serial=fields.get('serial', 'unassigned'), led_count=2 if dot else 8)


# Compatibility name; there is one discovery implementation.
discover_control_devices = discover_devices


def control_target_exists(target: Path) -> bool:
    from .usb_control import address_from_selector
    from .usb_discovery import info_for_selector

    if address_from_selector(target) is not None:
        return info_for_selector(target) is not None
    return path_exists(target.parent)


def usb_program_is_current(target: Path, program: str) -> bool:
    from .usb_control import Device, address_from_selector

    previous = _usb_acknowledgements.get(target)
    if previous is None or previous[0] != program:
        return False
    address = address_from_selector(target)
    with Device(bus=address[0], address=address[1]) as device:
        status = device.status()
    return (status['state'] == 'APPLIED' and status['id'] == previous[1]['id']
            and status['applied'] == previous[1]['applied'])


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
