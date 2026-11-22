from __future__ import annotations

import argparse
import errno
import hashlib
import http.client
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from subprocess import SubprocessError

from .device_writer import device_write_lock, discover_devices, discover_mounted_devices, sync_directory
from .status_reader import read_status_bytes


RELEASES_URL = "https://api.github.com/repos/inteliwear/sidepulse/contents/firmware?ref=main"
DOWNLOAD_ROOT = "https://raw.githubusercontent.com/inteliwear/sidepulse/main/firmware"
MAX_PACKAGE_BYTES = 4 * 1024 * 1024
PRODUCT_NAMES = {"dot": "SidePulse Dot", "pro": "SidePulse Pro"}
USB_DEVICE_ROOT = Path("/dev/sidepulse-usb")
PACKAGE_FILES = {"FIRMWARE.BIN", "README.txt", "RELEASE_NOTES.txt", "SHA256SUMS.txt"}


class FirmwareError(RuntimeError):
    pass


@dataclass(frozen=True)
class FirmwareDevice:
    root: Path
    product: str
    version: str
    serial: str

    @property
    def name(self) -> str:
        return PRODUCT_NAMES[self.product]


@dataclass(frozen=True)
class FirmwarePackage:
    product: str
    version: str
    payload: bytes


def normalize_version(value: str) -> str:
    match = re.fullmatch(r"v?(\d+)\.(\d+)(?:\.(\d+))?", value)
    if not match:
        raise FirmwareError(f"Invalid firmware version: {value!r}. Use a version such as 1.1.0.")
    return ".".join(str(int(part or 0)) for part in match.groups())


def version_key(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in normalize_version(value).split("."))


def _usb_address(root: Path) -> tuple[int, int] | None:
    from .usb_control import address_from_selector

    try:
        return address_from_selector(root)
    except ValueError as exc:
        raise FirmwareError(str(exc)) from exc


def read_usb_device(bus: int, address: int) -> FirmwareDevice:
    from .usb_control import Device

    root = USB_DEVICE_ROOT / f"Dot-{bus:03d}-{address:03d}"
    try:
        with Device(bus=bus, address=address) as device:
            info = device.info()
    except (ImportError, OSError, ValueError, RuntimeError) as exc:
        raise FirmwareError(f"Could not read Dot firmware over USB: {exc}") from exc
    return FirmwareDevice(root, "dot", info["version"], info["serial"])


def find_usb_devices() -> list[FirmwareDevice]:
    from .usb_discovery import dots

    return [FirmwareDevice(info['root'], "dot", info['version'], info['serial'])
            for info in dots(force=True) if info.get('storage_active') is False]


def read_device(root: Path) -> FirmwareDevice:
    root = root.expanduser()
    address = _usb_address(root)
    if address is not None:
        return read_usb_device(*address)
    root = root.resolve()
    try:
        text = read_status_bytes(root / "STATUS.TXT", max_bytes=65536).decode("utf-8", errors="replace")
    except OSError as exc:
        raise FirmwareError(f"Cannot read {root / 'STATUS.TXT'}: {exc}") from exc
    fields = {}
    for line in text.replace("\x00", "").splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2:
            fields[parts[0]] = parts[1]
    dot = "app_version" in fields or ("app_build" in fields and "fw_state" in fields)
    pro = "release_version" in fields or ("firmware_version" in fields and "firmware_slot" in fields)
    if dot == pro:
        raise FirmwareError(f"Cannot identify a SidePulse Dot or Pro from {root / 'STATUS.TXT'}.")
    product = "dot" if dot else "pro"
    version = fields.get("app_version" if dot else "release_version", "unknown")
    return FirmwareDevice(root, product, version, fields.get("serial", "unassigned"))


def find_devices(device_path: Path | None) -> list[FirmwareDevice]:
    if device_path is not None:
        return [read_device(device_path)]
    devices = []
    for candidate in discover_devices():
        try:
            devices.append(read_device(candidate.root))
        except FirmwareError:
            continue
    if not devices:
        raise FirmwareError("No mounted SidePulse device or USB-control Dot found. Connect it or pass --device with its drive or USB selector.")
    return devices


