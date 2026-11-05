"""Read-only Mac SMC temperatures, sampled by the daemon's history recorder.

SMC keys are not a public Apple API. TCMz is the CPU die hotspot on Apple
Silicon; TC0D/TCAD cover Intel die/package sensors. Ts0P is palm-rest skin
temperature, used for the case reading. Never substitute battery or ambient
temperature for a missing case sensor.

Protocol/key references:
https://github.com/exelban/stats/blob/master/SMC/smc.swift
https://github.com/acidanthera/VirtualSMC/blob/master/Docs/SMCSensorKeys.txt
"""
from __future__ import annotations

import ctypes as ct
import math
import struct
import sys


SENSOR_KEYS = {
    'cpu_temperature_c': ('TCMz', 'TC0D', 'TCAD'),
    'case_temperature_c': ('Ts0P',),
}


class _KeyInfo(ct.Structure):
    _fields_ = [('size', ct.c_uint32), ('type', ct.c_uint32), ('attributes', ct.c_uint8)]


class _KeyData(ct.Structure):
    # The 80-byte AppleSMC user-client message; version/power-limit fields unused.
    _fields_ = [('key', ct.c_uint32), ('reserved', ct.c_uint8 * 24),
                ('info', _KeyInfo), ('result', ct.c_uint8), ('status', ct.c_uint8),
                ('command', ct.c_uint8), ('value', ct.c_uint32), ('bytes', ct.c_uint8 * 32)]


def decode_temperature(data_type: bytes, data: bytes) -> float | None:
    if data_type == b'flt ' and len(data) == 4:
        value = struct.unpack('<f', data)[0]
    elif data_type == b'sp78' and len(data) == 2:
        value = int.from_bytes(data, 'big', signed=True) / 256
    else:
        return None
    return round(value, 1) if math.isfinite(value) and 0 < value < 130 else None


class _SMC:
    def __init__(self):
        self.io = ct.CDLL('/System/Library/Frameworks/IOKit.framework/IOKit')
        system = ct.CDLL('/usr/lib/libSystem.B.dylib')
        self.io.IOServiceMatching.argtypes = [ct.c_char_p]
        self.io.IOServiceMatching.restype = ct.c_void_p
        self.io.IOServiceGetMatchingService.argtypes = [ct.c_uint32, ct.c_void_p]
        self.io.IOServiceGetMatchingService.restype = ct.c_uint32
        self.io.IOServiceOpen.argtypes = [ct.c_uint32, ct.c_uint32, ct.c_uint32, ct.POINTER(ct.c_uint32)]
        self.io.IOObjectRelease.argtypes = [ct.c_uint32]
        self.io.IOServiceClose.argtypes = [ct.c_uint32]
        self.io.IOConnectCallStructMethod.argtypes = [ct.c_uint32, ct.c_uint32,
            ct.c_void_p, ct.c_size_t, ct.c_void_p, ct.POINTER(ct.c_size_t)]
        self.connection = ct.c_uint32()
        self.info = {}
        service = self.io.IOServiceGetMatchingService(0, self.io.IOServiceMatching(b'AppleSMC'))
        if not service:
            raise OSError('AppleSMC unavailable')
        try:
            task = ct.c_uint32.in_dll(system, 'mach_task_self_').value
            if self.io.IOServiceOpen(service, task, 0, ct.byref(self.connection)):
                raise OSError('AppleSMC access unavailable')
        finally:
            self.io.IOObjectRelease(service)

    def _call(self, request):
        response = _KeyData()
        size = ct.c_size_t(ct.sizeof(response))
        error = self.io.IOConnectCallStructMethod(self.connection, 2,
            ct.byref(request), ct.sizeof(request), ct.byref(response), ct.byref(size))
        return response if not error and not response.result and size.value == ct.sizeof(response) else None

    def read_temperature(self, key):
        request = _KeyData()
        request.key = int.from_bytes(key.encode('ascii'), 'big')
        if key not in self.info:
            request.command = 9  # Read key metadata once, including missing keys.
            response = self._call(request)
            self.info[key] = response.info if response is not None else None
        info = self.info[key]
        if info is None or info.size not in (2, 4):
            return None
        request.info.size = info.size
        request.command = 5  # Read sensor bytes. No write commands are used.
        response = self._call(request)
        if response is None:
            return None
        return decode_temperature(info.type.to_bytes(4, 'big'), bytes(response.bytes[:info.size]))

    def close(self):
        if self.connection.value:
            self.io.IOServiceClose(self.connection)
            self.connection.value = 0


class TemperatureReader:
    def __init__(self):
        self.client = None
        self.keys = None

    def read(self) -> dict[str, float | None]:
        values = dict.fromkeys(SENSOR_KEYS)
        if self.keys is None:
            self.keys = {}
            if sys.platform != 'darwin':
                return values
            try:
                self.client = _SMC()
            except (OSError, AttributeError):
                return values
            for field, candidates in SENSOR_KEYS.items():
                for key in candidates:
                    value = self.client.read_temperature(key)
                    if value is not None:
                        self.keys[field] = key
                        values[field] = value
                        break
            return values
        for field, key in self.keys.items():
            values[field] = self.client.read_temperature(key)
        return values

    def close(self):
        if self.client is not None:
            self.client.close()
        self.client = None
        self.keys = {}
