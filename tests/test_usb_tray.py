"""Storage-free tray discovery, animation delivery and acknowledgement handling."""
from contextlib import nullcontext
from pathlib import Path
import struct
from unittest.mock import Mock, patch

import pytest

from sidepulse import battery, device_writer as writer, firmware, led_status, storage
from sidepulse import usb_control as usb, usb_discovery as discovery
from sidepulse.models import AgentMode

ROOT = Path('/dev/sidepulse-usb/Dot-002-001')
INFO = dict(root=ROOT, bus=2, address=1, serial='SPD-000006', led_count=2,
            storage_active=False, storage_saved=False, version='1.1.15')


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    monkeypatch.delenv('SIDEPULSE_MOUNT_ROOTS', raising=False)
    writer._uncertain_usb_programs.clear()
    writer._usb_acknowledgements.clear()
    discovery._devices = None
    yield
    writer._uncertain_usb_programs.clear()
    writer._usb_acknowledgements.clear()
    discovery._devices = None


def test_discovery_merges_hidden_dot_and_preserves_stable_identity():
    drive = Path('/Volumes/SidePulse')
    mounted = writer.DeviceCandidate(drive, drive / 'LEDS.LED', 'mounted')
    reattached = dict(INFO, root=Path('/dev/sidepulse-usb/Dot-002-009'), address=9)
    with patch.object(writer, 'discover_mounted_devices', side_effect=lambda **kwargs: [mounted]), \
         patch.object(discovery, 'dots', side_effect=[(INFO,), (reattached,)]):
        first = writer.discover_control_devices()
        second = writer.discover_control_devices()
    assert first[0].root == mounted.root
    assert first[1].root == first[1].target == ROOT
    assert first[1].device_id == second[1].device_id == 'sidepulse:SPD-000006'
    assert second[1].target == reattached['root']


def test_unmounted_storage_on_dot_is_available_over_usb():
    with patch.object(writer, 'discover_mounted_devices', return_value=[]), \
         patch.object(discovery, 'dots', return_value=(dict(INFO, storage_active=True),)):
        assert writer.discover_devices()[0].root == ROOT


def test_legacy_dot_is_discovered_and_accepts_an_automatic_write(monkeypatch):
    # Emulate 1.1.14 at the libusb boundary so the actual status decoder and
    # acknowledged LED writer run against the same device as discovery.
    library = Mock()
    library.libusb_error_name.return_value = b'LIBUSB_ERROR_PIPE'
    programs = []

    def transfer(handle, direction, request, value, index, buffer, length, timeout):
        assert index == usb.INDEX
        if (direction, request) == (0xc0, 0x54):
            return -9
        if (direction, request) == (0xc0, 0x51):
            ident, program = programs[-1] if programs else (0, b'')
            buffer[:] = struct.pack('<4sHBBHHHH', b'SPC2', ident, 3 if programs else 0,
                                    0, len(program), len(program), len(programs), 0)
        elif (direction, request) == (0x40, 0x50):
            programs.append((value, bytes(buffer)))
        else:
            pytest.fail(f'Unexpected USB request {request:#x}')
        return length

    def open_device(device, *, bus, address):
        assert (bus, address) == (2, 1)
        device.usb, device.handle = library, 1

    library.libusb_control_transfer.side_effect = transfer
    monkeypatch.setattr(usb.Device, '__init__', open_device)
    monkeypatch.setattr(usb, 'enumerate_addresses', lambda: [(2, 1)])
    monkeypatch.setattr(writer, 'discover_mounted_devices', lambda **kwargs: [])
    monkeypatch.setattr(writer, 'device_write_lock', lambda root: nullcontext())

    [dot] = writer.discover_devices()
    assert (dot.root, dot.connection, dot.version, dot.serial) == (ROOT, 'USB', 'unknown', 'unassigned')
    assert led_status.led_count_for_target(dot.target) == 2
    assert storage.find_dots() == firmware.find_usb_devices() == []
    assert programs == []
    assert writer.write_led_program('off') == ROOT
    assert [program for _, program in programs] == [b'off']