def download(url: str, *, limit: int = MAX_PACKAGE_BYTES) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "sidepulse-cli", "Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read(limit + 1)
    except (OSError, urllib.error.URLError, http.client.HTTPException) as exc:
        raise FirmwareError(f"Could not download firmware release data: {exc}. You can use --file with a local OTA ZIP.") from exc
    if len(data) > limit:
        raise FirmwareError("Firmware download exceeds the size limit.")
    return data


def published_versions() -> list[str]:
    try:
        entries = json.loads(download(RELEASES_URL, limit=1024 * 1024))
    except (ValueError, UnicodeError) as exc:
        raise FirmwareError("The firmware release listing is invalid.") from exc
    if not isinstance(entries, list):
        raise FirmwareError("The firmware release listing is invalid.")
    versions = [
        entry["name"][1:]
        for entry in entries
        if isinstance(entry, dict) and entry.get("type") == "dir"
        and re.fullmatch(r"v\d+\.\d+\.\d+", str(entry.get("name", "")))
    ]
    if not versions:
        raise FirmwareError("No published firmware releases found.")
    return sorted(set(versions), key=version_key, reverse=True)


def latest_versions(products: set[str]) -> dict[str, str]:
    """Find the latest release for each model without downloading OTA images."""
    remaining = set(products)
    result = {}
    for release in published_versions():
        checksums = parse_checksums(download(f"{DOWNLOAD_ROOT}/v{release}/SHA256SUMS.txt", limit=65536))
        for product in tuple(remaining):
            if f"sidepulse-{product}-{release}-ota.zip" in checksums:
                result[product] = release
                remaining.remove(product)
        if not remaining:
            break
    return result


def parse_checksums(data: bytes) -> dict[str, str]:
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeError as exc:
        raise FirmwareError("Invalid firmware checksums.") from exc
    result = {}
    for line in lines:
        if not line.strip():
            continue
        match = re.fullmatch(r"([0-9a-fA-F]{64})  ([A-Za-z0-9_.-]+)", line)
        if not match or match[2] in result:
            raise FirmwareError("Invalid firmware checksums.")
        result[match[2]] = match[1].lower()
    return result


def verify_checksum(data: bytes, expected: str | None, name: str) -> None:
    if expected is None or hashlib.sha256(data).hexdigest() != expected:
        raise FirmwareError(f"Firmware checksum mismatch: {name}.")


def read_package(data: bytes, filename: str) -> FirmwarePackage:
    match = re.fullmatch(r"sidepulse-(dot|pro)-(\d+\.\d+\.\d+)-ota\.zip", filename)
    if not match:
        raise FirmwareError("Use a customer OTA ZIP named sidepulse-dot-VERSION-ota.zip or sidepulse-pro-VERSION-ota.zip.")
    product, version = match.groups()
    if len(data) > MAX_PACKAGE_BYTES:
        raise FirmwareError("Firmware package exceeds the size limit.")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
            prefix = "" if set(names) == PACKAGE_FILES else filename[:-4] + "/"
            if len(names) != 4 or set(names) != {prefix + name for name in PACKAGE_FILES}:
                raise FirmwareError("Unexpected files in firmware package.")
            if sum(info.file_size for info in archive.infolist()) > MAX_PACKAGE_BYTES:
                raise FirmwareError("Expanded firmware package exceeds the size limit.")
            contents = {name: archive.read(prefix + name) for name in PACKAGE_FILES}
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError, zlib.error) as exc:
        raise FirmwareError(f"Invalid firmware ZIP: {exc}") from exc
    checksums = parse_checksums(contents["SHA256SUMS.txt"])
    if set(checksums) != PACKAGE_FILES - {"SHA256SUMS.txt"}:
        raise FirmwareError("Firmware package checksums are incomplete.")
    for name, digest in checksums.items():
        verify_checksum(contents[name], digest, name)
    title = f"{PRODUCT_NAMES[product]} firmware update - version {version}"
    if contents["README.txt"].splitlines()[:1] != [title.encode()]:
        raise FirmwareError("Firmware package model or version does not match its filename.")
    if not contents["FIRMWARE.BIN"]:
        raise FirmwareError("Firmware image is empty.")
    return FirmwarePackage(product, version, contents["FIRMWARE.BIN"])


