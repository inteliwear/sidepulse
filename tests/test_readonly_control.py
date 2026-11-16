"""Read-only transport routing and SPC2 safety, without hardware I/O."""
import errno
import os
import subprocess
from pathlib import Path
import threading
from unittest.mock import Mock, patch

import pytest

from sidepulse import device_writer as writer
from sidepulse import readonly_control as readonly
from sidepulse import usb_control as usb


@pytest.fixture
def volume(tmp_path):
    (tmp_path / "LEDS.LED").write_text("off")
    (tmp_path / "STATUS.TXT").write_text("release_version 1.1.0\nfirmware_version 1.1.0\nserial SPP-000002\n")
    return tmp_path


@pytest.mark.parametrize("error", [errno.EROFS, errno.EACCES, errno.EPERM])
def test_denied_open_uses_fallback(volume, error):
    with patch.object(Path, "open", side_effect=OSError(error, "denied")), patch.object(
        readonly, "write_readonly_program"
    ) as fallback:
        writer.write_text_synced(volume / "LEDS.LED", "off")
    fallback.assert_called_once_with(volume / "LEDS.LED", "off")


@pytest.mark.parametrize("error", [errno.EIO, errno.ENOENT, errno.ENOSPC])
def test_other_open_errors_do_not_fallback(volume, error):
    with patch.object(Path, "open", side_effect=OSError(error, "failed")), patch.object(
        readonly, "write_readonly_program"
    ) as fallback, pytest.raises(OSError):
        writer.write_text_synced(volume / "LEDS.LED", "off")
    fallback.assert_not_called()


@pytest.mark.parametrize("operation", ["write", "flush", "__exit__"])
def test_never_replays_after_open(volume, operation):
    with patch.object(Path, "open") as opened, patch.object(readonly, "write_readonly_program") as fallback:
        handle = opened.return_value
        handle.__enter__.return_value = handle
        getattr(handle, operation).side_effect = OSError(errno.EROFS, "failed after open")
        handle.fileno.return_value = -1
        with pytest.raises(OSError):
            writer.write_text_synced(volume / "LEDS.LED", "off")
        fallback.assert_not_called()


def test_writable_and_dry_run_do_not_probe(volume):
    with patch.object(readonly, "read_device_info") as probe:
        target = writer.write_led_program("#ff0000", device_path=volume)
        assert target.read_text() == "#ff0000"
        writer.write_led_program("off", device_path=volume, dry_run=True)
        probe.assert_not_called()


@pytest.mark.parametrize("model,field,extra", [
    ("Dot", "app_version", ""), ("Pro", "release_version", "firmware_version 1.1.0\n"),
])
@pytest.mark.parametrize("version,supported", [
    ("1.0.5", False), ("1.0.5-read2me.13", False), ("1.1.0-rc.1", False),
    ("1.1", True), ("1.1.0", True), ("1.10.0", True), ("2.0.0", True),
    ("v1.1.1+build", True), ("unknown", False), ("", False),
])
def test_firmware_gate(volume, model, field, extra, version, supported):
    (volume / "STATUS.TXT").write_text(f"{field} {version}\n{extra}")
    with patch.object(readonly.sys, "platform", "linux"):
        if supported:
            assert readonly.read_device_info(volume) == (model, version)
        else:
            with pytest.raises(writer.DeviceWriteError):
                readonly.read_device_info(volume)


def test_unknown_and_old_firmware_never_open_a_transport(volume):
    with patch.object(readonly.sys, "platform", "linux"), patch(
        "sidepulse._vendor.read2me.Channel"
    ) as channel, patch.object(usb, "Device") as device:
        for status in ("", "app_version 1.0.5\n", "release_version 1.0.5\nfirmware_version 1.0.5\n"):
            (volume / "STATUS.TXT").write_text(status)
            with pytest.raises(writer.DeviceWriteError):
                readonly.write_readonly_program(volume / "LEDS.LED", "off")
        channel.assert_not_called()
        device.assert_not_called()


def test_pro_upload_and_errors(volume):
    with patch.object(readonly, "read_device_info", return_value=("Pro", "1.1.0")), patch(
        "sidepulse._vendor.read2me.Channel"
    ) as factory:
        channel = factory.return_value.__enter__.return_value
        readonly.write_readonly_program(volume / "LEDS.LED", "off")
        factory.assert_called_once_with(volume / "setup.html")
        channel.upload.assert_called_once_with(b"off", "LEDS.LED")
        channel.upload.side_effect = RuntimeError("received payload checksum differs")
        with pytest.raises(writer.DeviceWriteError, match="checksum"):
            readonly.write_readonly_program(volume / "LEDS.LED", "off")


