"""Read the macOS display brightness the Control Center slider uses.

AppleARMBacklight's 16-bit ``brightness`` key sits at 50% (32768/65536) on
current Macs even when the slider is much lower, so LED mirroring must not
use it. DisplayServices tracks the slider; BrightnessMilliNits is the ioreg
fallback. Returns a 0.0-1.0 ratio; 1.0 if nothing can be read.
"""

from __future__ import annotations

import re
import subprocess


def display_services_ratio() -> float | None:
    """Control Center / keyboard brightness, 0.0-1.0. None if unavailable."""
    try:
        import ctypes
        from ctypes import POINTER, c_float, c_int, c_uint32

        cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        ds = ctypes.CDLL(
            "/System/Library/PrivateFrameworks/DisplayServices.framework/DisplayServices"
        )
        cg.CGMainDisplayID.restype = c_uint32
        ds.DisplayServicesGetBrightness.argtypes = [c_uint32, POINTER(c_float)]
        ds.DisplayServicesGetBrightness.restype = c_int
        val = c_float()
        rc = ds.DisplayServicesGetBrightness(cg.CGMainDisplayID(), ctypes.byref(val))
        if rc == 0 and 0.0 <= val.value <= 1.0:
            return float(val.value)
    except Exception:
        return None
    return None


def ioreg_nits_ratio(out: str) -> float | None:
    """Parse AppleARMBacklight BrightnessMilliNits value/max from ioreg text."""
    m = re.search(r'"BrightnessMilliNits"\s*=\s*\{([^}]*)\}', out)
    if not m:
        return None
    body = m.group(1)
    vm = re.search(r'"value"=(\d+)', body)
    xm = re.search(r'"max"=(\d+)', body)
    if not vm or not xm:
        return None
    maximum = int(xm.group(1))
    if maximum <= 0:
        return None
    return max(0.0, min(1.0, int(vm.group(1)) / maximum))


def system_brightness_ratio() -> float:
    """Current display brightness as a 0.0-1.0 ratio (falls back to 1.0)."""
    ratio = display_services_ratio()
    if ratio is not None:
        return ratio
    try:
        out = subprocess.run(
            ["/usr/sbin/ioreg", "-rc", "AppleARMBacklight"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        ).stdout
        nits = ioreg_nits_ratio(out)
        if nits is not None:
            return nits
    except Exception:
        pass
    return 1.0