def load_package(product: str, version: str | None, path: Path | None) -> FirmwarePackage:
    if path is not None:
        path = path.expanduser()
        with path.open("rb") as stream:
            return read_package(stream.read(MAX_PACKAGE_BYTES + 1), path.name)
    releases = [normalize_version(version)] if version else published_versions()
    for release in releases:
        name = f"sidepulse-{product}-{release}-ota.zip"
        base = f"{DOWNLOAD_ROOT}/v{release}"
        checksums = parse_checksums(download(f"{base}/SHA256SUMS.txt", limit=65536))
        if name not in checksums:
            continue
        data = download(f"{base}/{name}")
        verify_checksum(data, checksums[name], name)
        return read_package(data, name)
    requested = f" for {normalize_version(version)}" if version else ""
    raise FirmwareError(f"No published {PRODUCT_NAMES[product]} firmware package found{requested}.")


def _dot_upgrade_drive(device: FirmwareDevice, address: tuple[int, int]) -> FirmwareDevice:
    from . import storage, usb_control

    matches = [dot for dot in storage.find_dots() if dot["serial"] == device.serial]
    if (device.serial == "unassigned" or len(matches) != 1
            or (matches[0]["bus"], matches[0]["address"]) != address):
        raise FirmwareError("Cannot uniquely identify this Dot after reconnecting. Select its mounted drive.")
    original = matches[0]
    connected = original
    restore_off = False
    try:
        with usb_control.Device(bus=address[0], address=address[1]) as usb:
            _check_dot_identity(usb, device)
            status = usb.storage_status()
            if status["state"] != "READY":
                raise FirmwareError("The Dot storage setting is busy or failed. Check sidepulse storage status.")
            if not status["active"]:
                print(f"Enabling USB storage for the update on {device.serial}.", flush=True)
                restore_off = not status["saved"]
                if restore_off:
                    usb.set_storage(True)
                usb.reconnect()
        if not status["active"]:
            connected = storage.wait_for_storage(original, True)
    finally:
        if restore_off:
            # Restore the saved preference before sending any firmware bytes,
            # even if the reconnect failed or its acknowledgement was lost.
            # The active drive stays available until the next device restart.
            try:
                matches = [dot for dot in storage.find_dots() if dot["serial"] == device.serial]
                if len(matches) != 1:
                    raise FirmwareError("Cannot identify the reconnected Dot.")
                with usb_control.Device(bus=matches[0]["bus"], address=matches[0]["address"]) as usb:
                    _check_dot_identity(usb, device)
                    status_now = usb.storage_status()
                    if status_now["state"] != "READY":
                        raise FirmwareError("The storage preference is still pending.")
                    if status_now["saved"]:
                        usb.set_storage(False)
            except (OSError, RuntimeError, ValueError) as exc:
                raise FirmwareError(f"Could not restore the storage preference: {exc}. "
                                    f"Check sidepulse storage status and run "
                                    f"sidepulse storage off --device {device.serial}.") from exc

    return _wait_for_dot_drive(device, (connected["bus"], connected["address"]))


def _check_dot_identity(usb, device: FirmwareDevice) -> None:
    info = usb.info()
    if (info["serial"], info["version"]) != (device.serial, device.version):
        raise FirmwareError("The selected Dot changed before the upgrade.")


