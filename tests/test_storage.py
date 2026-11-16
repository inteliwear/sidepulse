import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sidepulse import storage, usb_control as usb
from sidepulse.cli import build_sidepulse_parser


DOT = dict(serial='SPD-000006', bus=2, address=1, device='/dev/sidepulse-usb/Dot-002-001')


def run(action, device=None, json=False):
    args = build_sidepulse_parser().parse_args(['storage', action] +
        (['--device', device] if device else []) + (['--json'] if json else []))
    return args.func(args)


def test_status_reads_without_eject_or_writes(capsys):
    with patch.object(storage, 'find_dots', return_value=[DOT]), patch.object(usb, 'Device') as opened, \
         patch.object(storage, 'eject_storage') as eject:
        dev = opened.return_value.__enter__.return_value
        dev.storage_status.return_value = dict(saved=False, active=False, state='READY')
        assert run('status', json=True) == 0
        dev.set_storage.assert_not_called()
        dev.reconnect.assert_not_called()
        eject.assert_not_called()
    assert json.loads(capsys.readouterr().out)[0]['active'] is False


@pytest.mark.parametrize('enabled', [True, False])
def test_change_ejects_before_save_and_reconnect(enabled):
    events = []
    state = dict(saved=not enabled, active=not enabled, state='READY')
    final = dict(saved=enabled, active=enabled, state='READY')
    with patch.object(storage, 'find_dots', return_value=[DOT]), patch.object(usb, 'Device') as opened, \
         patch.object(storage, 'eject_storage', side_effect=lambda *a: events.append('eject')), \
         patch.object(storage, 'wait_for_storage', return_value=dict(DOT, **final)) as wait:
        dev = opened.return_value.__enter__.return_value
        dev.storage_status.return_value = state
        dev.set_storage.side_effect = lambda value: events.append(('save', value)) or dict(state, saved=value)
        dev.reconnect.side_effect = lambda: events.append('reconnect')
        assert run('on' if enabled else 'off') == 0
        assert events == ([] if enabled else ['eject']) + [('save', enabled), 'reconnect']
        wait.assert_called_once_with(DOT, enabled)


def test_eject_failure_stops_before_any_write(capsys):
    with patch.object(storage, 'find_dots', return_value=[DOT]), patch.object(usb, 'Device') as opened, \
         patch.object(storage, 'eject_storage', side_effect=OSError('disk busy')):
        dev = opened.return_value.__enter__.return_value
        dev.storage_status.return_value = dict(saved=True, active=True, state='READY')
        assert run('off') == 1
        dev.set_storage.assert_not_called()
        dev.reconnect.assert_not_called()
    assert 'disk busy' in capsys.readouterr().err


def test_multiple_dots_require_selection_before_mutation():
    other = dict(DOT, serial='SPD-000007', address=2, device='/dev/sidepulse-usb/Dot-002-002')
    with patch.object(storage, 'find_dots', return_value=[DOT, other]), patch.object(usb, 'Device') as opened:
        assert run('off') == 1
        opened.assert_not_called()
        opened.return_value.__enter__.return_value.storage_status.return_value = dict(saved=False, active=False, state='READY')
        assert run('off', device='SPD-000007') == 0
        opened.assert_called_once_with(bus=2, address=2)


def test_saved_change_reconnects_even_when_no_save_needed():
    final = dict(saved=False, active=False, state='READY')
    with patch.object(storage, 'find_dots', return_value=[DOT]), patch.object(usb, 'Device') as opened, \
         patch.object(storage, 'eject_storage'), \
         patch.object(storage, 'wait_for_storage', return_value=dict(DOT, **final)):
        dev = opened.return_value.__enter__.return_value
        dev.storage_status.return_value = dict(final, active=True)
        assert run('off') == 0
        dev.set_storage.assert_not_called()
        dev.reconnect.assert_called_once()


def test_usb_out_failure_is_never_replayed_or_reconnected():
    with patch.object(storage, 'find_dots', return_value=[DOT]), patch.object(usb, 'Device') as opened:
        dev = opened.return_value.__enter__.return_value
        dev.storage_status.return_value = dict(saved=False, active=False, state='READY')
        dev.set_storage.side_effect = OSError('lost acknowledgement')
        assert run('on') == 1
        dev.set_storage.assert_called_once_with(True)
        dev.reconnect.assert_not_called()


