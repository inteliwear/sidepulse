"""Cache Dot metadata until an explicit device refresh."""
from pathlib import Path
import threading

from . import usb_control as usb

_lock = threading.Lock()
_devices = None


def dots(*, force=False):
    global _devices
    with _lock:
        if not force and _devices is not None:
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
        return _devices


def info_for_selector(root: Path):
    address = usb.address_from_selector(root)
    if address is None:
        return None
    return next((info for info in dots() if (info['bus'], info['address']) == address), None)