def _wait_for_dot_drive(device: FirmwareDevice, address: tuple[int, int]) -> FirmwareDevice:
    from .readonly_control import usb_address_for_volume

    deadline = time.monotonic() + 10
    while True:
        drives = []
        for candidate in discover_mounted_devices():
            try:
                mounted = read_device(candidate.root)
            except (FirmwareError, OSError):
                continue
            if mounted.product == "dot" and mounted.serial == device.serial:
                drives.append(mounted)
        if drives:
            if len(drives) != 1 or drives[0].version != device.version:
                raise FirmwareError("Cannot uniquely identify the original Dot on a mounted drive.")
            if sys.platform == "darwin" or sys.platform.startswith("linux"):
                if usb_address_for_volume(drives[0].root) != address:
                    raise FirmwareError("The mounted drive belongs to another USB connection.")
            return drives[0]
        if time.monotonic() >= deadline:
            raise FirmwareError(f"No mounted drive appeared for {device.serial}. Check host storage access "
                                "and reconnect the Dot to apply its saved storage setting.")
        time.sleep(0.25)


def write_firmware(device: FirmwareDevice, package: FirmwarePackage, *, read_only: bool = False) -> None:
    if package.product != device.product:
        raise FirmwareError("The firmware package does not match the device model.")
    if read_only:
        with device_write_lock(device.root):
            _readonly_firmware(device, package)
        return
    address = _usb_address(device.root)
    if address is not None:
        try:
            with device_write_lock(device.root):
                device = _dot_upgrade_drive(device, address)
        except FirmwareError:
            raise
        except (ImportError, OSError, ValueError, RuntimeError, SubprocessError) as exc:
            raise FirmwareError(f"Could not prepare the Dot drive: {exc}") from exc
    with device_write_lock(device.root):
        _write_mounted_firmware(device, package)


def _write_mounted_firmware(device: FirmwareDevice, package: FirmwarePackage) -> None:
    if read_device(device.root) != device:
        raise FirmwareError("The connected device changed. Run the upgrade again.")
    target = device.root / "FIRMWARE.BIN"
    if target.is_symlink():
        raise FirmwareError("The firmware target must not be a symbolic link.")
    try:
        stream = target.open("wb")
    except OSError as exc:
        if exc.errno != errno.EROFS:
            raise
        _readonly_firmware(device, package)
        return
    try:
        with stream:
            stream.write(package.payload)
            stream.flush()
            os.fsync(stream.fileno())
        sync_directory(device.root)
    except OSError as exc:
        raise FirmwareError(
            f"Firmware transfer did not finish: {exc}. Keep the device connected for at least "
            "10 seconds, then reconnect and check its version before retrying."
        ) from exc


def _readonly_firmware(device: FirmwareDevice, package: FirmwarePackage, *, dry_run: bool = False) -> None:
    from ._vendor.read2me import Channel

    if device.product != "pro":
        raise FirmwareError("Dot firmware updates require a writable drive.")
    if read_device(device.root) != device:
        raise FirmwareError("The connected device changed. Run the upgrade again.")
    control = device.root / "setup.html"
    if control.is_symlink():
        raise FirmwareError("The firmware control file must not be a symbolic link.")
    try:
        with Channel(control) as channel:
            if dry_run:
                status = channel.status()
                if status["state"] == "VALIDATING":
                    raise FirmwareError("The Pro is validating another transfer.")
                if status["capacity"] < len(package.payload):
                    raise FirmwareError("The firmware image exceeds the Pro receiver capacity.")
            else:
                channel.upload(package.payload, "FIRMWARE.BIN")
    except (OSError, RuntimeError, ValueError) as exc:
        raise FirmwareError(f"Read-only Pro firmware update failed: {exc}") from exc


def cmd_firmware_version(args: argparse.Namespace) -> int:
    try:
        devices = find_devices(args.device)
    except (FirmwareError, OSError) as exc:
        print(f"SidePulse firmware: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps([
            {"model": device.name, "version": device.version, "serial": device.serial, "device": str(device.root)}
            for device in devices
        ]))
    else:
        for device in devices:
            print(f"{device.name}: {device.version}  ({device.root}, serial {device.serial})")
    return 0


