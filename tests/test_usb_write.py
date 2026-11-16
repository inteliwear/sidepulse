"""Direct LED writes must never treat a logical USB selector as a disk."""
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import pytest

from sidepulse import cli, device_writer as writer, usb_control as usb


SELECTOR = Path('/dev/sidepulse-usb/Dot-002-001')


@pytest.fixture(autouse=True)
def reset_usb_write_state():
    writer._uncertain_usb_programs.clear()
    writer._usb_acknowledgements.clear()
    yield
    writer._uncertain_usb_programs.clear()
    writer._usb_acknowledgements.clear()


def test_exact_cli_command_uses_selected_usb_and_normalizes_text(capsys):
    with patch.object(usb, 'Device') as opened, \
         patch.object(writer, 'device_write_lock', return_value=nullcontext()) as lock, \
         patch.object(writer, 'write_text_synced') as filesystem, patch.object(Path, 'mkdir') as mkdir:
        assert cli.sidepulse_main(['write', '--device', str(SELECTOR), r'off\n#FF0000']) == 0
        opened.assert_called_once_with(bus=2, address=1)
        opened.return_value.__enter__.return_value.write.assert_called_once_with(b'off\n#FF0000')
        lock.assert_called_once_with(SELECTOR)
        filesystem.assert_not_called()
        mkdir.assert_not_called()
    assert f'wrote: {SELECTOR}' in capsys.readouterr().out


def test_dry_run_does_not_probe_or_write_usb_or_disk(capsys):
    with patch.object(usb, 'Device') as opened, patch.object(writer, 'write_text_synced') as filesystem, \
         patch.object(writer, 'device_write_lock') as lock:
        assert cli.sidepulse_main(['write', '#FF0000', '--device', str(SELECTOR), '--dry-run']) == 0
        opened.assert_not_called()
        filesystem.assert_not_called()
        lock.assert_not_called()
    assert f'would write: {SELECTOR}' in capsys.readouterr().out


@pytest.mark.parametrize('suffix', ['', '/Dot-002-000', '/Dot-256-001', '/Dot-002-128',
                                   '/Dot-002-001/LEDS.LED', '/Dot-002-001/../Other', '/Other'])
def test_malformed_selector_never_reaches_usb_or_filesystem(suffix):
    with patch.object(usb, 'Device') as opened, patch.object(writer, 'write_text_synced') as filesystem, \
         pytest.raises(writer.DeviceWriteError, match='Invalid USB Dot'):
        writer.write_led_program('#ff0000', device_path=usb.USB_DEVICE_ROOT / suffix.lstrip('/'))
    opened.assert_not_called()
    filesystem.assert_not_called()


def test_custom_file_is_rejected_before_io():
    with patch.object(usb, 'Device') as opened, patch.object(writer, 'write_text_synced') as filesystem, \
         pytest.raises(writer.DeviceWriteError, match='live LEDS.LED'):
        writer.write_led_program('#ff0000', device_path=SELECTOR, file_name='INIT.LED')
    opened.assert_not_called()
    filesystem.assert_not_called()


@pytest.mark.parametrize('error', [OSError('device gone'), usb.UncertainUSBWriteError('acknowledgement lost'),
                                  RuntimeError('LED command was not applied')])
def test_failed_or_uncertain_write_is_not_replayed(error, capsys):
    with patch.object(usb, 'Device') as opened, \
         patch.object(writer, 'device_write_lock', return_value=nullcontext()), \
         patch.object(writer, 'write_text_synced') as filesystem:
        device = opened.return_value.__enter__.return_value
        device.write.side_effect = error
        assert cli.sidepulse_main(['write', '#FF0000', '--device', str(SELECTOR)]) != 0
        device.write.assert_called_once()
        filesystem.assert_not_called()
    result = capsys.readouterr()
    assert 'wrote:' not in result.out
    assert str(error) in result.err


def test_validation_precedes_any_usb_access():
    with patch.object(usb, 'Device') as opened:
        for program in ('', 'x' * 513, '\n'.join(['off'] * 21)):
            with pytest.raises(writer.DeviceWriteError):
                writer.write_led_program(program, device_path=SELECTOR)
    opened.assert_not_called()
