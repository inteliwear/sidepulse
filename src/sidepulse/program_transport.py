"""Local JSON submission and individual status delivery over the existing bridge."""
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

from .program_status import MAX_BYTES, ProgramState, StatusError, encoded
from .providers import default_state_dir, candidate_state_dirs


def status_socket_path(state_dir: Path | None = None) -> Path:
    path = (state_dir or default_state_dir()) / 'program-status.sock'
    if len(str(path).encode()) > 96:
        digest = hashlib.sha256(str(path).encode()).hexdigest()[:16]
        return Path('/tmp') / f'sidepulse-status-{os.getuid()}' / f'{digest}.sock'
    return path


def submit_report(report: dict, *, timeout: float = 0.5, path: Path | None = None) -> dict:
    paths = (path,) if path is not None else tuple(dict.fromkeys([
        status_socket_path(), *(status_socket_path(directory) for directory in candidate_state_dirs())]))
    for target in paths:
        try:
            return _submit_report_at(report, target, timeout)
        except (FileNotFoundError, ConnectionRefusedError):
            if target == paths[-1]:
                raise


def _submit_report_at(report: dict, path: Path, timeout: float) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(str(path))
        client.sendall(encoded(report).encode())
        client.shutdown(socket.SHUT_WR)
        data = bytearray()
        while chunk := client.recv(4096):
            data.extend(chunk)
            if len(data) > 1024 * 1024:
                raise StatusError('Daemon response exceeds its size limit')
    reply = json.loads(data)
    if not isinstance(reply, dict):
        raise StatusError('Invalid daemon response')
    if not reply.get('ok'):
        raise StatusError(reply.get('error', 'Report rejected'))
    return reply


def submit_status(report: dict, *, timeout: float = 0.5) -> dict:
    import uuid
    report = dict(report)
    report.setdefault('request_id', str(uuid.uuid4()))
    try:
        return submit_report(report, timeout=timeout)
    except OSError as exc:
        raise StatusError('SidePulse daemon unavailable; start it with sidepulse service start') from exc


def daemon_command(command: dict, *, timeout: float = 0.5, path: Path | None = None) -> dict:
    try:
        return submit_report({'v': 1, **command}, timeout=timeout, path=path)
    except OSError as exc:
        raise StatusError('SidePulse daemon unavailable; start it with sidepulse service start') from exc


def write_daemon_program(program: str, *, device_path: Path, hold_seconds: float = 1.5) -> Path:
    reply = daemon_command({'type': 'write_led', 'program': program, 'target': str(device_path),
                            'hold_seconds': hold_seconds}, timeout=5)
    return Path(reply['target'])


class ProgramServer:
    def __init__(self, state: ProgramState, path: Path | None = None, on_command=None):
        self.state, self.path, self.on_command = state, path or status_socket_path(), on_command
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
        from concurrent.futures import ThreadPoolExecutor
        self.workers = ThreadPoolExecutor(max_workers=4, thread_name_prefix='sidepulse-ipc')
        self.slots = threading.BoundedSemaphore(4)
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
            if not self.slots.acquire(blocking=False):
                try:
                    conn.sendall(b'{"ok":false,"error":"Daemon is busy; retry shortly"}')
                except OSError:
                    pass
                conn.close()
                continue
            self.workers.submit(self.handle_connection, conn)

    def handle_connection(self, conn):
        try:
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
                    if report.get('type') == 'get_state':
                        if type(report.get('v')) is not int or report['v'] != 1:
                            raise StatusError('Unsupported protocol version')
                        from .program_presentation import ProgramMonitor
                        ack = {'ok': True, 'snapshot': ProgramMonitor(self.state).snapshot().to_dict()}
                        if self.on_command:
                            ack.update(self.on_command({'v': 1, 'type': 'output_info'}))
                    elif report.get('type') == 'acknowledge_session':
                        if type(report.get('v')) is not int or report['v'] != 1:
                            raise StatusError('Unsupported protocol version')
                        ack = {'ok': True, 'acknowledged': self.state.acknowledge_session(
                            report.get('agent_id'), report.get('seq'), report.get('updated_at'))}
                    elif report.get('type') in {'output_control', 'write_led', 'play_lid_animation'} and self.on_command:
                        ack = self.on_command(report)
                    else:
                        ack = self.state.submit(report)
                except (ValueError, OSError, TypeError) as exc:
                    ack = {'ok': False, 'error': str(exc)}
                except Exception:
                    ack = {'ok': False, 'error': 'Could not account report'}
                try:
                    conn.sendall(json.dumps(ack, ensure_ascii=False, allow_nan=False).encode('utf-8', errors='replace'))
                except OSError:
                    pass

        finally:
            self.slots.release()

    def stop(self):
        self.running = False
        if self.socket:
            self.socket.close()
        if hasattr(self, 'thread'):
            self.thread.join(timeout=1)
            self.path.unlink(missing_ok=True)
        if hasattr(self, 'workers'):
            self.workers.shutdown(wait=False, cancel_futures=True)


def endpoint(server: str, channel: str) -> str:
    from .links import normalize_server
    return f'{normalize_server(server)}/api/leds/{urllib.parse.quote(channel, safe="")}'


def sender_identity(server: str, channel: str) -> str:
    return 'relay-' + hashlib.sha256(endpoint(server, channel).encode()).hexdigest()[:16]


def publish_update(server: str, channel: str, update: dict, *, phone: bool = False) -> None:
    # Existing phone push handlers carry application data under `data`.
    body = encoded({'data': update} if phone else update).encode()
    request = urllib.request.Request(endpoint(server, channel), data=body,
        method='POST', headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            if response.read(128).strip() not in {b'OK', b'OK QUEUED'}:
                raise StatusError('Invalid bridge acknowledgement')
    except Exception as exc:
        # URLs contain credentials. Pending updates remain in daemon memory for retry.
        raise StatusError('Status bridge unavailable; update pending in daemon memory') from exc


class ProgramRelay:
    def __init__(self, state: ProgramState):
        import threading
        self.state = state
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
            self.stop_event.wait(1)

    def stop(self):
        self.stop_event.set()
        if hasattr(self, 'thread'):
            self.thread.join(timeout=1)

    def sync(self):
        from .program_status import phone_update
        from .links import load_phone_links
        from .relay import load_relay_config
        from .settings import load_settings
        config = load_relay_config()
        sender = sender_identity(config.server, config.receiver_channel) if config.receiver_channel else None
        self.state.retain_senders({'local', sender} if sender else {'local'})
        targets = []
        if config.outbound_channel:
            targets.append((config.server, config.outbound_channel, False))
        settings = load_settings()
        configured = list(targets)
        for link in load_phone_links():
            if link.status_protocol == 2:
                channel = link.token if link.platform == 'android' else 'apns_' + link.token
                target = (link.server, channel, True)
                configured.append(target)
                if settings.sync_sessions_to_phone(link.link_id):
                    targets.append(target)
        self.state.retain_targets({hashlib.sha256(json.dumps([server, channel, phone]).encode()).hexdigest()
                                   for server, channel, phone in configured})
        for server, channel, phone in targets:
            target = hashlib.sha256(json.dumps([server, channel, phone]).encode()).hexdigest()
            for number, update in self.state.pending_events(target, local_only=not phone):
                if self.stop_event.is_set():
                    return
                try:
                    publish_update(server, channel, phone_update(update) if phone else update, phone=phone)
                except StatusError:
                    break  # Retry this event in order; do not block another destination.
                self.state.acknowledge_event(target, number)
