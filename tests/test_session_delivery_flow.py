"""Local socket -> independent HTTP delivery, without credentials or devices."""
import errno
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

from sidepulse.program_status import ProgramState
from sidepulse.program_transport import ProgramRelay, ProgramServer, daemon_command, submit_report, status_socket_path
from sidepulse.settings import AgentMonitorSettings


def report(scope='session', state='working', **extra):
    return {'v': 1, 'type': 'program_status', 'scope': scope, 'status': {'state': state}, **extra}


def test_unencodable_session_does_not_block_later_sessions_or_destinations(tmp_path):
    received = []
    slow_started, fast_delivered, release = (threading.Event() for _ in range(3))
    class Receiver(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path.endswith('/slow'):
                slow_started.set()
                release.wait(3)
            else:
                fast_delivered.set()
            received.append((self.path, body))
            self.send_response(200); self.end_headers(); self.wfile.write(b'OK')
    http = ThreadingHTTPServer(('127.0.0.1', 0), Receiver)
    thread = threading.Thread(target=http.serve_forever, daemon=True); thread.start()
    state = ProgramState(publisher='test-installation')
    relay = ProgramRelay(state)
    socket = ProgramServer(state, status_socket_path(tmp_path)); socket.start()
    server = f'http://127.0.0.1:{http.server_port}'
    config = SimpleNamespace(server=server, receiver_channel=None, outbound_channel='slow')
    phone = SimpleNamespace(status_protocol=2, link_id='phone', platform='ios', server=server, token='fast')
    try:
        with patch('sidepulse.relay.load_relay_config', return_value=config), patch('sidepulse.links.load_phone_links', return_value=[phone]), patch('sidepulse.settings.load_settings', return_value=AgentMonitorSettings()):
            relay.sync()
            submit_report(report('\\' * 128, context={'provider': '"' * 128, 'session_id': '\\' * 512}, status={'state': 'working', 'title': '"' * 192, 'id': '/'.join(['a' * 32] * 3)}), path=socket.path)
            submit_report(report('good'), path=socket.path)
            relay.sync(wait=False)
            assert slow_started.wait(2) and fast_delivered.wait(2)
            # A blocked destination never stalls hook submission or the UI socket.
            submit_report(report('good', 'done'), path=socket.path)
            assert daemon_command({'type': 'get_state'}, path=socket.path)['ok']
            release.set(); relay.sync()
            relay.sync()
            good = [body for path, body in received if path.endswith('/slow')]
            assert good and all(body['scope'] == 'good' for body in good)
            assert good[-1]['status']['state'] == 'done'
            # Repeated updates replace one pending value, including before delivery.
            for _ in range(100): submit_report(report('good'), path=socket.path)
            assert len(state.pending_events('new-destination')) == 2
            assert len({body['scope'] for _, body in state.pending_events('new-destination')}) == 2
    finally:
        release.set(); relay.stop(); socket.stop(); http.shutdown(); http.server_close(); thread.join()


def test_transient_accept_failure_recovers_without_restarting_session_state(tmp_path):
    state = ProgramState(publisher='test-installation')
    server = ProgramServer(state, status_socket_path(tmp_path)); server.start()
    original = server.socket
    class Listener:
        failed = threading.Event()
        def accept(self):
            if not self.failed.is_set():
                self.failed.set()
                raise OSError(errno.ECONNABORTED, 'aborted client')
            return original.accept()
        def close(self): original.close()
    listener = Listener()
    server.socket = listener
    try:
        assert listener.failed.wait(2)
        submit_report(report(), path=server.path)
        assert server.thread.is_alive() and len(state.statuses()) == 1
    finally: server.stop()


def test_retry_after_and_retired_destinations_do_not_repeat_requests():
    import io
    import urllib.error
    from sidepulse.program_transport import BridgeFailure, publish_update
    value = {**report(), 'publisher': 'p', 'seq': 1, 'updated_at': 1}
    for code, retry, retired in [(429, 30, False), (410, 0, True)]:
        error = urllib.error.HTTPError('http://localhost/secret', code, 'no', {'Retry-After': str(retry)}, io.BytesIO())
        with patch('urllib.request.urlopen', side_effect=error):
            try: publish_update('http://127.0.0.1:1234', 'test-channel', value)
            except BridgeFailure as failure:
                assert failure.retry_after == retry and failure.retired == retired
                assert 'secret' not in str(failure)
            else: raise AssertionError('failure was acknowledged')
        state = ProgramState(publisher='test-installation')
        state.submit(report())
        relay = ProgramRelay(state)
        config = SimpleNamespace(server='http://127.0.0.1:1234', receiver_channel=None, outbound_channel='test-channel')
        try:
            with patch('sidepulse.relay.load_relay_config', return_value=config), patch('sidepulse.links.load_phone_links', return_value=[]), patch('sidepulse.settings.load_settings', return_value=AgentMonitorSettings()), patch('urllib.request.urlopen', side_effect=error) as request, patch('sidepulse.program_transport.time.monotonic', return_value=100):
                relay.sync() # The state submitted before configuration is not forwarded.
                state.submit(report())
                relay.sync(); relay.sync()
                state.submit(report(state='done')); relay.sync()
                assert request.call_count == 1
                with patch('sidepulse.program_transport.time.monotonic', return_value=131):
                    relay.sync()
                assert request.call_count == (1 if retired else 2)
        finally: relay.stop()


def test_session_link_survives_daemon_queries_and_matches_forwarded_identity(tmp_path):
    from urllib.parse import urlsplit, parse_qs
    state = ProgramState(publisher='source-installation')
    server = ProgramServer(state, status_socket_path(tmp_path)); server.start()
    try:
        for sender in ['local', 'remote']:
            value = {**report('release / + &?🙂'), 'publisher': state.publisher if sender == 'local' else 'other', 'seq': 1, 'updated_at': 1}
            state.receive_update(sender, value)
        rows = daemon_command({'type': 'get_state'}, path=server.path)['snapshot']['statuses']
        outgoing = {value['publisher'] for _, value in state.pending_events('phone')}
        assert {parse_qs(urlsplit(row['session_link']).query)['publisher'][0] for row in rows} == outgoing
        assert all(parse_qs(urlsplit(row['session_link']).query)['scope'] == ['release / + &?🙂'] for row in rows)
    finally: server.stop()


def test_reenabled_destination_skips_values_captured_before_it_was_enabled():
    state = ProgramState(publisher='test-installation')
    relay = ProgramRelay(state)
    config = SimpleNamespace(server='http://127.0.0.1:1', receiver_channel=None, outbound_channel='target')
    started, release = threading.Event(), threading.Event()
    sent = []
    def publish(server, channel, update, **kwargs):
        sent.append(update['scope'])
        if update['scope'] == 'in-flight':
            started.set()
            assert release.wait(3)
    try:
        with patch('sidepulse.relay.load_relay_config', return_value=config), patch('sidepulse.links.load_phone_links', return_value=[]), patch('sidepulse.settings.load_settings', return_value=AgentMonitorSettings()), patch('sidepulse.program_transport.publish_update', side_effect=publish):
            relay.sync()
            state.submit(report('in-flight')); state.submit(report('old-pending'))
            relay.sync(wait=False)
            assert started.wait(1)
            config.outbound_channel = None; relay.sync(wait=False)
            config.outbound_channel = 'target'; relay.sync(wait=False)
            state.submit(report('new-after-enable'))
            release.set(); relay.sync(); relay.sync()
            assert sent == ['in-flight', 'new-after-enable']
    finally:
        release.set(); relay.stop()


def test_first_hook_after_socket_starts_is_forwarded(tmp_path):
    from sidepulse.service import SidePulseService
    from sidepulse.relay import RelayConfig
    service = SidePulseService(poll_seconds=0.01)
    service.program_server.path = status_socket_path(tmp_path)
    original_start = service.program_server.start
    delivered = threading.Event()
    def start_and_submit_hook():
        original_start()
        submit_report(report('first-hook'), path=service.program_server.path)
    def poll_and_stop():
        try:
            assert delivered.wait(2), 'First hook was skipped when the relay initialized'
        finally:
            service.stop()
    config = RelayConfig(server='http://127.0.0.1:1', outbound_channel='target')
    with patch('sidepulse.relay.load_relay_config', return_value=config), patch('sidepulse.service.load_relay_config', return_value=config), patch('sidepulse.links.load_phone_links', return_value=[]), patch('sidepulse.settings.load_settings', return_value=AgentMonitorSettings()), patch('sidepulse.program_transport.publish_update', side_effect=lambda *args, **kwargs: delivered.set()), patch.object(service, 'install_signal_handlers'), patch.object(service.program_server, 'start', side_effect=start_and_submit_hook), patch.object(service.runtime, 'poll', side_effect=poll_and_stop), patch.object(service, 'sync_outputs'):
        assert service.run() == 0
    assert delivered.is_set()
