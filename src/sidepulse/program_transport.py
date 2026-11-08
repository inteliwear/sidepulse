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
import urllib.error
import errno
from pathlib import Path

from .program_status import MAX_BYTES, MAX_BRIDGE_MESSAGE_BYTES, ProgramState, StatusError, bridge_update, encoded, phone_update
from .providers import default_state_dir, candidate_state_dirs


def status_socket_path(state_dir: Path | None = None) -> Path:
    path = (state_dir or default_state_dir()) / 'program-status.sock'
    if os.name != 'nt' and len(str(path).encode()) > 96:
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
    if os.name == 'nt':
        # CPython on Windows does not expose AF_UNIX. Only the protected
        # endpoint file carries the random credential; it never goes to logs.
        endpoint = json.loads(path.read_text(encoding='utf-8'))
        if (not isinstance(endpoint, dict) or endpoint.get('host') != '127.0.0.1'
                or type(endpoint.get('port')) is not int or not 1 <= endpoint['port'] <= 65535
                or not isinstance(endpoint.get('token'), str) or len(endpoint['token']) != 64):
            raise StatusError('Invalid local daemon endpoint')
        address, family = ('127.0.0.1', endpoint['port']), socket.AF_INET
        encoded(report)  # Apply the same size/number limits as Unix submissions.
        payload = json.dumps({'auth': endpoint['token'], 'report': report},
                             ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()
    else:
        address, family = str(path), socket.AF_UNIX
        payload = encoded(report).encode()
    with socket.socket(family, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(address)
        client.sendall(payload)
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
        self.bound_identity = None
        self.windows_lock = None
        self.token = None

    def start(self):
        import threading
        if os.name == 'nt':
            self._start_windows()
            return
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
        info = self.path.stat()
        self.bound_identity = (info.st_dev, info.st_ino)
        self.path.chmod(0o600)
        self.socket.listen(16)
        self.socket.settimeout(0.2)
        from concurrent.futures import ThreadPoolExecutor
        self.workers = ThreadPoolExecutor(max_workers=4, thread_name_prefix='sidepulse-ipc')
        self.slots = threading.BoundedSemaphore(4)
        self.running = True
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def _start_windows(self):
        import secrets
        import threading
        from concurrent.futures import ThreadPoolExecutor
        from .windows_support import file_lock, private_directory
        private_directory(self.path.parent)
        lock = file_lock(self.path.with_suffix('.lock'), blocking=False)
        try:
            lock.__enter__()
        except OSError as exc:
            raise StatusError('Program status service already running') from exc
        self.windows_lock = lock
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.bind(('127.0.0.1', 0))
            self.socket.listen(16)
            self.socket.settimeout(.2)
            self.token = secrets.token_hex(32)
            import tempfile
            with tempfile.NamedTemporaryFile(dir=self.path.parent, mode='w', encoding='utf-8', delete=False) as stream:
                temporary = Path(stream.name)
                json.dump({'host': '127.0.0.1', 'port': self.socket.getsockname()[1], 'token': self.token}, stream)
            try:
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)
            info = self.path.stat()
            self.bound_identity = (info.st_dev, info.st_ino)
            self.workers = ThreadPoolExecutor(max_workers=4, thread_name_prefix='sidepulse-ipc')
            self.slots = threading.BoundedSemaphore(4)
            self.running = True
            self.thread = threading.Thread(target=self.serve, daemon=True)
            self.thread.start()
        except BaseException:
            self.stop()
            raise

    def serve(self):
        while self.running:
            try:
                conn, _ = self.socket.accept()
            except socket.timeout:
                continue
            except OSError as exc:
                if not self.running or exc.errno in {errno.EBADF, errno.ENOTSOCK, errno.EINVAL}:
                    self.running = False
                    break
                # Aborted clients and temporary resource pressure must not kill IPC.
                time.sleep(0.05)
                continue
            if not self.slots.acquire(blocking=False):
                try:
                    conn.sendall(b'{"ok":false,"error":"Daemon is busy; retry shortly"}')
                except OSError:
                    pass
                conn.close()
                continue
            try:
                self.workers.submit(self.handle_connection, conn)
            except RuntimeError:
                conn.close()
                self.slots.release()
                self.running = False

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
                        if len(data) > MAX_BYTES + (256 if self.token else 0):
                            raise StatusError('Report too large')
                    report = json.loads(data)
                    if not isinstance(report, dict):
                        raise StatusError('Report must be an object')
                    if self.token:
                        import secrets
                        auth = report.get('auth')
                        if not isinstance(auth, str) or not secrets.compare_digest(auth, self.token):
                            raise StatusError('Local daemon authentication failed')
                        report = report.get('report')
                        if not isinstance(report, dict):
                            raise StatusError('Report must be an object')
                        if len(encoded(report).encode()) > MAX_BYTES:
                            raise StatusError('Report too large')
                    if report.get('type') == 'get_state':
                        if type(report.get('v')) is not int or report['v'] != 1:
                            raise StatusError('Unsupported protocol version')
                        from .program_presentation import ProgramMonitor
                        ack = {'ok': True, 'snapshot': ProgramMonitor(self.state).snapshot().to_dict()}
                        if self.on_command:
                            ack.update(self.on_command({'v': 1, 'type': 'output_info'}))
                    elif report.get('type') in {'output_control', 'write_led', 'play_lid_animation', 'service_stop'} and self.on_command:
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
        if self.bound_identity is not None:
            try:
                info = self.path.stat()
                if (info.st_dev, info.st_ino) == self.bound_identity:
                    self.path.unlink(missing_ok=True)
            except FileNotFoundError:
                pass
            self.bound_identity = None
        if hasattr(self, 'workers'):
            self.workers.shutdown(wait=False, cancel_futures=True)
        if self.windows_lock is not None:
            lock, self.windows_lock = self.windows_lock, None
            lock.__exit__(None, None, None)


def endpoint(server: str, channel: str) -> str:
    from .links import normalize_server
    return f'{normalize_server(server)}/api/leds/{urllib.parse.quote(channel, safe="")}'


def sender_identity(server: str, channel: str) -> str:
    return 'relay-' + hashlib.sha256(endpoint(server, channel).encode()).hexdigest()[:16]


class UndeliverableStatus(StatusError):
    """This value cannot be delivered; discard it and let newer values through."""


class BridgeFailure(StatusError):
    def __init__(self, *, retry_after=0.0, retired=False, status_code=None):
        super().__init__('Status bridge unavailable')
        self.retry_after, self.retired, self.status_code = retry_after, retired, status_code


def publish_update(server: str, channel: str, update: dict, *, phone: bool = False) -> None:
    try:
        update = phone_update(update) if phone else bridge_update(update)
        body = encoded({'data': update} if phone else update).encode()
        while len(body) > MAX_BRIDGE_MESSAGE_BYTES and update['status'].get('msg'):
            update['status']['msg'] = update['status']['msg'][:-1]
            body = encoded({'data': update} if phone else update).encode()
        if len(body) > MAX_BRIDGE_MESSAGE_BYTES:
            raise StatusError('Status metadata exceeds the 2048-byte bridge limit')
    except StatusError as exc:
        raise UndeliverableStatus(str(exc)) from exc
    request = urllib.request.Request(endpoint(server, channel), data=body,
        method='POST', headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            if response.read(128).strip() not in {b'OK', b'OK QUEUED'}:
                raise BridgeFailure()
    except urllib.error.HTTPError as exc:
        if exc.code in {400, 413, 422}:
            raise UndeliverableStatus('Bridge rejected this status value') from None
        delay = 0.0
        retry = exc.headers.get('Retry-After', '') if exc.headers else ''
        try:
            delay = max(0.0, float(retry))
        except ValueError:
            from email.utils import parsedate_to_datetime
            try:
                delay = max(0.0, parsedate_to_datetime(retry).timestamp() - time.time())
            except (ValueError, TypeError, OverflowError):
                pass
        raise BridgeFailure(retry_after=delay, retired=exc.code in {401, 403, 404, 410}, status_code=exc.code) from None
    except Exception as exc:
        # Never include credential-bearing URLs in diagnostics.
        if isinstance(exc, BridgeFailure):
            raise
        raise BridgeFailure() from None


class ProgramRelay:
    def __init__(self, state: ProgramState):
        import threading
        self.state = state
        self.stop_event = threading.Event()
        from concurrent.futures import ThreadPoolExecutor
        self.workers = ThreadPoolExecutor(max_workers=4, thread_name_prefix='sidepulse-delivery')
        self.jobs = {}
        self.retry = {}
        self.targets = set()

    def start(self):
        import threading
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.sync(wait=False)
            except Exception:
                pass  # Retained state will be retried. Do not log credential URLs.
            self.stop_event.wait(1)

    def stop(self):
        self.stop_event.set()
        if hasattr(self, 'thread'):
            self.thread.join(timeout=1)
        self.workers.shutdown(wait=False, cancel_futures=True)

    def sync(self, *, wait=True):
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
        configured_ids = {self.target_id(*entry) for entry in configured}
        self.state.retain_targets(configured_ids)
        active = {self.target_id(*entry) for entry in targets}
        for target in active - self.targets:
            self.state.start_delivery(target)
        self.targets = active
        for target in set(self.retry) - configured_ids:
            self.retry.pop(target, None)
        for server, channel, phone in targets:
            target = self.target_id(server, channel, phone)
            previous = self.jobs.get(target)
            if previous is not None and not previous.done():
                continue
            failures, retry_at = self.retry.get(target, (0, 0))
            if time.monotonic() >= retry_at:
                self.jobs[target] = self.workers.submit(self.deliver, target, server, channel, phone)
        if wait:
            from concurrent.futures import wait as wait_for_jobs
            wait_for_jobs(list(self.jobs.values()))
        self.jobs = {key: job for key, job in self.jobs.items() if not job.done()}

    @staticmethod
    def target_id(server, channel, phone):
        return hashlib.sha256(json.dumps([server, channel, phone]).encode()).hexdigest()

    def deliver(self, target, server, channel, phone):
        for number, update in self.state.pending_events(target, local_only=not phone):
            if self.stop_event.is_set() or target not in self.targets:
                return
            # A slow earlier send may span a newer value or disable/re-enable.
            if not self.state.event_is_pending(target, number):
                continue
            try:
                publish_update(server, channel, update, phone=phone)
            except UndeliverableStatus:
                self.state.acknowledge_event(target, number)
                continue
            except StatusError as exc:
                failures = self.retry.get(target, (0, 0))[0] + 1
                delay = max(min(300, 2 ** min(failures, 9)), getattr(exc, 'retry_after', 0))
                self.retry[target] = (failures, float('inf') if getattr(exc, 'retired', False) else time.monotonic() + delay)
                return
            self.retry.pop(target, None)
            self.state.acknowledge_event(target, number)
