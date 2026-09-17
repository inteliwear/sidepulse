from __future__ import annotations

import ctypes
import sys
from collections.abc import Callable


IOKIT_FRAMEWORK = "/System/Library/Frameworks/IOKit.framework/IOKit"
CORE_FOUNDATION_FRAMEWORK = (
    "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
)
PowerSourceCallback = ctypes.CFUNCTYPE(None, ctypes.c_void_p)


class PowerSourceNotifier:
    """Deliver macOS power-source changes on the application's main run loop."""

    def __init__(self, callback: Callable[[], None]) -> None:
        self.callback = callback
        self.last_error: str | None = None
        self._iokit = None
        self._core_foundation = None
        self._callback_ref = None
        self._source = None
        self._run_loop = None
        self._modes = None

    def start(self) -> bool:
        if self._source is not None:
            return True
        if sys.platform != "darwin":
            self.last_error = "power-source notifications require macOS"
            return False

        try:
            from AppKit import NSEventTrackingRunLoopMode

            iokit = ctypes.CDLL(IOKIT_FRAMEWORK)
            core_foundation = ctypes.CDLL(CORE_FOUNDATION_FRAMEWORK)

            create_source = iokit.IOPSNotificationCreateRunLoopSource
            create_source.argtypes = [PowerSourceCallback, ctypes.c_void_p]
            create_source.restype = ctypes.c_void_p
            core_foundation.CFRunLoopGetMain.restype = ctypes.c_void_p
            core_foundation.CFRunLoopAddSource.argtypes = [
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_void_p,
            ]
            core_foundation.CFRunLoopRemoveSource.argtypes = [
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_void_p,
            ]
            core_foundation.CFRelease.argtypes = [ctypes.c_void_p]

            callback_ref = PowerSourceCallback(self._notify)
            source = create_source(callback_ref, None)
            if not source:
                raise RuntimeError("IOKit did not create a notification source")
            run_loop = core_foundation.CFRunLoopGetMain()
            common_mode = ctypes.c_void_p.in_dll(
                core_foundation,
                "kCFRunLoopCommonModes",
            ).value
            event_tracking_mode = NSEventTrackingRunLoopMode.__c_void_p__().value
            if not run_loop or not common_mode or not event_tracking_mode:
                core_foundation.CFRelease(source)
                raise RuntimeError("Core Foundation main run loop is unavailable")

            for mode in (common_mode, event_tracking_mode):
                core_foundation.CFRunLoopAddSource(run_loop, source, mode)
        except Exception as exc:
            self.last_error = str(exc)
            return False

        self._iokit = iokit
        self._core_foundation = core_foundation
        self._callback_ref = callback_ref
        self._source = source
        self._run_loop = run_loop
        self._modes = (common_mode, event_tracking_mode)
        self.last_error = None
        return True

    def stop(self) -> None:
        if self._source is None or self._core_foundation is None:
            return
        for mode in self._modes or ():
            self._core_foundation.CFRunLoopRemoveSource(
                self._run_loop,
                self._source,
                mode,
            )
        self._core_foundation.CFRelease(self._source)
        self._source = None
        self._run_loop = None
        self._modes = None
        self._callback_ref = None
        self._core_foundation = None
        self._iokit = None

    def _notify(self, _context) -> None:
        try:
            self.callback()
        except Exception as exc:
            self.last_error = str(exc)
