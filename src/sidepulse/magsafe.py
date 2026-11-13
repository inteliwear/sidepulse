"""Opt-in MagSafe output through a bundled, natively approved LED helper."""
from __future__ import annotations

import select
import socket
import struct
import threading
import time

from .led_status import display_state_for_mode
from .models import AgentMode
from .magsafe_connection import read_magsafe_connection

MAGSAFE_DEVICE_ID = "magsafe:3"
MAGSAFE_DEVICE_NAME = "MagSafe 3 LED"
MAGSAFE_HELPER_PLIST = "io.sidepulse.magsafe.helper.plist"
MAGSAFE_SOCKET = "/var/run/sidepulse-magsafe.sock"


def native_service():
    from ServiceManagement import SMAppService
    return SMAppService.daemonServiceWithPlistName_(MAGSAFE_HELPER_PLIST)


def unregister_helper():
    service = native_service()
    if int(service.status()) in (0, 3):
        return
    success, error = service.unregisterAndReturnError_(None)
    if not success:
        raise RuntimeError(str(error or "Could not unregister the MagSafe helper."))

def magsafe_state(mode: AgentMode, *, sleep_allowed: bool = False) -> str:
    return "idle" if sleep_allowed else display_state_for_mode(mode).value


class MagSafeController:
    """Registration is explicit; background status refreshes only check approval."""

    def __init__(self, *, connection_reader=None, on_reconnect=None,
                 poll_seconds=0.75, retry_seconds=2.0):
        self.ready = False
        self.connection: bool | None = None
        self._observed_connection = False
        self._read_connection = connection_reader or read_magsafe_connection
        self._on_reconnect = on_reconnect
        self._poll_seconds = poll_seconds
        self._retry_seconds = retry_seconds
        self._fresh_state = threading.Event()
        self._permission_status = None
        self.last_error: str | None = None
        self._state = "idle"
        self._updated = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def update(self, mode: AgentMode, *, sleep_allowed: bool = False):
        self._state = magsafe_state(mode, sleep_allowed=sleep_allowed)
        self._updated = time.monotonic()
        self._fresh_state.set()

    def request_approval(self):
        if self._thread is not None and not self._thread.is_alive():
            self._thread = None  # An explicit setup action can retry a failed connection.
        try:
            service = native_service()
            status = int(service.status())
            self._permission_status = status
            if status == 2:  # RequiresApproval
                from ServiceManagement import SMAppService
                SMAppService.openSystemSettingsLoginItems()
            elif status != 1:  # Enabled
                success, error = service.registerAndReturnError_(None)
                if not success:
                    raise RuntimeError(str(error or "Could not register the MagSafe helper."))
            self.last_error = None
        except Exception as exc:
            self.last_error = str(exc)

    def start(self):
        if self._thread is not None:
            if self._thread.is_alive():
                return
            self._thread = None
        try:
            status = int(native_service().status())
            self._permission_status = status
            if status != 1:
                if status == 2:
                    self.last_error = "Approve the MagSafe helper in System Settings."
                elif self.last_error is None:
                    self.last_error = "Set up the MagSafe helper to enable LED control."
                return
        except Exception as exc:
            self.last_error = str(exc)
            return
        self._stop.clear()
        self.last_error = None
        self.connection = None
        self._observed_connection = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
            if not self._thread.is_alive():
                self._thread = None

    @property
    def needs_setup(self):
        return self._permission_status != 1

    @property
    def status_reason(self):
        if self._observed_connection and self.connection is False:
            return "MagSafe disconnected"
        if self._observed_connection and self.connection is None:
            return "MagSafe connection unavailable"
        return self.last_error or ("Agent Status" if self.ready else "Connecting to MagSafe helper…")

    def _open_session(self):
        self._permission_status = int(native_service().status())
        if self._permission_status != 1:
            raise RuntimeError("Approve the MagSafe helper in System Settings.")
        connection = socket.socket(socket.AF_UNIX)
        try:
            connection.settimeout(1.5)
            connection.connect(MAGSAFE_SOCKET)
            peer = connection.getsockopt(0, socket.LOCAL_PEERCRED, 76)
            if struct.unpack_from("=I", peer, 4)[0] != 0:
                raise RuntimeError("MagSafe helper is not authorized.")
            reply = self._read_line(connection)
            if reply != "READY":
                raise RuntimeError(reply or "MagSafe helper could not start.")
            return connection
        except Exception:
            connection.close()
            raise

    def _release_session(self, connection):
        self.ready = False
        try:
            connection.sendall(b"quit\n")
            if self._read_line(connection) != "RESTORED":
                raise RuntimeError("MagSafe restoration could not be confirmed.")
        except Exception as exc:
            self.last_error = self.last_error or str(exc)
        finally:
            connection.close()

    def _run(self):
        connection = None
        retry_at = 0.0
        awaiting_state = False
        try:
            while not self._stop.is_set():
                previous = self.connection
                try:
                    self.connection = self._read_connection()
                except Exception as exc:
                    self.connection = None
                    self.last_error = str(exc)
                self._observed_connection = True
                if self.connection is not True:
                    if connection is not None:
                        self._release_session(connection)
                        connection = None
                    retry_at = 0.0
                else:
                    if previous is not True and self._on_reconnect is not None:
                        # Recompute SidePulse's aggregate, including any state
                        # expiry that occurred while the cable was absent.
                        self._fresh_state.clear()
                        awaiting_state = True
                        self._on_reconnect()
                    if awaiting_state and self._fresh_state.is_set():
                        awaiting_state = False
                    fresh = not awaiting_state and time.monotonic() - self._updated <= 45
                    if not fresh:
                        if connection is not None:
                            self._release_session(connection)
                            connection = None
                        self.last_error = "Waiting for current SidePulse status."
                    elif time.monotonic() >= retry_at:
                        try:
                            if connection is None:
                                connection = self._open_session()
                                self.last_error = None
                                self.ready = True
                            connection.sendall((self._state + "\n").encode("ascii"))
                            if select.select([connection], [], [], 0)[0]:
                                reply = self._read_line(connection)
                                raise RuntimeError(reply or "MagSafe helper disconnected.")
                        except Exception as exc:
                            self.last_error = str(exc)
                            if connection is not None:
                                self._release_session(connection)
                                connection = None
                            self.ready = False
                            retry_at = time.monotonic() + self._retry_seconds
                self._stop.wait(self._poll_seconds)
        except Exception as exc:
            self.last_error = str(exc)
        finally:
            if connection is not None:
                self._release_session(connection)
            self.ready = False

    @staticmethod
    def _read_line(connection):
        data = bytearray()
        while len(data) < 512:
            part = connection.recv(1)
            if not part or part == b"\n":
                return data.decode(errors="replace")
            data.extend(part)
        raise RuntimeError("Invalid response from MagSafe helper.")
