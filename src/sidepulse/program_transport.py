"""Local JSON submission and snapshot delivery over the existing bridge."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import socket
import time
import urllib.parse
import urllib.request
from pathlib import Path

from .program_status import MAX_BYTES, ProgramStore, StatusError, encoded
from .providers import default_state_dir


def status_socket_path() -> Path:
    path = default_state_dir() / 'program-status.sock'
    if len(str(path).encode()) > 96:
        digest = hashlib.sha256(str(path).encode()).hexdigest()[:16]
        return Path('/tmp') / f'sidepulse-status-{os.getuid()}' / f'{digest}.sock'
    return path


def submit_report(report: dict, *, timeout: float = 0.5, path: Path | None = None) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(str(path or status_socket_path()))
        client.sendall(encoded(report).encode())
        client.shutdown(socket.SHUT_WR)
        data = bytearray()
        while chunk := client.recv(4096):
            data.extend(chunk)
            if len(data) > 4096:
                raise StatusError('Invalid daemon acknowledgement')
    reply = json.loads(data)
    if not reply.get('ok'):
        raise StatusError(reply.get('error', 'Report rejected'))
    return reply


def durable_submit(report: dict, *, timeout: float = 0.5) -> dict:
    import uuid
    report = dict(report)
    report.setdefault('request_id', str(uuid.uuid4()))
    try:
        return submit_report(report, timeout=timeout)
    except OSError:
        store = ProgramStore()
        try:
            return store.submit(report)
        finally:
            store.close()


class ProgramServer:
    def __init__(self, store: ProgramStore, path: Path | None = None):
        self.store, self.path = store, path or status_socket_path()
        self.socket = None
        self.running = False

    def start(self):
        import threading
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.parent.parent == Path('/tmp') and self.path.parent.name == f'sidepulse-status-{os.getuid()}':
            info = self.path.parent.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise StatusError('Unsafe program status socket directory')
        if self.path.exists():
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.1)
                try:
                    probe.connect(str(self.path))
                except (ConnectionRefusedError, FileNotFoundError):
                    self.path.unlink(missing_ok=True)
                else:
                    raise StatusError('Program status service already running')
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.bind(str(self.path))
        self.path.chmod(0o600)
        self.socket.listen(16)
        self.socket.settimeout(0.2)
        self.running = True
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        while self.running:
            try:
                conn, _ = self.socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with conn:
                conn.settimeout(0.5)
                try:
                    data = bytearray()
                    deadline = time.monotonic() + 1
                    while chunk := conn.recv(4096):
                        if time.monotonic() > deadline:
                            raise StatusError("Report deadline exceeded")
                        data.extend(chunk)
                        if len(data) > MAX_BYTES:
                            raise StatusError('Report too large')
                    report = json.loads(data)
                    if not isinstance(report, dict):
                        raise StatusError('Report must be an object')
                    ack = self.store.submit(report)
                except (ValueError, OSError, TypeError) as exc:
                    ack = {'ok': False, 'error': str(exc)}
                except Exception:
                    ack = {'ok': False, 'error': 'Could not persist report'}
                try:
                    conn.sendall(json.dumps(ack).encode())
                except OSError:
                    pass

    def stop(self):
        self.running = False
        if self.socket:
            self.socket.close()
            self.thread.join(timeout=1)
            self.path.unlink(missing_ok=True)


def endpoint(server: str, channel: str) -> str:
    from .links import normalize_server
    return f'{normalize_server(server)}/api/leds/{urllib.parse.quote(channel, safe="")}'


def sender_identity(server: str, channel: str) -> str:
    return 'relay-' + hashlib.sha256(endpoint(server, channel).encode()).hexdigest()[:16]


def publish_snapshot(server: str, channel: str, snapshot: dict, *, phone: bool = False) -> None:
    # Existing phone push handlers carry application data under `data`.
    body = encoded({'data': snapshot} if phone else snapshot).encode()
    request = urllib.request.Request(endpoint(server, channel), data=body,
        method='POST', headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            if response.read(128).strip() not in {b'OK', b'OK QUEUED'}:
                raise StatusError('Invalid bridge acknowledgement')
    except Exception as exc:
        # URLs contain credentials. The local ledger remains available for retry.
        raise StatusError('Status bridge unavailable; update saved locally for retry') from exc


class ProgramRelay:
    def __init__(self, store: ProgramStore):
        import threading
        self.store = store
        self.sent = {}
        self.stop_event = threading.Event()

    def start(self):
        import threading
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.sync()
            except Exception:
                pass  # Retained state will be retried. Do not log credential URLs.
            self.stop_event.wait(5)

    def stop(self):
        self.stop_event.set()
        if hasattr(self, 'thread'):
            self.thread.join(timeout=1)

    def sync(self):
        import time
        from .program_status import fits_phone_push
        from .links import load_phone_links
        from .relay import load_relay_config
        from .settings import load_settings
        config = load_relay_config()
        sender = sender_identity(config.server, config.receiver_channel) if config.receiver_channel else None
        self.store.retain_senders({'local', sender} if sender else {'local'})
        # Incoming snapshots share the service's existing SSE receiver with raw
        # hook events. A second listener would compete for queued messages.
        targets = []
        if config.outbound_channel:
            targets.append(((config.server, config.outbound_channel), self.store.local_snapshot(), False))
        settings = load_settings()
        for link in load_phone_links():
            if link.status_protocol != 1 or not settings.sync_sessions_to_phone(link.link_id):
                continue
            try:
                snapshot = self.store.phone_snapshot()
                if not fits_phone_push(snapshot):
                    continue  # Never replace missing session state with direct LED control.
            except StatusError:
                continue  # Capacity must not interrupt independent desktop relay delivery.
            targets.append(((link.server, link.token if link.platform == 'android' else 'apns_' + link.token), snapshot, True))
        active_targets = {target for target, _, _ in targets}
        self.sent = {target: value for target, value in self.sent.items() if target in active_targets}
        now = time.monotonic()
        for target, snapshot, phone in targets:
            previous = self.sent.get(target)
            if snapshot['revision'] and (previous is None or previous[0] != snapshot['revision'] or now - previous[1] >= 60):
                try:
                    publish_snapshot(*target, snapshot, phone=phone)
                    self.sent[target] = (snapshot['revision'], now)
                except StatusError:
                    pass