def cmd_firmware_upgrade(args: argparse.Namespace) -> int:
    try:
        devices = find_devices(args.device)
        if len(devices) > 1 and not args.all:
            paths = "\n".join(f"  {device.root}" for device in devices)
            raise FirmwareError("Multiple SidePulse devices found. Select one with --device or upgrade all with --all:\n" + paths)
    except (FirmwareError, OSError) as exc:
        print(f"SidePulse firmware upgrade failed: {exc}", file=sys.stderr)
        return 1

    packages = {}
    failed = False
    for device in devices:
        print(f"{device.name}: firmware {device.version} ({device.root}, serial {device.serial})", flush=True)
        try:
            if device.product not in packages:
                packages[device.product] = load_package(device.product, args.release_version, args.file)
            _upgrade_device(device, packages[device.product], dry_run=args.dry_run, read_only=args.read_only)
        except (FirmwareError, OSError) as exc:
            print(f"SidePulse firmware upgrade failed for {device.root}: {exc}", file=sys.stderr)
            failed = True
    return int(failed)


def _upgrade_device(device: FirmwareDevice, package: FirmwarePackage, *, dry_run: bool, read_only: bool) -> None:
    if package.product != device.product:
        raise FirmwareError(f"This ZIP is for {PRODUCT_NAMES[package.product]}, but the device is {device.name}.")
    try:
        current = version_key(device.version)
    except FirmwareError:
        current = None
    target = version_key(package.version)
    if current == target:
        print(f"{device.name} is already on firmware {package.version}.")
        return
    if current is not None and current > target:
        raise FirmwareError(f"Firmware {package.version} is older than installed version {device.version}; downgrade refused.")
    if dry_run:
        if _usb_address(device.root) is None and hasattr(os, "statvfs"):
            read_only = read_only or bool(os.statvfs(device.root).f_flag & os.ST_RDONLY)
        if read_only:
            _readonly_firmware(device, package, dry_run=True)
        print(f"Would upgrade {device.name} from {device.version} to {package.version}. Package verified; no files written.")
        return
    print(f"Sending firmware {package.version}. Keep the device connected.", flush=True)
    write_firmware(device, package, read_only=read_only)
    print(
        f"Firmware {package.version} transferred. Leave the device connected for at least 10 seconds "
        "while it applies the update, then reconnect and run `sidepulse firmware version` "
        "to confirm installation."
    )


def add_firmware_parser(subparsers: argparse._SubParsersAction) -> None:
    firmware = subparsers.add_parser("firmware", help="Show or upgrade device firmware.")
    commands = firmware.add_subparsers(dest="firmware_command", required=True)
    version = commands.add_parser("version", help="Show firmware versions of connected devices.")
    version.add_argument("--device", type=Path, help="Mounted SidePulse drive. Default: all detected devices.")
    version.add_argument("--json", action="store_true", help="Print device versions as JSON.")
    version.set_defaults(func=cmd_firmware_version)
    upgrade = commands.add_parser("upgrade", help="Install the latest published firmware for a connected device.")
    selection = upgrade.add_mutually_exclusive_group()
    selection.add_argument("--device", type=Path, help="Select a mounted SidePulse drive or Dot USB selector.")
    selection.add_argument("--all", action="store_true", help="Upgrade every detected SidePulse device.")
    source = upgrade.add_mutually_exclusive_group()
    source.add_argument("--version", dest="release_version", help="Install a specific release, such as 1.1.0.")
    source.add_argument("--file", type=Path, help="Install a local customer OTA ZIP.")
    upgrade.add_argument("--read-only", action="store_true", help="Use only the Pro read-only firmware channel (macOS).")
    upgrade.add_argument("--dry-run", action="store_true", help="Download and verify the package without writing to the device.")
    upgrade.set_defaults(func=cmd_firmware_upgrade)
