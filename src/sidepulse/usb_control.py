#!/usr/bin/env python3
"""Dot SPC2 LED control. Never claims, detaches, or resets USB interfaces.

Adapted from sdstatus_bitbang/utils/usb-control/sidepulse_usb_control.py.
"""
import ctypes as C
import ctypes.util
import secrets
import struct
import time

INDEX = 0x5350
STATUS_BYTES = 16
MAX_BYTES = 512


class UncertainUSBWriteError(RuntimeError):
    """The command may have applied; background writers must not replay it."""


def decode_status(data):
    if len(data) != STATUS_BYTES or data[:4] != b'SPC2':
        raise ValueError('not a SidePulse USB-control v2 response')
    ident, state, reserved, length, received, applied, polls = struct.unpack_from('<HBBHHHH', data, 4)
    if state > 5 or reserved != 0 or length > MAX_BYTES or received > length:
        raise ValueError('invalid USB-control status')
    return dict(id=ident, state=('IDLE', 'RECEIVING', 'BUSY', 'APPLIED', 'INVALID', 'ABORTED')[state],
                length=length, received=received, applied=applied, polls=polls)


DOT_VID_PID = (0x1a86, 0xfe10)
MAX_PORT_DEPTH = 7


def load_libusb(library=None):
    from libusb_package import find_library

    path = library or find_library('libusb-1.0') or ctypes.util.find_library('usb-1.0')
    if not path:
        raise OSError('libusb is unavailable; reinstall SidePulse with its libusb-package dependency')
    u = C.CDLL(path)
    u.libusb_init.argtypes = [C.POINTER(C.c_void_p)]
    u.libusb_exit.argtypes = [C.c_void_p]
    u.libusb_get_device_list.argtypes = [C.c_void_p, C.POINTER(C.POINTER(C.c_void_p))]
    u.libusb_get_device_list.restype = C.c_ssize_t
    u.libusb_free_device_list.argtypes = [C.POINTER(C.c_void_p), C.c_int]
    u.libusb_get_device_descriptor.argtypes = [C.c_void_p, C.c_void_p]
    u.libusb_get_bus_number.argtypes = [C.c_void_p]
    u.libusb_get_bus_number.restype = C.c_uint8
    u.libusb_get_device_address.argtypes = [C.c_void_p]
    u.libusb_get_device_address.restype = C.c_uint8
    u.libusb_get_port_numbers.argtypes = [C.c_void_p, C.POINTER(C.c_uint8), C.c_int]
    u.libusb_get_port_numbers.restype = C.c_int
    return u


def list_dots(library=None):
    """Every attached Dot as (bus, port path, address), mounted or not.

    The port path names the physical socket, so it stays the same when the
    Dot is unplugged and plugged back into the same port; the address does not.
    """
    u = load_libusb(library)
    context = C.c_void_p()
    if u.libusb_init(C.byref(context)) < 0:
        raise OSError('USB: libusb_init failed')
    devices = C.POINTER(C.c_void_p)()
    try:
        count = u.libusb_get_device_list(context, C.byref(devices))
        if count < 0:
            raise OSError(f'USB: libusb_get_device_list failed ({count})')
        found = []
        for i in range(count):
            descriptor = (C.c_ubyte * 18)()
            if u.libusb_get_device_descriptor(devices[i], descriptor) < 0:
                continue
            # libusb's descriptor contains host-endian integer fields.
            vid = C.c_uint16.from_buffer(descriptor, 8).value
            pid = C.c_uint16.from_buffer(descriptor, 10).value
            if (vid, pid) != DOT_VID_PID:
                continue
            ports = (C.c_uint8 * MAX_PORT_DEPTH)()
            depth = u.libusb_get_port_numbers(devices[i], ports, MAX_PORT_DEPTH)
            found.append((
                u.libusb_get_bus_number(devices[i]),
                tuple(ports[:depth]) if depth > 0 else (),
                u.libusb_get_device_address(devices[i]),
            ))
        return sorted(found)
    finally:
        if devices:
            u.libusb_free_device_list(devices, 1)
        u.libusb_exit(context)


