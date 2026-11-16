import ctypes
import struct
from unittest.mock import Mock, patch

import pytest

from sidepulse.temperature import TemperatureReader, _KeyData, _SMC, decode_temperature


@pytest.mark.parametrize('kind,data,expected', [
    (b'flt ', struct.pack('<f', 73.25), 73.2),
    (b'sp78', bytes.fromhex('20c0'), 32.8),
    (b'sp78', bytes.fromhex('ff00'), None),
    (b'flt ', struct.pack('<f', float('nan')), None),
    (b'flt ', struct.pack('<f', float('inf')), None),
    (b'flt ', struct.pack('<f', 0), None),
    (b'flt ', struct.pack('<f', 200), None),
    (b'flt ', b'\x00', None),
    (b'ui32', b'\x00' * 4, None),
])
def test_sensor_encodings_and_invalid_readings(kind, data, expected):
    assert decode_temperature(kind, data) == expected


def test_smc_abi_and_metadata_are_reused_for_sensor_reads():
    assert ctypes.sizeof(_KeyData) == 80
    assert _KeyData.info.offset == 28
    assert _KeyData.bytes.offset == 48
    client = _SMC.__new__(_SMC)
    client.info = {}
    commands = []
    def response(request):
        commands.append(request.command)
        result = _KeyData()
        result.info.size = 4
        result.info.type = int.from_bytes(b'flt ', 'big')
        result.bytes[:4] = struct.pack('<f', 61.5)
        return result
    client._call = response
    assert client.read_temperature('TCMz') == 61.5
    assert client.read_temperature('TCMz') == 61.5
    assert commands == [9, 5, 5]  # Metadata once, sensor bytes on each sample.


def test_fallback_selection_reuses_connection_and_does_not_substitute_case_sensor():
    client = Mock()
    values = {'TCMz': None, 'TC0D': 65.0, 'Ts0P': None}
    client.read_temperature.side_effect = values.get
    with patch('sidepulse.temperature.sys.platform', 'darwin'), patch('sidepulse.temperature._SMC', return_value=client) as open_smc:
        reader = TemperatureReader()
        assert reader.read() == {'cpu_temperature_c': 65.0, 'case_temperature_c': None}
        client.read_temperature.reset_mock()
        values['TC0D'] = 67.0
        assert reader.read() == {'cpu_temperature_c': 67.0, 'case_temperature_c': None}
        client.read_temperature.assert_called_once_with('TC0D')
        open_smc.assert_called_once()
        values['TC0D'] = None
        assert reader.read()['cpu_temperature_c'] is None  # No stale value.
        reader.close()
        client.close.assert_called_once()


def test_unsupported_platform_and_access_failure_stay_quiet():
    for platform in ('linux', 'darwin'):
        with patch('sidepulse.temperature.sys.platform', platform), patch('sidepulse.temperature._SMC', side_effect=OSError('unavailable')) as open_smc:
            reader = TemperatureReader()
            for _ in range(2):
                assert reader.read() == {'cpu_temperature_c': None, 'case_temperature_c': None}
            assert open_smc.call_count == (1 if platform == 'darwin' else 0)
            reader.close()
