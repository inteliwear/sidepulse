"""Storage-free tray discovery, animation delivery and acknowledgement handling."""
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import pytest

from sidepulse import battery, device_writer as writer, led_status
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
