from __future__ import annotations

import json
import socket
import threading
from pathlib import Path
from typing import Callable

from .providers import candidate_state_dirs, default_state_dir


MAX_EVENT_BYTES = 1024 * 1024


def default_event_socket_path() -> Path:
    return default_state_dir() / "events.sock"


def candidate_event_socket_paths() -> tuple[Path, ...]:
    """Socket paths a listening app may have bound, most likely first."""
    return tuple(directory / "events.sock" for directory in candidate_state_dirs())


def default_latest_state_path() -> Path:
    return default_state_dir() / "latest.json"


def request_settings_window(*, socket_path: Path | None = None) -> bool:
    """Ask a running UI to open settings, requiring an acknowledgement."""
    targets = (socket_path,) if socket_path is not None else candidate_event_socket_paths()
    for target in targets:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(0.5)
            try:
                client.connect(str(target.expanduser()))
                client.sendall(b'{"command":"open-settings"}')
                client.shutdown(socket.SHUT_WR)
                if client.recv(2) == b"ok":
                    return True
            except OSError:
                continue
    return False


class UICommandServer:
    def __init__(
        self,
        *,
        socket_path: Path | None = None,
        on_open_settings: Callable[[], None] | None = None,
    ) -> None:
        self.on_open_settings = on_open_settings
        self.socket_path = (socket_path or default_event_socket_path()).expanduser()
        self.socket: socket.socket | None = None
        self.thread: threading.Thread | None = None
        self.running = False

    def start(self) -> Path:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.socket_path))
        server.listen(16)
        server.settimeout(0.5)
        self.socket = server
        self.running = True
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()
        return self.socket_path

    def stop(self) -> None:
        self.running = False
        server = self.socket
        self.socket = None
        if server is not None:
            try:
                server.close()
            except OSError:
                pass
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    def _serve(self) -> None:
        while self.running:
            server = self.socket
            if server is None:
                return
            try:
                connection, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                return

            with connection:
                self._handle_connection(connection)

    def _handle_connection(self, connection: socket.socket) -> None:
        chunks: list[bytes] = []
        total = 0
        while True:
            try:
                chunk = connection.recv(65536)
            except OSError:
                return
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_EVENT_BYTES:
                return
            chunks.append(chunk)

        try:
            message = json.loads(b"".join(chunks).decode("utf-8"))
        except Exception:
            return

        if not isinstance(message, dict):
            return
        if message.get("command") == "open-settings":
            if self.on_open_settings is not None:
                self.on_open_settings()
                try:
                    connection.sendall(b"ok")
                except OSError:
                    pass
            return
