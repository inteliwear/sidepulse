"""Lid-controlled panel power, without changing brightness or external displays."""
from __future__ import annotations

import ctypes
import plistlib
import subprocess
from collections.abc import Callable
from pathlib import Path

from .providers import default_state_dir


def builtin_framebuffer_id(entries: list[dict]) -> int:
    # External framebuffers are named dispext*, even with no monitor attached.
    matches = [
        entry["IORegistryEntryID"]
        for entry in entries
        if str(entry.get("IONameMatched", "")).split(",")[0] == "disp0"
        and isinstance(entry.get("IORegistryEntryID"), int)
    ]
    if len(matches) != 1:
        raise RuntimeError("Could not uniquely identify the built-in framebuffer")
    return matches[0]


class BuiltinPanelPower:
    """Lazy, process-lifetime connection to the Apple Silicon internal panel.

    IOMobileFramebuffer is a private macOS API. Unsupported hardware fails
    closed: never fall back to a command that sleeps every display.
    """

    def __init__(self) -> None:
        self._framebuffer = None
        self._unavailable_error: RuntimeError | None = None

    def _connect(self) -> None:
        result = subprocess.run(
            ["/usr/sbin/ioreg", "-a", "-r", "-c", "IOMobileFramebuffer", "-d", "1"],
            check=True, capture_output=True, timeout=3,
        )
        registry_id = builtin_framebuffer_id(plistlib.loads(result.stdout))
        iokit = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
        panel = ctypes.CDLL(
            "/System/Library/PrivateFrameworks/IOMobileFramebuffer.framework/IOMobileFramebuffer"
        )
        system = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        iokit.IORegistryEntryIDMatching.argtypes = [ctypes.c_uint64]
        iokit.IORegistryEntryIDMatching.restype = ctypes.c_void_p
        iokit.IOServiceGetMatchingService.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        iokit.IOServiceGetMatchingService.restype = ctypes.c_uint32
        iokit.IOObjectRelease.argtypes = [ctypes.c_uint32]
        panel.IOMobileFramebufferOpen.argtypes = [
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        panel.IOMobileFramebufferOpen.restype = ctypes.c_int32
        panel.IOMobileFramebufferRequestPowerChange.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        panel.IOMobileFramebufferRequestPowerChange.restype = ctypes.c_int32
        service = iokit.IOServiceGetMatchingService(0, iokit.IORegistryEntryIDMatching(registry_id))
        if not service:
            raise RuntimeError("Built-in framebuffer service disappeared")
        framebuffer = ctypes.c_void_p()
        try:
            task = ctypes.c_uint32.in_dll(system, "mach_task_self_").value
            status = panel.IOMobileFramebufferOpen(service, task, 0, ctypes.byref(framebuffer))
            if status or not framebuffer.value:
                raise RuntimeError(f"Opening built-in framebuffer failed: {status}")
        finally:
            iokit.IOObjectRelease(service)
        self._panel = panel
        self._framebuffer = framebuffer

    def __call__(self, on: bool) -> None:
        if self._unavailable_error is not None:
            raise self._unavailable_error
        if self._framebuffer is None:
            try:
                self._connect()
            except Exception as exc:
                # There is no useful retry for a missing private framework or
                # a Mac without the Apple Silicon internal framebuffer. Cache
                # that result so lid polling never blocks the app repeatedly.
                self._unavailable_error = RuntimeError(str(exc))
                raise self._unavailable_error from exc
        status = self._panel.IOMobileFramebufferRequestPowerChange(self._framebuffer, int(on))
        if status:
            raise RuntimeError(f"Built-in panel power request failed: {status}")


class InternalDisplayController:
    def __init__(
        self, set_power: Callable[[bool], None] | None = None,
        *, recovery_path: Path | None = None,
    ) -> None:
        self.set_power = set_power or BuiltinPanelPower()
        self.recovery_path = recovery_path or default_state_dir() / "internal-panel-off"
        self.powered_off = self.recovery_path.exists()
        self._applied = False
        self.last_error: str | None = None

    def update(self, lid_closed: bool | None) -> bool:
        if lid_closed is None:
            return False
        # On launch with an open lid, leave the user's display state alone.
        if lid_closed == self.powered_off and (self._applied or not lid_closed):
            return False
        self._applied = False
        try:
            if lid_closed:
                # Record intent before the native call, so a crash or uncertain
                # API result can still be recovered after the lid opens.
                self.recovery_path.parent.mkdir(parents=True, exist_ok=True)
                self.recovery_path.touch()
                self.powered_off = True
            self.set_power(not lid_closed)
            if not lid_closed:
                self.recovery_path.unlink(missing_ok=True)
        except Exception as exc:
            self.last_error = str(exc)
            return False
        self.powered_off = lid_closed
        self._applied = True
        self.last_error = None
        return True

    def release(self) -> None:
        self.update(False)
