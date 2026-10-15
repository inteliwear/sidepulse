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


def request_program_show(
    program: str,
    seconds: float | None,
    *,
    socket_path: Path | None = None,
) -> str | None:
    """Ask a running UI to play an LED program, then restore live status.

    With seconds=None, the program stays until request_program_clear() or the
    next show. Returns the reply, "ok" or "error: <reason>", or None when no UI
    answers.
    """
    message: dict[str, object] = {"command": "show", "program": program}
    if seconds is None:
        message["hold"] = True
    else:
        message["seconds"] = seconds
    return _request_reply(message, socket_path)


def request_program_clear(*, socket_path: Path | None = None) -> str | None:
    """Ask a running UI to end any show at once and restore live status."""
    return _request_reply({"command": "clear"}, socket_path)


def _request_reply(message: dict[str, object], socket_path: Path | None) -> str | None:
    payload = json.dumps(
        message,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    targets = (socket_path,) if socket_path is not None else candidate_event_socket_paths()
    for target in targets:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(0.5)
            try:
                client.connect(str(target.expanduser()))
                client.sendall(payload)
                client.shutdown(socket.SHUT_WR)
                reply = b"".join(iter(lambda: client.recv(1024), b""))
            except OSError:
                continue
            if reply:
                return reply.decode("utf-8", "replace")
    return None


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
        on_open_settings: Callable[[], None] | None = None,
        on_show: Callable[[str, float | None], None] | None = None,
        on_clear: Callable[[], None] | None = None,
    ) -> None:
        self.on_event = on_event
        self.on_open_settings = on_open_settings
        self.on_show = on_show
        self.on_clear = on_clear
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
        if message.get("command") == "clear":
            if self.on_clear is not None:
                self.on_clear()
                try:
                    connection.sendall(b"ok")
                except OSError:
                    pass
            return
        if message.get("command") == "show":
            if self.on_show is None:
                return
            program = message.get("program")
            seconds = message.get("seconds")
            hold = message.get("hold") is True
            # Catch every handler error and report it to the client.
            # An uncaught error stops the accept loop.
            try:
                if not isinstance(program, str):
                    raise ValueError("show needs a string program")
                if hold:
                    if seconds is not None:
                        raise ValueError("show takes seconds or hold, not both")
                elif isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
                    raise ValueError("show needs a number of seconds, or hold")
                self.on_show(program, None if hold else float(seconds))
                reply = b"ok"
            except Exception as exc:
                reply = f"error: {exc}".encode("utf-8")
            try:
                connection.sendall(reply)
            except OSError:
                pass
            return
        provider = message.get("provider")
        line = message.get("line")
        if isinstance(provider, str) and isinstance(line, dict):
            self.on_event(provider, line)
