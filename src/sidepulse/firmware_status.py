"""Firmware checks used by the tray's background worker."""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from . import firmware


@dataclass(frozen=True)
class FirmwareStatus:
    device: firmware.FirmwareDevice | None = None
    latest: str | None = None
    error: str = ""

    @property
    def outdated(self) -> bool:
        if self.device is None or self.latest is None:
            return False
        try:
            return firmware.version_key(self.device.version) < firmware.version_key(self.latest)
        except firmware.FirmwareError:
            return False


class FirmwareStatusReader:
    """Read devices afresh; cache successful release checks for six hours."""

    def __init__(self):
        self.latest: dict[str, str] = {}
        self.error = ""
        self.retry_at = 0.0
        self.products: set[str] = set()

    def read(self, roots: tuple[Path, ...]) -> dict[Path, FirmwareStatus]:
        result = {}
        for root in roots:
            try:
                result[root] = FirmwareStatus(device=firmware.read_device(root))
            except (firmware.FirmwareError, OSError) as exc:
                result[root] = FirmwareStatus(error=str(exc))
        products = {status.device.product for status in result.values() if status.device}
        if products and (not products <= self.products or time.monotonic() >= self.retry_at):
            try:
                self.latest = firmware.latest_versions(products)
                self.error = ""
                delay = 6 * 60 * 60
            except (firmware.FirmwareError, OSError) as exc:
                # An unsuccessful check must never label a device up to date.
                self.latest = {}
                self.error = str(exc)
                delay = 5 * 60
            self.products = products
            self.retry_at = time.monotonic() + delay
        statuses = {}
        for root, status in result.items():
            latest = self.latest.get(status.device.product) if status.device else None
            error = status.error or self.error
            if status.device and latest is None and not error:
                error = "No release available"
            statuses[root] = FirmwareStatus(
                device=status.device,
                latest=latest,
                error=error,
            )
        return statuses


def install_update(device: firmware.FirmwareDevice, version: str) -> None:
    """Install only the offered, newer version on the same connected device."""
    if firmware.read_device(device.root) != device:
        raise firmware.FirmwareError("The connected device changed. Check for updates again.")
    if firmware.version_key(version) <= firmware.version_key(device.version):
        raise firmware.FirmwareError("This firmware is no longer newer than the installed version.")
    package = firmware.load_package(device.product, version, None)
    if package.product != device.product or package.version != firmware.normalize_version(version):
        raise firmware.FirmwareError("The firmware package does not match the selected update.")
    firmware.write_firmware(device, package)
