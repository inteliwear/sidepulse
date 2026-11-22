"""Version reporting on a storage-disabled Dot, without mounting or writing files."""
import argparse
import json
import struct
from unittest.mock import MagicMock, patch

import pytest

from sidepulse import firmware, storage, usb_control as usb


def response(*, active=False, serial=6, version='1.1.15'):
    data = bytearray(48)
    data[:8] = b'SPI1' + bytes((1, 2, 2, int(active)))
    struct.pack_into('<I', data, 8, serial)
    data[12:12 + len(version)] = version.encode('ascii')
    return bytes(data)


def test_decode_real_identity_and_version():
    info = usb.decode_device_info(response())
    assert info['version'] == '1.1.15'
    assert info['serial'] == 'SPD-000006'
    assert info['board_revision'] == 2 and info['led_count'] == 2
    assert not info['storage_active']
    assert usb.decode_device_info(response(serial=0))['serial'] == 'unassigned'


@pytest.mark.parametrize('offset,value', [(0, 0), (4, 2), (5, 9), (6, 9),
                                           (7, 8), (7, 16), (12, 0xff), (43, 1), (44, 1)])
def test_reject_bad_or_truncated_info(offset, value):
    data = bytearray(response()); data[offset] = value
    with pytest.raises(ValueError):
        usb.decode_device_info(bytes(data))


def test_exact_info_request():
    device = usb.Device.__new__(usb.Device)
    device._transfer = MagicMock(return_value=response())
    assert device.info()['serial'] == 'SPD-000006'
    device._transfer.assert_called_once_with(0xc0, 0x54, 0, bytes(48))


def test_discover_only_hidden_storage_and_preserve_device_selection():
    def factory(*, bus, address):
        obj = MagicMock()
        obj.__enter__.return_value.info.return_value = usb.decode_device_info(
            response(active=address == 1, serial=address))
        return obj
    with patch.object(usb, 'enumerate_addresses', return_value=[(2, 2), (2, 1), (2, 2)]), \
         patch.object(usb, 'Device', side_effect=factory) as opened:
        devices = firmware.find_usb_devices()
    assert devices == [firmware.FirmwareDevice(firmware.USB_DEVICE_ROOT / 'Dot-002-002',
                                              'dot', '1.1.15', 'SPD-000002')]
    assert opened.call_count == 2


def test_old_firmware_and_missing_usb_library_do_not_break_discovery():
    with patch.object(usb, 'enumerate_addresses', return_value=[(1, 2)]), \
         patch.object(usb, 'Device', side_effect=OSError('PIPE')):
        assert firmware.find_usb_devices() == []
    with patch.object(usb, 'enumerate_addresses', side_effect=ImportError('libusb-package')):
        assert firmware.find_usb_devices() == []


def test_explicit_usb_selector_reads_selected_device():
    with patch.object(usb, 'Device') as opened:
        opened.return_value.__enter__.return_value.info.return_value = usb.decode_device_info(response())
        device = firmware.read_device(firmware.USB_DEVICE_ROOT / 'Dot-002-003')
    opened.assert_called_once_with(bus=2, address=3)
    assert device.serial == 'SPD-000006' and device.version == '1.1.15'


@pytest.mark.parametrize('selector', ['bad', 'Dot-256-001', 'Dot-002-128', 'Dot-002-000', 'Dot-002-001/subdir'])
def test_reject_malformed_usb_selector(selector):
    with pytest.raises(firmware.FirmwareError):
        firmware.read_device(firmware.USB_DEVICE_ROOT / selector)


def test_firmware_version_outputs_usb_device(capsys):
    device = firmware.FirmwareDevice(firmware.USB_DEVICE_ROOT / 'Dot-002-001', 'dot', '1.1.15', 'SPD-000006')
    with patch.object(firmware, 'find_devices', return_value=[device]):
        assert firmware.cmd_firmware_version(argparse.Namespace(device=None, json=True)) == 0
    result = json.loads(capsys.readouterr().out)
    assert result[0]['version'] == '1.1.15' and result[0]['serial'] == 'SPD-000006'


def test_usb_dry_run_checks_package_without_changing_storage_or_writing(capsys):
    device = firmware.FirmwareDevice(usb.USB_DEVICE_ROOT / 'Dot-002-001', 'dot', '1.1.15', 'SPD-000006')
    package = firmware.FirmwarePackage('dot', '1.1.17', b'payload')
    args = argparse.Namespace(device=None, all=True, read_only=False, release_version=None, file=None, dry_run=True)
    with patch.object(firmware, 'find_devices', return_value=[device]), \
         patch.object(firmware, 'load_package', return_value=package), patch.object(usb, 'Device') as opened:
        assert firmware.cmd_firmware_upgrade(args) == 0
        opened.assert_not_called()
    assert 'Would upgrade' in capsys.readouterr().out


