"""Briefly cache Dot metadata used repeatedly by the tray."""
from pathlib import Path
import threading
import time

from . import usb_control as usb

_lock = threading.Lock()
_expires = 0.0
_devices = ()


def dots(*, force=False):
    global _devices, _expires
    with _lock:
        if not force and time.monotonic() < _expires:
            return _devices
        devices = []
        try:
            addresses = usb.enumerate_addresses()
        except (ImportError, OSError):
            addresses = []
        for bus, address in sorted(set(addresses)):
            try:
                with usb.Device(bus=bus, address=address) as device:
                    info = device.info()
                devices.append(dict(info, bus=bus, address=address,
                                    root=usb.USB_DEVICE_ROOT / f'Dot-{bus:03d}-{address:03d}'))
            except (ImportError, OSError, ValueError, RuntimeError):
                continue
        _devices = tuple(devices)
        _expires = time.monotonic() + 1
        return _devices


def info_for_selector(root: Path):
    address = usb.address_from_selector(root)
    if address is None:
        return None
    return next((info for info in dots() if (info['bus'], info['address']) == address), None)