def test_reconnect_follows_same_serial_at_new_usb_address():
    other = dict(DOT, serial='SPD-000007')
    reconnected = dict(DOT, address=5, device='/dev/sidepulse-usb/Dot-002-005')
    with patch.object(storage, 'find_dots', side_effect=[[other], [other, reconnected]]), \
         patch.object(storage.time, 'sleep'), patch.object(usb, 'Device') as opened:
        opened.return_value.__enter__.return_value.storage_status.return_value = dict(saved=False, active=False, state='READY')
        result = storage.wait_for_storage(DOT, False)
        assert result['device'] == reconnected['device']
        opened.assert_called_once_with(bus=2, address=5)


def test_macos_eject_matches_exact_usb_device():
    def node(address, disk):
        return {'IOObjectClass': 'IOUSBHostDevice', 'idVendor': 0x1a86, 'idProduct': 0xfe10,
                'locationID': 2 << 24, 'USB Address': address,
                'IORegistryEntryChildren': [{'BSD Name': disk}]}
    with patch.object(storage.sys, 'platform', 'darwin'), \
         patch.object(storage, '_plist_command', return_value=[node(1, 'disk6'), node(2, 'disk7')]), \
         patch.object(storage.subprocess, 'run') as command:
        storage.eject_storage(2, 1)
        command.assert_called_once_with(['/usr/sbin/diskutil', 'eject', '/dev/disk6'],
                                        check=True, capture_output=True, timeout=15)


def test_linux_unmounts_only_selected_usb_media(tmp_path):
    usb_root = tmp_path / 'usb'
    block = usb_root / 'selected' / 'block' / 'sda'
    part = block / 'sda1'
    part.mkdir(parents=True)
    (part / 'dev').write_text('8:1\n')
    (block / 'dev').write_text('8:0\n')
    (block.parent.parent / 'busnum').write_text('2\n')
    (block.parent.parent / 'devnum').write_text('1\n')
    mountinfo = tmp_path / 'mountinfo'
    mountinfo.write_text('30 1 8:1 / /media/Dot rw - vfat /dev/sda1 rw\n'
                         '31 1 8:16 / /media/Other rw - vfat /dev/sdb rw\n')
    with patch.object(storage.sys, 'platform', 'linux'), \
         patch.object(storage, 'Path', side_effect=lambda name: usb_root if name == '/sys/bus/usb/devices' else mountinfo), \
         patch.object(storage.subprocess, 'run') as command:
        storage.eject_storage(2, 1)
    command.assert_called_once_with(['udisksctl', 'unmount', '-b', '/dev/sda1'],
                                    check=True, capture_output=True, timeout=15)


def test_selected_mount_resolves_to_exact_usb_address():
    other = dict(DOT, address=2)
    with patch.object(storage, 'usb_address_for_volume', return_value=(2, 2)) as resolve:
        assert storage.select_dots([DOT, other], '/Volumes/AnotherDot') == [other]
        resolve.assert_called_once_with(Path('/Volumes/AnotherDot'))


@pytest.mark.parametrize('data', [b'', b'SPS1\x02\0\0\0', b'SPS1\0\0\x03\0', b'SPS1\0\0\0\x01'])
def test_reject_malformed_storage_status(data):
    with pytest.raises(ValueError):
        usb.decode_storage_status(data)


def test_storage_protocol_polls_until_flash_save_acknowledged():
    dev = usb.Device.__new__(usb.Device)
    dev._transfer = MagicMock(side_effect=[b'SPS1\0\0\0\0', b'', b'SPS1\0\0\x01\0', b'SPS1\x01\0\0\0', b''])
    with patch.object(usb.time, 'sleep'):
        assert dev.set_storage(True) == dict(saved=True, active=False, state='READY')
        dev.reconnect()
    calls = dev._transfer.call_args_list
    assert [(call.args[0:3]) for call in calls] == [(0xc0, 0x53, 0), (0x40, 0x52, 1),
                                                   (0xc0, 0x53, 0), (0xc0, 0x53, 0), (0x40, 0x55, 0)]