def test_dot_uses_mounted_device_address(volume):
    with patch.object(readonly, "read_device_info", return_value=("Dot", "1.1.0")), patch.object(
        readonly, "usb_address_for_volume", return_value=(2, 9)
    ) as address, patch.object(usb, "Device") as factory:
        readonly.write_readonly_program(volume / "LEDS.LED", "off")
        address.assert_called_once_with(volume)
        factory.assert_called_once_with(bus=2, address=9)
        factory.return_value.__enter__.return_value.write.assert_called_once_with(b"off")


def test_custom_filename_refused(volume):
    with patch.object(readonly, "read_device_info") as probe, pytest.raises(writer.DeviceWriteError):
        readonly.write_readonly_program(volume / "custom.led", "off")
    probe.assert_not_called()


def usb_tree(disk, bus, address, vid=0x1A86):
    return {"IOObjectClass": "IOUSBHostDevice", "idVendor": vid, "idProduct": 0xFE10,
            "locationID": bus << 24, "USB Address": address,
            "IORegistryEntryChildren": [{"IORegistryEntryChildren": [{"BSD Name": disk}]}]}


def test_mac_mount_matching_with_multiple_devices(volume):
    tree = [usb_tree("disk2", 1, 2), usb_tree("disk3", 2, 2), usb_tree("disk4", 3, 2, vid=123)]
    info = {"MountPoint": str(volume), "DeviceIdentifier": "disk3"}
    with patch.object(readonly.sys, "platform", "darwin"), patch.object(
        readonly, "_plist_command", side_effect=[info, tree]
    ) as commands:
        assert readonly.usb_address_for_volume(volume) == (2, 2)
        assert "-l" in commands.call_args.args[0]


@pytest.mark.parametrize("tree", [[], [usb_tree("disk3", 2, 2, vid=123)],
    [usb_tree("disk3", 1, 2), usb_tree("disk3", 2, 2)]])
def test_mac_missing_wrong_or_ambiguous_device_is_refused(volume, tree):
    info = {"MountPoint": str(volume), "DeviceIdentifier": "disk3"}
    with patch.object(readonly.sys, "platform", "darwin"), patch.object(
        readonly, "_plist_command", side_effect=[info, tree]
    ), pytest.raises(writer.DeviceWriteError):
        readonly.usb_address_for_volume(volume)


@pytest.mark.skipif(os.name == "nt", reason="POSIX device major/minor IDs")
def test_linux_mount_maps_through_sysfs(volume):
    sysroot = volume / "sys"
    usbroot = sysroot / "usb-device"
    block = usbroot / "storage" / "block" / "sdc" / "sdc1"
    block.mkdir(parents=True)
    (usbroot / "idVendor").write_text("1a86")
    (usbroot / "idProduct").write_text("fe10")
    (usbroot / "busnum").write_text("3")
    (usbroot / "devnum").write_text("7")
    device = volume.stat().st_dev
    (sysroot / f"{os.major(device)}:{os.minor(device)}").symlink_to(block)
    with patch.object(readonly.sys, "platform", "linux"), patch.object(
        readonly.os.path, "ismount", return_value=True
    ), patch.object(readonly, "Path", side_effect=lambda value: sysroot if value == "/sys/dev/block" else Path(value)):
        assert readonly.usb_address_for_volume(volume) == (3, 7)
        (usbroot / "idVendor").write_text("ffff")
        with pytest.raises(writer.DeviceWriteError):
            readonly.usb_address_for_volume(volume)


def test_pro_unsupported_platform_is_reported(volume):
    with patch.object(readonly.sys, "platform", "linux"), pytest.raises(
        writer.DeviceWriteError, match="macOS only"
    ):
        readonly.write_readonly_program(volume / "LEDS.LED", "off")


def status(ident=0, state="IDLE", length=0, received=0, applied=0):
    return dict(id=ident, state=state, length=length, received=received, applied=applied, polls=1)


def fake_usb(*states, out_error=None):
    device = usb.Device.__new__(usb.Device)
    device.status = Mock(side_effect=states)
    device._transfer = Mock(side_effect=out_error)
    return device


