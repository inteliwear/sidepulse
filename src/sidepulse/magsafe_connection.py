"""Read the MagSafe port's IORegistry connection flag without charging guesses."""
from __future__ import annotations

import ctypes as C
import plistlib
import sys
from functools import lru_cache


@lru_cache(maxsize=1)
def _frameworks():
    io = C.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
    cf = C.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    signatures = [
        (io.IOServiceGetMatchingServices, [C.c_uint, C.c_void_p, C.POINTER(C.c_uint)], C.c_int),
        (io.IOIteratorNext, [C.c_uint], C.c_uint),
        (io.IOObjectRelease, [C.c_uint], C.c_int),
        (io.IORegistryEntryCreateCFProperty, [C.c_uint, C.c_void_p, C.c_void_p, C.c_uint], C.c_void_p),
        (cf.CFDataCreate, [C.c_void_p, C.c_char_p, C.c_long], C.c_void_p),
        (cf.CFPropertyListCreateWithData, [C.c_void_p, C.c_void_p, C.c_ulong, C.c_void_p, C.c_void_p], C.c_void_p),
        (cf.CFStringCreateWithCString, [C.c_void_p, C.c_char_p, C.c_uint], C.c_void_p),
        (cf.CFGetTypeID, [C.c_void_p], C.c_ulong),
        (cf.CFBooleanGetTypeID, [], C.c_ulong),
        (cf.CFBooleanGetValue, [C.c_void_p], C.c_ubyte),
        (cf.CFRelease, [C.c_void_p], None),
    ]
    for function, arguments, result in signatures:
        function.argtypes = arguments
        function.restype = result
    return io, cf


def read_magsafe_connection() -> bool | None:
    """PortType 17 is MagSafe; absent/unreadable flags mean unknown, not connected."""
    if sys.platform != "darwin":
        return None
    try:
        io, cf = _frameworks()
        matching_bytes = plistlib.dumps({
            "IOProviderClass": "IOService", "IOPropertyMatch": {"PortType": 17},
        })
        data = cf.CFDataCreate(None, matching_bytes, len(matching_bytes))
        if not data:
            return None
        try:
            matching = cf.CFPropertyListCreateWithData(None, data, 0, None, None)
        finally:
            cf.CFRelease(data)
        if not matching:
            return None
        iterator = C.c_uint()
        # IOKit consumes the matching dictionary on both success and failure.
        if io.IOServiceGetMatchingServices(0, matching, C.byref(iterator)):
            return None
        key = cf.CFStringCreateWithCString(None, b"ConnectionActive", 0x08000100)
        result = None
        try:
            if not key:
                return None
            while entry := io.IOIteratorNext(iterator.value):
                try:
                    value = io.IORegistryEntryCreateCFProperty(entry, key, None, 0)
                    if value:
                        try:
                            if cf.CFGetTypeID(value) == cf.CFBooleanGetTypeID():
                                result = bool(cf.CFBooleanGetValue(value)) or result is True
                        finally:
                            cf.CFRelease(value)
                finally:
                    io.IOObjectRelease(entry)
        finally:
            if key:
                cf.CFRelease(key)
            if iterator.value:
                io.IOObjectRelease(iterator.value)
        return result
    except (OSError, AttributeError):
        return None