@pytest.mark.parametrize('dry_run', [True, False])
def test_read_only_upgrade_never_enables_dot_storage(dry_run, capsys):
    device = firmware.FirmwareDevice(usb.USB_DEVICE_ROOT / 'Dot-002-001', 'dot', '1.1.15', 'SPD-000006')
    package = firmware.FirmwarePackage('dot', '1.1.17', b'payload')
    args = argparse.Namespace(device=None, all=True, read_only=True, release_version=None, file=None, dry_run=dry_run)
    with patch.object(firmware, 'find_devices', return_value=[device]), \
         patch.object(firmware, 'load_package', return_value=package), patch.object(usb, 'Device') as opened:
        assert firmware.cmd_firmware_upgrade(args) == 1
        opened.assert_not_called()
    assert 'writable drive' in capsys.readouterr().err


def test_hidden_dot_copies_to_matching_remounted_drive_and_restores_preference(tmp_path, monkeypatch):
    mounted = tmp_path / 'PulseDot 1'
    mounted.mkdir()
    serial = 'SPD-000006'
    (mounted / 'STATUS.TXT').write_text(f'app_version 1.1.15\nserial {serial}\n')
    other = tmp_path / 'PulseDot'
    other.mkdir()
    (other / 'STATUS.TXT').write_text('app_version 1.1.15\nserial SPD-000007\n')
    monkeypatch.setenv('SIDEPULSE_MOUNT_ROOTS', str(tmp_path))
    original = dict(bus=2, address=1, serial=serial)
    reconnected = dict(original, address=9)
    device = firmware.FirmwareDevice(usb.USB_DEVICE_ROOT / 'Dot-002-001', 'dot', '1.1.15', serial)
    hidden, visible = MagicMock(), MagicMock()
    for handle in (hidden, visible):
        handle.__enter__.return_value = handle
        handle.info.return_value = dict(serial=serial, version='1.1.15')
    hidden.storage_status.return_value = dict(active=False, saved=False, state='READY')
    visible.storage_status.return_value = dict(active=True, saved=True, state='READY')
    with patch.object(storage, 'find_dots', side_effect=[[original], [reconnected]]), \
         patch.object(storage, 'wait_for_storage', return_value=reconnected) as wait, \
         patch('sidepulse.readonly_control.usb_address_for_volume', return_value=(2, 9)), \
         patch.object(usb, 'Device', side_effect=[hidden, visible]) as opened:
        firmware.write_firmware(device, firmware.FirmwarePackage('dot', '1.1.17', b'payload'))
    hidden.set_storage.assert_called_once_with(True)
    hidden.reconnect.assert_called_once_with()
    wait.assert_called_once_with(original, True)
    opened.assert_called_with(bus=2, address=9)
    visible.set_storage.assert_called_once_with(False)
    visible.reconnect.assert_not_called()
    assert (mounted / 'FIRMWARE.BIN').read_bytes() == b'payload'
    assert not (other / 'FIRMWARE.BIN').exists()


def test_duplicate_usb_serial_is_rejected_before_storage_change():
    serial = 'SPD-000006'
    device = firmware.FirmwareDevice(usb.USB_DEVICE_ROOT / 'Dot-002-001', 'dot', '1.1.15', serial)
    with patch.object(storage, 'find_dots', return_value=[dict(serial=serial, bus=2, address=a) for a in (1, 2)]), \
         patch.object(usb, 'Device') as opened, pytest.raises(firmware.FirmwareError, match='uniquely identify'):
        firmware.write_firmware(device, firmware.FirmwarePackage('dot', '1.1.17', b'payload'))
    opened.assert_not_called()


def test_enumeration_failure_keeps_shared_context_available(monkeypatch):
    library = MagicMock()
    library.libusb_init.return_value = 0
    library.libusb_get_device_list.return_value = -1
    monkeypatch.setattr(usb, '_contexts', {})
    with patch.object(usb, '_usb_library', return_value=library):
        with pytest.raises(OSError):
            usb.enumerate_addresses()
        library.libusb_get_device_list.return_value = 0
        assert usb.enumerate_addresses() == []
    library.libusb_init.assert_called_once()
    library.libusb_exit.assert_not_called()


def test_concurrent_discovery_and_handles_share_context_without_teardown(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import ctypes as C

    library = MagicMock()
    def initialize(pointer):
        C.cast(pointer, C.POINTER(C.c_void_p))[0] = C.c_void_p(42)
        return 0
    library.libusb_init.side_effect = initialize
    library.libusb_get_device_list.return_value = 0
    monkeypatch.setattr(usb, '_contexts', {})
    def use_usb(_):
        assert usb.enumerate_addresses() == []
        # A missing device must release its own resources, never the shared
        # context another discovery or write is still using.
        with pytest.raises(OSError, match='found 0'):
            usb.Device(bus=2, address=1)
        u, context = usb._usb_context()
        device = usb.Device.__new__(usb.Device)
        device.usb, device.context, device.handle = u, context, C.c_void_p(7)
        device.close(); device.close()
        return context.value
    with patch.object(usb, '_usb_library', return_value=library):
        with ThreadPoolExecutor(max_workers=8) as pool:
            assert list(pool.map(use_usb, range(32))) == [42] * 32
    library.libusb_init.assert_called_once()
    assert library.libusb_close.call_count == 32
    library.libusb_exit.assert_not_called()