def test_legacy_probe_requires_valid_led_status():
    with patch.object(usb, 'enumerate_addresses', return_value=[(2, 1)]), \
         patch.object(usb, 'Device') as opened:
        device = opened.return_value.__enter__.return_value
        device.info.side_effect = BrokenPipeError()
        device.status.side_effect = ValueError('not a SidePulse USB-control v2 response')
        assert discovery.dots() == ()
        device.status.assert_called_once_with()


def test_restricted_mount_roots_do_not_discover_usb(monkeypatch):
    monkeypatch.setenv('SIDEPULSE_MOUNT_ROOTS', '/test/volumes')
    with patch.object(writer, 'discover_mounted_devices', return_value=[]), patch.object(discovery, 'dots') as dots:
        assert writer.discover_control_devices() == []
        dots.assert_not_called()


def test_metadata_cache_avoids_reprobing_for_every_menu_item():
    with patch.object(usb, 'enumerate_addresses', return_value=[(2, 1)]) as enumerate_usb, \
         patch.object(usb, 'Device') as opened:
        opened.return_value.__enter__.return_value.info.return_value = {k: v for k, v in INFO.items() if k != 'root'}
        assert discovery.dots() == discovery.dots()
        assert discovery.info_for_selector(ROOT)['led_count'] == 2
    enumerate_usb.assert_called_once()
    opened.assert_called_once_with(bus=2, address=1)


def test_usb_library_unavailable_is_empty_discovery():
    with patch.object(usb, 'enumerate_addresses', side_effect=ImportError('no library')):
        assert discovery.dots() == ()


def test_usb_target_resolution_and_real_led_count():
    assert writer.resolve_target_path(device_path=ROOT) == ROOT
    with patch.object(discovery, 'dots', return_value=(dict(INFO, led_count=4),)):
        assert writer.control_target_exists(ROOT)
        assert led_status.led_count_for_target(ROOT) == 4
    with patch.object(discovery, 'dots', return_value=()):
        assert not writer.control_target_exists(ROOT)


def test_agent_animation_uses_ack_and_does_not_restart_unchanged_program():
    controller = led_status.AgentLedController(device_path=ROOT)
    with patch.object(discovery, 'dots', return_value=(INFO,)), patch.object(usb, 'Device') as opened, \
         patch.object(writer, 'device_write_lock', return_value=nullcontext()), \
         patch.object(writer, 'write_text_synced') as file_read:
        device = opened.return_value.__enter__.return_value
        device.write.return_value = dict(id=9, applied=1)
        device.status.return_value = dict(id=9, applied=1, state='APPLIED')
        first = controller.sync_mode(AgentMode.WORKING)
        second = controller.sync_mode(AgentMode.WORKING)
        assert first.changed and not first.error and first.target == ROOT
        assert not second.changed and not second.error
        device.write.assert_called_once()
        file_read.assert_not_called()
        # Another USB client changed the program: the tray can restore its mode.
        device.status.return_value = dict(id=10, applied=2, state='APPLIED')
        assert controller.sync_mode(AgentMode.WORKING).changed


def test_battery_animation_uses_usb_without_a_leds_file():
    with patch.object(discovery, 'dots', return_value=(INFO,)), patch.object(usb, 'Device') as opened, \
         patch.object(writer, 'device_write_lock', return_value=nullcontext()):
        result = battery.write_battery_to_leds(battery.BatterySnapshot(percent=50), device_path=ROOT)
        assert result.target == ROOT and result.changed
        opened.return_value.__enter__.return_value.write.assert_called_once()


def test_uncertain_usb_ack_is_not_automatically_replayed():
    with patch.object(usb, 'Device') as opened, patch.object(writer, 'device_write_lock', side_effect=lambda root: nullcontext()):
        device = opened.return_value.__enter__.return_value
        device.write.side_effect = usb.UncertainUSBWriteError('lost acknowledgement')
        with pytest.raises(writer.DeviceWriteError, match='lost acknowledgement'):
            writer.write_led_program('#ff0000', device_path=ROOT)
        with pytest.raises(writer.DeviceWriteError, match='automatic replay stopped'):
            writer.write_led_program('#ff0000', device_path=ROOT)
        device.write.assert_called_once()
