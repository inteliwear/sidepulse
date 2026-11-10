import json
from pathlib import Path
from unittest.mock import patch

import pytest

from sidepulse import cli, device_list as listing
from sidepulse.device_writer import DeviceCandidate


DOT = DeviceCandidate(Path('/dev/sidepulse-usb/Dot-002-001'), Path('/dev/sidepulse-usb/Dot-002-001'),
                      'USB control', 'sidepulse-usb:SPD-000006', 'SidePulse Dot', '1.1.15', 'SPD-000006', 'USB', 2)


@pytest.mark.parametrize('command', ['list', 'ls'])
def test_aliases_show_shared_usb_selector(command, capsys):
    with patch.object(listing, 'discover_devices', return_value=[DOT]):
        assert cli.sidepulse_main([command]) == 0
    text = capsys.readouterr().out
    assert DOT.model in text and str(DOT.root) in text and DOT.serial in text


def test_list_renders_shared_candidates_without_another_probe(capsys):
    root = Path('/Volumes/SidePulse')
    pro = DeviceCandidate(root, root / 'LEDS.LED', 'mounted', model='SidePulse Pro',
                          version='1.1.0', serial='SPP-000002')
    with patch.object(listing, 'discover_devices', return_value=[pro, DOT]) as discover:
        assert cli.sidepulse_main(['ls', '--json']) == 0
    discover.assert_called_once_with()
    devices = json.loads(capsys.readouterr().out)
    assert len(devices) == 2
    assert devices[0]['device'] == str(root) and devices[0]['connection'] == 'drive'
    assert devices[1]['serial'] == DOT.serial and devices[1]['connection'] == 'USB'


@pytest.mark.parametrize('as_json', [False, True])
def test_no_devices_is_successful_empty_list(as_json, capsys):
    with patch.object(listing, 'discover_devices', return_value=[]):
        assert cli.sidepulse_main(['list'] + (['--json'] if as_json else [])) == 0
    text = capsys.readouterr().out
    assert json.loads(text) == [] if as_json else 'No SidePulse devices found.' in text
