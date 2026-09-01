from __future__ import annotations

import json
import socket
import threading
from pathlib import Path
from typing import Callable

from .providers import candidate_state_dirs, default_state_dir


MAX_EVENT_BYTES = 1024 * 1024
HOOK_EVENT_SEND_TIMEOUT_SECONDS = 0.2


def default_event_socket_path() -> Path:
    return default_state_dir() / "events.sock"


def candidate_event_socket_paths() -> tuple[Path, ...]:
    """Socket paths a listening app may have bound, most likely first."""
    return tuple(directory / "events.sock" for directory in candidate_state_dirs())


def default_latest_state_path() -> Path:
    return default_state_dir() / "latest.json"


def send_hook_event(
    provider: str,
    line: dict,
    *,
    socket_path: Path | None = None,
    timeout: float = HOOK_EVENT_SEND_TIMEOUT_SECONDS,
) -> bool:
    payload = json.dumps(
        {"provider": provider, "line": line},
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    if len(payload) > MAX_EVENT_BYTES:
        return False

    if socket_path is not None:
        return _send_payload(socket_path.expanduser(), payload, timeout)

    # The caller and the app can resolve different state dirs, so try each candidate.
    for target in candidate_event_socket_paths():
        if _send_payload(target.expanduser(), payload, timeout):
            return True
    return False


def _send_payload(target: Path, payload: bytes, timeout: float) -> bool:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(str(target))
        client.sendall(payload)
        return True
    except OSError:
        return False
    finally:
        client.close()


class HookEventServer:
    def __init__(
        self,
        on_event: Callable[[str, dict], None],
        *,
        socket_path: Path | None = None,
    ) -> None:
        self.on_event = on_event
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
        provider = message.get("provider")
        line = message.get("line")
        if isinstance(provider, str) and isinstance(line, dict):
            self.on_event(provider, line)