class Device:
    def __init__(self, *, bus: int, address: int, library=None):
        u = self.usb = load_libusb(library)
        u.libusb_open.argtypes = [C.c_void_p, C.POINTER(C.c_void_p)]
        u.libusb_close.argtypes = [C.c_void_p]
        u.libusb_control_transfer.argtypes = [C.c_void_p, C.c_uint8, C.c_uint8,
                                            C.c_uint16, C.c_uint16, C.c_void_p,
                                            C.c_uint16, C.c_uint]
        u.libusb_error_name.argtypes = [C.c_int]
        u.libusb_error_name.restype = C.c_char_p
        self.context = C.c_void_p()
        self.handle = C.c_void_p()
        self._check(u.libusb_init(C.byref(self.context)))
        devices = C.POINTER(C.c_void_p)()
        try:
            count = self._check(u.libusb_get_device_list(self.context, C.byref(devices)))
            matches = []
            for i in range(count):
                descriptor = (C.c_ubyte * 18)()
                self._check(u.libusb_get_device_descriptor(devices[i], descriptor))
                # libusb's descriptor contains host-endian integer fields.
                vid = C.c_uint16.from_buffer(descriptor, 8).value
                pid = C.c_uint16.from_buffer(descriptor, 10).value
                if ((vid, pid) == (0x1a86, 0xfe10)
                        and u.libusb_get_bus_number(devices[i]) == bus
                        and u.libusb_get_device_address(devices[i]) == address):
                    matches.append(devices[i])
            if len(matches) != 1:
                raise OSError(f'expected the selected SidePulse Dot at USB {bus}:{address}; found {len(matches)}')
            self._check(u.libusb_open(matches[0], C.byref(self.handle)))
        except BaseException:
            if devices:
                u.libusb_free_device_list(devices, 1)
                devices = None
            self.close()
            raise
        finally:
            if devices:
                u.libusb_free_device_list(devices, 1)

    def _check(self, result):
        if result < 0:
            raise OSError(f'USB: {self.usb.libusb_error_name(result).decode()} ({result})')
        return result

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if self.handle:
            self.usb.libusb_close(self.handle)
            self.handle = C.c_void_p()
        if self.context:
            self.usb.libusb_exit(self.context)
            self.context = C.c_void_p()

    def _transfer(self, request_type, request, value, data):
        if not self.handle:
            raise ValueError('USB device is closed')
        buffer = (C.c_ubyte * len(data)).from_buffer_copy(data)
        count = self._check(self.usb.libusb_control_transfer(
            self.handle, request_type, request, value, INDEX, buffer, len(data), 1000))
        if count != len(data):
            raise OSError(f'short USB transfer: {count}/{len(data)}')
        return bytes(buffer)

    def status(self):
        return decode_status(self._transfer(0xc0, 0x51, 0, bytes(STATUS_BYTES)))

    def write(self, text):
        data = text.encode('ascii') if isinstance(text, str) else bytes(text)
        if not 1 <= len(data) <= MAX_BYTES:
            raise ValueError('USB control accepts 1..512 bytes of LED text')
        before = self.status()
        if before['state'] == 'BUSY':
            raise RuntimeError('another LED command is pending')
        ident = secrets.randbelow(65535) + 1
        while ident == before['id']:
            ident = secrets.randbelow(65535) + 1
        # An OUT error can occur after application. Reconcile the same command,
        # but never resubmit it: IDs are acknowledgement tags, not deduplication.
        out_error = None
        try:
            self._transfer(0x40, 0x50, ident, data)
        except OSError as exc:
            out_error = exc
        deadline = time.monotonic() + 2
        while True:
            try:
                status = self.status()
            except (OSError, ValueError) as exc:
                raise UncertainUSBWriteError(
                    'LED command result is uncertain; USB acknowledgement was lost'
                ) from exc
            if status['id'] == ident and status['state'] not in ('BUSY', 'RECEIVING'):
                if (status['state'] != 'APPLIED' or status['length'] != len(data)
                        or status['received'] != len(data)
                        or status['applied'] != (before['applied'] + 1) % 65536):
                    if status['state'] in ('INVALID', 'ABORTED'):
                        raise RuntimeError(f'LED command was not applied: {status}')
                    raise UncertainUSBWriteError(f'LED command result is uncertain: {status}')
                return status
            if time.monotonic() >= deadline:
                raise UncertainUSBWriteError(f'LED command result is uncertain; no matching acknowledgement: {status}') from out_error
            time.sleep(0.005)
