"""Automatic LED-only transport fallback for denied filesystem write-open."""
from __future__ import annotations

import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys

from .device_writer import DeviceWriteError
from .status_reader import read_status_bytes

# Accessed with device_write_lock held. A service tick is not authorization to
# resubmit a USB command whose acknowledgement was lost. An explicit new CLI
# invocation or a different program can start a new command.
_uncertain_programs: dict[Path, bytes] = {}


def read_device_info(root: Path) -> tuple[str, str]:
    data = read_status_bytes(root / "STATUS.TXT")
    fields = {}
    for line in data.decode("ascii", errors="replace").splitlines():
        pair = line.strip("\0 \r").split(None, 1)
        if len(pair) == 2:
            fields[pair[0]] = pair[1]
    # These are the production status formats, including unassigned devices.
    dot = "app_version" in fields
    pro = "release_version" in fields and "firmware_version" in fields
    if dot == pro:
        raise DeviceWriteError("Cannot identify SidePulse model and firmware from STATUS.TXT.")
    if dot:
        model, version = "Dot", fields["app_version"]
    else:
        model, version = "Pro", fields["release_version"]
    match = re.fullmatch(r"v?(\d+)\.(\d+)(?:\.(\d+))?(-[^+\s]+)?(?:\+[^\s]+)?", version)
    if match:
        number = tuple(int(part or 0) for part in match.group(1, 2, 3))
    if not match or number < (1, 1, 0) or (number == (1, 1, 0) and match.group(4)):
        raise DeviceWriteError(
            f"Read-only control requires SidePulse firmware 1.1 or newer; {model} reports {version}. "
            "Update the firmware using a computer that permits storage writes."
        )
    return model, version


def write_readonly_program(target: Path, text: str) -> None:
    """Called only after write-open was denied, with the writer lock held."""
    if target.name.upper() != "LEDS.LED":
        raise DeviceWriteError("Automatic read-only fallback supports LEDS.LED only.")
    try:
        model, _ = read_device_info(target.parent)
        if model == "Pro":
            from ._vendor.read2me import Channel

            with Channel(target.parent / "setup.html") as channel:
                channel.upload(text.encode("utf-8"), "LEDS.LED")
        else:
            from .usb_control import Device, UncertainUSBWriteError

            key = target.parent.resolve()
            payload = text.encode("utf-8")
            if _uncertain_programs.get(key) == payload:
                raise DeviceWriteError(
                    "Previous USB LED command has an uncertain result; automatic replay stopped. "
                    "Select a different program or retry explicitly with sidepulse write."
                )
            _uncertain_programs.pop(key, None)
            bus, address = usb_address_for_volume(target.parent)
            with Device(bus=bus, address=address) as device:
                try:
                    device.write(payload)
                except UncertainUSBWriteError:
                    _uncertain_programs[key] = payload
                    raise
    except DeviceWriteError:
        raise
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        raise DeviceWriteError(f"Read-only LED control failed for {target.parent}: {exc}") from exc


def _plist_command(command: list[str]):
    return plistlib.loads(subprocess.run(
        command, check=True, capture_output=True, timeout=5,
    ).stdout)


def usb_address_for_volume(root: Path) -> tuple[int, int]:
    """Resolve the selected mounted volume, never the first matching USB VID/PID."""
    if sys.platform == "darwin":
        info = _plist_command(["/usr/sbin/diskutil", "info", "-plist", str(root)])
        mount = info.get("MountPoint")
        if not mount or Path(mount).resolve() != root.resolve():
            raise DeviceWriteError("The selected Dot path is not a mounted volume.")
        disk = info.get("DeviceIdentifier")
        if not disk:
            raise DeviceWriteError("Cannot identify the mounted Dot disk.")
        tree = _plist_command([
            "/usr/sbin/ioreg", "-a", "-l", "-p", "IOService", "-r", "-c", "IOUSBHostDevice",
        ])
        matches = set()

        def visit(node, usb=None):
            if node.get("IOObjectClass") == "IOUSBHostDevice":
                usb = node
            if node.get("BSD Name") == disk and usb is not None:
                if (usb.get("idVendor"), usb.get("idProduct")) == (0x1A86, 0xFE10):
                    location, address = usb.get("locationID"), usb.get("USB Address")
                    if isinstance(location, int) and isinstance(address, int):
                        # libusb's Darwin bus number is the high byte of locationID.
                        matches.add((location >> 24, address))
            for child in node.get("IORegistryEntryChildren", []):
                visit(child, usb)

        for node in tree:
            visit(node)
        if len(matches) == 1:
            return matches.pop()
    elif sys.platform.startswith("linux"):
        if not os.path.ismount(root):
            raise DeviceWriteError("The selected Dot path is not a mounted volume.")
        dev = root.stat().st_dev
        node = (Path("/sys/dev/block") / f"{os.major(dev)}:{os.minor(dev)}").resolve()
        for parent in (node, *node.parents):
            if (parent / "idVendor").exists() and (parent / "idProduct").exists():
                vid = int((parent / "idVendor").read_text().strip(), 16)
                pid = int((parent / "idProduct").read_text().strip(), 16)
                if (vid, pid) == (0x1A86, 0xFE10):
                    return (int((parent / "busnum").read_text()),
                            int((parent / "devnum").read_text()))
                break
    raise DeviceWriteError("Cannot uniquely match the mounted Dot to its USB connection.")
