from pathlib import Path
from unittest.mock import patch

import pytest

from sidepulse import firmware
from sidepulse.firmware_status import FirmwareStatus, FirmwareStatusReader, install_update


ROOT = Path("/Volumes/SidePulse")
DEVICE = firmware.FirmwareDevice(ROOT, "pro", "1.0.5", "SP-123")


def test_latest_versions_uses_each_models_latest_package_without_images():
    manifests = [
        f'{"a" * 64}  sidepulse-dot-1.1.14-ota.zip\n'.encode(),
        f'{"b" * 64}  sidepulse-pro-1.1.0-ota.zip\n'.encode(),
    ]
    with patch.object(firmware, "published_versions", return_value=["1.1.14", "1.1.0"]), patch.object(
        firmware, "download", side_effect=manifests,
    ) as download:
        assert firmware.latest_versions({"dot", "pro"}) == {"dot": "1.1.14", "pro": "1.1.0"}
    assert all(call.args[0].endswith("/SHA256SUMS.txt") for call in download.call_args_list)


@pytest.mark.parametrize("current,latest,outdated", [
    ("1.9.0", "1.10.0", True), ("1.1.0", "1.1.0", False),
    ("1.2.0", "1.1.0", False), ("unknown", "1.1.0", False),
])
def test_only_known_older_versions_offer_upgrades(current, latest, outdated):
    device = firmware.FirmwareDevice(ROOT, "pro", current, "SP-123")
    assert FirmwareStatus(device, latest).outdated is outdated


def test_reader_caches_releases_but_rereads_device_version():
    reader = FirmwareStatusReader()
    newer = firmware.FirmwareDevice(ROOT, "pro", "1.1.0", "SP-123")
    with patch.object(firmware, "read_device", side_effect=[DEVICE, newer]), patch.object(
        firmware, "latest_versions", return_value={"pro": "1.1.0"},
    ) as latest:
        assert reader.read((ROOT,))[ROOT].outdated
        assert not reader.read((ROOT,))[ROOT].outdated
        latest.assert_called_once_with({"pro"})


def test_reader_keeps_installed_version_offline_and_retries_after_backoff():
    reader = FirmwareStatusReader()
    with patch.object(firmware, "read_device", return_value=DEVICE), patch.object(
        firmware, "latest_versions", side_effect=[firmware.FirmwareError("offline"), {"pro": "1.1.0"}],
    ) as latest, patch("sidepulse.firmware_status.time.monotonic", return_value=100) as clock:
        first = reader.read((ROOT,))[ROOT]
        assert first.device.version == "1.0.5"
        assert first.error == "offline"
        assert not first.outdated
        reader.read((ROOT,))
        assert latest.call_count == 1
        clock.return_value = 401
        assert reader.read((ROOT,))[ROOT].outdated


def test_reader_queries_new_model_even_with_cached_releases():
    reader = FirmwareStatusReader()
    dot_root = Path("/Volumes/PulseDot")
    dot = firmware.FirmwareDevice(dot_root, "dot", "1.1.0", "SPD-123")
    with patch.object(firmware, "read_device", side_effect=[DEVICE, DEVICE, dot]), patch.object(
        firmware, "latest_versions", side_effect=[{"pro": "1.1.0"}, {"pro": "1.1.0", "dot": "1.1.14"}],
    ) as latest:
        reader.read((ROOT,))
        assert reader.read((ROOT, dot_root))[dot_root].outdated
        assert latest.call_count == 2


def test_unreadable_device_never_queries_releases():
    with patch.object(firmware, "read_device", side_effect=OSError("disconnected")), patch.object(
        firmware, "latest_versions",
    ) as latest:
        status = FirmwareStatusReader().read((ROOT,))[ROOT]
        assert status.device is None
        assert status.error == "disconnected"
        latest.assert_not_called()


@pytest.mark.parametrize("current", ["1.1.0", "1.2.0", "unknown"])
def test_install_rejects_equal_newer_or_unknown_versions_before_download(current):
    device = firmware.FirmwareDevice(ROOT, "pro", current, "SP-123")
    with patch.object(firmware, "read_device", return_value=device), patch.object(firmware, "load_package") as load:
        with pytest.raises(firmware.FirmwareError):
            install_update(device, "1.1.0")
        load.assert_not_called()


def test_install_rechecks_identity_and_pins_offered_version():
    package = firmware.FirmwarePackage("pro", "1.1.0", b"payload")
    with patch.object(firmware, "read_device", return_value=DEVICE), patch.object(
        firmware, "load_package", return_value=package,
    ) as load, patch.object(firmware, "write_firmware") as write:
        install_update(DEVICE, "1.1.0")
        load.assert_called_once_with("pro", "1.1.0", None)
        write.assert_called_once_with(DEVICE, package)
    changed = firmware.FirmwareDevice(ROOT, "pro", "1.0.5", "OTHER")
    with patch.object(firmware, "read_device", return_value=changed), patch.object(firmware, "load_package") as load:
        with pytest.raises(firmware.FirmwareError, match="changed"):
            install_update(DEVICE, "1.1.0")
        load.assert_not_called()


@pytest.mark.parametrize("product,version", [("dot", "1.1.0"), ("pro", "1.2.0")])
def test_install_refuses_mismatched_package(product, version):
    with patch.object(firmware, "read_device", return_value=DEVICE), patch.object(
        firmware, "load_package", return_value=firmware.FirmwarePackage(product, version, b"payload"),
    ), patch.object(firmware, "write_firmware") as write:
        with pytest.raises(firmware.FirmwareError, match="does not match"):
            install_update(DEVICE, "1.1.0")
        write.assert_not_called()
