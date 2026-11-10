"""A Dot whose drive never mounts is still found and driven over USB control."""
import os
from unittest.mock import MagicMock, patch

import pytest

from sidepulse import device_writer as writer
from sidepulse import usb_control
from sidepulse.led_status import led_count_for_target


@pytest.fixture
def no_drives(tmp_path, monkeypatch):
    """Discovery sees an empty mount folder and the real default roots are untouched."""
    monkeypatch.delenv("SIDEPULSE_MOUNT_ROOTS", raising=False)
    monkeypatch.delenv("SIDEPULSE_USB_CONTROL", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    mounts = tmp_path / "Volumes"
    mounts.mkdir()
    monkeypatch.setattr(writer, "default_mount_roots", lambda **_: (mounts,))
    return mounts


def attached(*dots):
    return patch.object(usb_control, "list_dots", return_value=list(dots))


def test_an_unmounted_dot_on_usb_is_found_as_a_two_led_dot(no_drives):
    with attached((1, (1,), 5)):
        candidates = writer.discover_devices()

    assert len(candidates) == 1
    assert candidates[0].target.name == "LEDS.LED"
    assert led_count_for_target(candidates[0].target) == 2
    assert not candidates[0].root.exists()


def test_a_mounted_drive_is_used_and_usb_is_not_offered(no_drives):
    (no_drives / "SidePulseDot").mkdir()
    with attached((1, (1,), 5)):
        candidates = writer.discover_devices()

    assert [candidate.root for candidate in candidates] == [no_drives / "SidePulseDot"]


def test_firmware_lookups_never_get_a_usb_dot(no_drives):
    with attached((1, (1,), 5)):
        assert writer.discover_devices(file_name="STATUS.TXT") == []


def test_usb_discovery_can_be_turned_off(no_drives, monkeypatch):
    monkeypatch.setenv("SIDEPULSE_USB_CONTROL", "0")
    with attached((1, (1,), 5)):
        assert writer.discover_devices() == []


def test_no_libusb_means_no_usb_dot_and_no_error(no_drives):
    with patch.object(usb_control, "list_dots", side_effect=OSError("libusb is unavailable")):
        assert writer.discover_devices() == []


def test_a_program_reaches_the_dot_after_a_replug_into_the_same_port(no_drives):
    with attached((1, (2, 3), 5)):
        [candidate] = writer.discover_devices()

    device = MagicMock()
    device.__enter__.return_value = device
    # The replug gave the Dot a new address on the same port.
    with attached((1, (2, 3), 9)), patch.object(usb_control, "Device", return_value=device) as opened:
        written = writer.write_led_program("#30d158 800ms pulse\\nrepeat", device_path=candidate.target)

    opened.assert_called_once_with(bus=1, address=9)
    device.write.assert_called_once_with(b"#30d158 800ms pulse\nrepeat")
    assert written == candidate.target
    assert not candidate.root.exists()


def test_a_dot_unplugged_since_discovery_fails_the_write(no_drives):
    with attached((1, (1,), 5)):
        [candidate] = writer.discover_devices()

    with attached(), pytest.raises(writer.DeviceWriteError, match="no longer connected"):
        writer.write_led_program("off", device_path=candidate.target)


def test_a_rejected_usb_command_is_a_device_write_error(no_drives):
    with attached((1, (1,), 5)):
        [candidate] = writer.discover_devices()

    device = MagicMock()
    device.__enter__.return_value = device
    device.write.side_effect = RuntimeError("LED command was not applied")
    with attached((1, (1,), 5)), patch.object(usb_control, "Device", return_value=device):
        with pytest.raises(writer.DeviceWriteError, match="not applied"):
            writer.write_led_program("off", device_path=candidate.target)


def test_a_usb_dot_keeps_its_name_across_addresses():
    root = writer.usb_root(1, (2, 3))
    assert writer.usb_location(root) == (1, (2, 3))
    assert writer.usb_location(writer.MOUNT_ROOT / "SidePulseDot") is None