def test_spc2_vector_and_malformed_status():
    data = bytes.fromhex("53 50 43 32 34 12 03 00 03 00 03 00 01 00 02 00")
    assert usb.decode_status(data) == dict(id=0x1234, state="APPLIED", length=3, received=3, applied=1, polls=2)
    for value in (b"", data[:-1], b"SPC1" + data[4:]):
        with pytest.raises(ValueError):
            usb.decode_status(value)
    for offset, value in ((6, 6), (7, 1), (9, 3), (10, 4)):
        bad = bytearray(data)
        bad[offset] = value
        with pytest.raises(ValueError):
            usb.decode_status(bad)


def test_usb_one_transfer_and_counter_wrap():
    device = fake_usb(status(applied=65535), status(42, "BUSY", 512, 512, 65535),
                      status(42, "APPLIED", 512, 512, 0))
    with patch.object(usb.secrets, "randbelow", return_value=41):
        result = device.write(b"x" * 512)
    assert result["state"] == "APPLIED"
    device._transfer.assert_called_once_with(0x40, 0x50, 42, b"x" * 512)


@pytest.mark.parametrize("state", [status(42, "INVALID", 3, 3), status(42, "ABORTED", 3, 2),
    status(42, "APPLIED", 3, 2, 1), status(42, "APPLIED", 4, 4, 1), status(42, "APPLIED", 3, 3, 2)])
def test_usb_requires_complete_matching_acknowledgement(state):
    device = fake_usb(status(), state)
    with patch.object(usb.secrets, "randbelow", return_value=41), pytest.raises(RuntimeError):
        device.write(b"off")
    assert device._transfer.call_count == 1


def test_usb_busy_does_not_submit():
    device = fake_usb(status(state="BUSY"))
    with pytest.raises(RuntimeError, match="pending"):
        device.write("off")
    device._transfer.assert_not_called()


def test_usb_reconciles_ambiguous_out_without_resubmission():
    device = fake_usb(status(), status(42, "APPLIED", 3, 3, 1), out_error=OSError("timeout"))
    with patch.object(usb.secrets, "randbelow", return_value=41):
        assert device.write("off")["state"] == "APPLIED"
    assert device._transfer.call_count == 1


def test_usb_missing_acknowledgement_is_uncertain_not_retried():
    device = fake_usb(status(), status(99, "APPLIED", 3, 3, 1))
    with patch.object(usb.secrets, "randbelow", return_value=41), patch.object(
        usb.time, "monotonic", side_effect=[0, 3]
    ), pytest.raises(usb.UncertainUSBWriteError, match="uncertain"):
        device.write("off")
    assert device._transfer.call_count == 1


def test_usb_disconnect_after_write_is_uncertain():
    device = fake_usb(status(), OSError("disconnected"))
    with pytest.raises(RuntimeError, match="uncertain"):
        device.write("off")
    assert device._transfer.call_count == 1


def test_background_tick_does_not_replay_uncertain_usb_command(volume):
    with patch.object(readonly, "read_device_info", return_value=("Dot", "1.1.0")), patch.object(
        readonly, "usb_address_for_volume", return_value=(2, 9)
    ), patch.object(usb, "Device") as factory:
        device = factory.return_value.__enter__.return_value
        device.write.side_effect = usb.UncertainUSBWriteError("lost acknowledgement")
        with pytest.raises(writer.DeviceWriteError, match="lost acknowledgement"):
            readonly.write_readonly_program(volume / "LEDS.LED", "off")
        with pytest.raises(writer.DeviceWriteError, match="automatic replay stopped"):
            readonly.write_readonly_program(volume / "LEDS.LED", "off")
        assert device.write.call_count == 1
        device.write.side_effect = None
        readonly.write_readonly_program(volume / "LEDS.LED", "#ff0000")
        assert device.write.call_count == 2


def test_writer_serializes_threads(volume):
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def write(target, program):
        calls.append(program)
        if program == "off":
            entered.set()
            assert release.wait(3)

    with patch.object(writer, "write_text_synced", side_effect=write):
        a = threading.Thread(target=writer.write_led_program, args=("off",), kwargs={"device_path": volume})
        b = threading.Thread(target=writer.write_led_program, args=("#ff0000",), kwargs={"device_path": volume})
        a.start()
        assert entered.wait(3)
        b.start()
        try:
            assert calls == ["off"]
        finally:
            release.set()
            a.join(3)
            b.join(3)
        assert calls == ["off", "#ff0000"]
