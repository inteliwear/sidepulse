"""End-to-end contracts across public Python, CLI and tray device entry points."""
from contextlib import nullcontext
import json
from pathlib import Path
from unittest.mock import patch

import pytest

import sidepulse
from sidepulse import cli, device_list, device_writer as writer, service
from sidepulse import readonly_control, usb_control as usb, usb_discovery


ROOT = Path('/dev/sidepulse-usb/Dot-002-001')
INFO = dict(root=ROOT, bus=2, address=1, serial='SPD-000006', led_count=2,
            storage_active=False, storage_saved=False, version='1.1.15')


def test_all_frontends_import_the_same_device_and_write_implementations():
    assert sidepulse.discover_devices is cli.discover_devices is device_list.discover_devices is service.discover_devices is writer.discover_devices
    assert sidepulse.write_led_program is service.write_led_program is writer.write_led_program
    assert cli.write_normalized_led_program is writer.write_normalized_led_program
    assert writer.discover_control_devices is writer.discover_devices


def test_python_api_cli_list_and_automatic_write_agree_on_a_hidden_dot(capsys):
    writer._uncertain_usb_programs.clear()
    with patch.object(writer, 'discover_mounted_devices', return_value=[]), \
         patch.object(usb_discovery, 'dots', return_value=(INFO,)), \
         patch.object(cli, 'load_ios_links', return_value=()), patch.object(usb, 'Device') as opened, \
         patch.object(writer, 'device_write_lock', side_effect=lambda root: nullcontext()), \
         patch.object(writer, 'write_text_synced') as disk_write:
        devices = sidepulse.discover_devices()
        assert len(devices) == 1 and devices[0].root == devices[0].target == ROOT
        assert cli.sidepulse_main(['ls', '--json']) == 0
        listed = json.loads(capsys.readouterr().out)
        assert listed[0]['device'] == str(devices[0].root)
        assert cli.sidepulse_main(['write', '#FF0000']) == 0
        sidepulse.write_led_program('#00FF00')
        opened.assert_called_with(bus=2, address=1)
        assert opened.return_value.__enter__.return_value.write.call_count == 2
        disk_write.assert_not_called()
    writer._usb_acknowledgements.clear()


@pytest.mark.parametrize('serial', ['SPD-000006', 'unassigned'])
def test_shared_discovery_deduplicates_mounted_dot_and_keeps_another_usb_dot(serial):
    root = Path('/Volumes/PulseDot')
    candidate = writer.DeviceCandidate(root, root / 'LEDS.LED', 'mounted', model='SidePulse Dot',
                                      serial=serial, version='1.1.15')
    second = dict(INFO, root=Path('/dev/sidepulse-usb/Dot-002-002'), address=2, serial='SPD-000007')
    with patch.object(writer, 'discover_mounted_devices', return_value=[candidate]), \
         patch.object(writer, 'describe_mounted_device', side_effect=lambda device: device), \
         patch.object(readonly_control, 'usb_address_for_volume', return_value=(2, 1)), \
         patch.object(usb_discovery, 'dots', return_value=(dict(INFO, serial=serial, storage_active=True), second)):
        devices = writer.discover_devices()
    assert [device.root for device in devices] == [root, second['root']]


def test_status_metadata_is_identical_for_list_and_python_api(tmp_path, capsys):
    drive = tmp_path / 'RenamedDrive'
    drive.mkdir()
    (drive / 'STATUS.TXT').write_text('app_version 1.2.3\nserial SPD-000012\n')
    with patch.dict('os.environ', {'SIDEPULSE_MOUNT_ROOTS': str(tmp_path)}):
        devices = sidepulse.discover_devices()
        assert cli.sidepulse_main(['list', '--json']) == 0
    listed = json.loads(capsys.readouterr().out)
    assert len(devices) == 1
    assert (listed[0]['model'], listed[0]['version'], listed[0]['serial']) == (
        devices[0].model, devices[0].version, devices[0].serial)
    assert devices[0].model == 'SidePulse Dot'
