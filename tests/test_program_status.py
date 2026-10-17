import io
import json
import socket
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest
from sidepulse.program_status import ProgramState, StatusError
from sidepulse.program_transport import ProgramServer, submit_report, submit_status, ProgramRelay, daemon_command
from sidepulse.program_adapter import hook_report
from sidepulse.program_presentation import ProgramMonitor, DaemonMonitor

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/program-updates.json').read_text())

@pytest.fixture
def store():
    return ProgramState()

def report(state, **fields):
    return {'v':1,'type':'program_status','scope':'session','status':{'state':state,**fields}}

def test_one_state_per_session_and_clear(store):
    store.submit(report('working',app='deploy',msg='hello',progress=30))
    store.submit(report('blocked',kind='permission'))
    assert store.pending_events('phone')[-1][1]['status'] == {'state':'blocked','kind':'permission'}
    for key in ('a','a/b','ab','c'): store.submit(report('working',id=key))
    store.submit(report('clear',id='a'))
    assert not store.statuses()
    store.submit(report('done',id='finished'))
    assert [s.mode.value for s in store.statuses()] == ['completed']
    store.submit(report('clear'))
    assert not store.statuses()


def test_acknowledge_done_through_socket_keeps_idle_and_does_not_publish(store):
    with tempfile.TemporaryDirectory(prefix='ps-', dir='/tmp') as directory:
        path = Path(directory) / 's'
        server = ProgramServer(store, path)
        server.start()
        try:
            completed = {**report('done', title='Build', msg='Finished'), 'request_id': 'completed-report'}
            submit_report(completed, path=path)
            monitor = DaemonMonitor(path)
            done = monitor.snapshot().statuses[0]
            events = store.pending_events('phone')
            command = {'type': 'acknowledge_session', 'agent_id': done.agent_id, 'seq': done.seq,
                       'updated_at': done.updated_at.isoformat()}
            assert daemon_command(command, path=path)['acknowledged']
            idle = monitor.snapshot().statuses[0]
            assert idle.agent_id == done.agent_id and idle.display_name == 'Build'
            assert idle.mode.value == 'idle_ready' and idle.message is None
            assert idle.seq == done.seq and idle.updated_at == done.updated_at
            assert store.pending_events('phone') == events
            submit_report(completed, path=path)
            assert monitor.snapshot().aggregate.mode.value == 'idle_ready'
            assert not daemon_command(command, path=path)['acknowledged']
            submit_report(report('done'), path=path)
            assert not daemon_command(command, path=path)['acknowledged']
            assert monitor.snapshot().aggregate.mode.value == 'completed'
            for bad in (None, True, '1', 0):
                with pytest.raises(StatusError):
                    daemon_command({**command, 'seq': bad}, path=path)
        finally:
            server.stop()


def test_acknowledgement_preserves_remote_guards_source_isolation_and_priority(store):
    event = dict(v=1, type='program_status', publisher='remote', scope='session', seq=10,
                 updated_at=int(datetime.now(timezone.utc).timestamp() * 1000), status={'state': 'done'})
    store.receive_update('a', event)
    done = store.statuses()[0]
    store.receive_update('b', {**event, 'status': {'state': 'blocked'}})
    assert store.acknowledge_session(done.agent_id, done.seq, done.updated_at.isoformat())
    assert not store.receive_update('a', event)
    assert not store.receive_update('a', {**event, 'seq': 9})
    snapshot = ProgramMonitor(store).snapshot()
    assert snapshot.aggregate.mode.value == 'waiting_for_input'
    assert snapshot.aggregate.active_count == 1
    assert sorted(s.mode.value for s in snapshot.statuses) == ['idle_ready', 'waiting_for_input']
    assert not store.acknowledge_session('missing', 10, done.updated_at.isoformat())
    store.receive_update('a', {**event, 'seq': 11, 'status': {'state': 'working'}})
    assert not store.acknowledge_session(done.agent_id, done.seq, done.updated_at.isoformat())
    assert sorted(s.mode.value for s in store.statuses()) == ['waiting_for_input', 'working']
    working = next(s for s in store.statuses() if s.agent_id == done.agent_id)
    assert not store.acknowledge_session(working.agent_id, working.seq, working.updated_at.isoformat())


def test_acknowledgement_does_not_refresh_capacity_recency(store):
    store.submit(report('done'))
    done = store.statuses()[0]
    for i in range(63):
        store.submit({**report('working'), 'scope': f's-{i}'})
    assert store.acknowledge_session(done.agent_id, done.seq, done.updated_at.isoformat())
    store.submit({**report('working'), 'scope': 'new'})
    assert len(store.statuses()) == 64
    assert done.agent_id not in {s.agent_id for s in store.statuses()}


def test_old_menu_cannot_acknowledge_reused_sequence_after_daemon_restart():
    from datetime import timedelta
    first = ProgramState(publisher='same-mac')
    first.submit(report('done'))
    old = first.statuses()[0]
    restarted = ProgramState(publisher='same-mac')
    with patch('sidepulse.program_status.datetime') as clock:
        clock.now.return_value = old.updated_at + timedelta(seconds=10)
        restarted.submit(report('done'))
    current = restarted.statuses()[0]
    assert (old.agent_id, old.seq) == (current.agent_id, current.seq)
    assert not restarted.acknowledge_session(old.agent_id, old.seq, old.updated_at.isoformat())
    assert restarted.statuses()[0].mode.value == 'completed'
    assert restarted.acknowledge_session(current.agent_id, current.seq, current.updated_at.isoformat())


def test_cleared_sessions_do_not_exhaust_live_scope_limit(store):
    for i in range(100):
        store.submit({**report('working'),'scope':f's-{i}'})
        store.submit({**report('clear'),'scope':f's-{i}'})
    assert not store.statuses()


def test_socket_ack_and_rejection(store,tmp_path):
    import tempfile
    directory=tempfile.TemporaryDirectory(prefix='ps-',dir='/tmp')
    path=Path(directory.name)/'s';server=ProgramServer(store,path);server.start()
    try:
        assert path.stat().st_mode & 0o777 == 0o600
        assert submit_report(report('working'),path=path)['seq']==1
        with pytest.raises(StatusError):submit_report(report('unknown'),path=path)
        assert submit_report(report('done'),path=path)['seq']==2
    finally:
        server.stop()
        directory.cleanup()


def test_hooks_scope_children_and_explicit_end():
    root=hook_report('claude',{'hook_event_name':'UserPromptSubmit','session_id':'abc'})
    child=hook_report('claude',{'hook_event_name':'PreToolUse','session_id':'abc','agent_id':'child','tool_name':'Bash'})
    assert root['status']['state']=='working'
    assert child['scope']==root['scope']
    assert child['status']['id'].startswith('agents/')
    ended = hook_report('claude',{'hook_event_name':'SessionEnd','session_id':'abc'})
    assert ended['status']['state'] == 'done'
    assert ended['type'] == 'program_status'


def test_latest_hook_replaces_permission_state(store):
    from sidepulse.collector import aggregate_status
    from sidepulse.models import AgentMode
    def emit(event, command=None):
        line={'hook_event_name':event,'session_id':'session','tool_name':'Bash','tool_input':{'command':command}}
        store.submit(hook_report('claude',line))
    emit('UserPromptSubmit')
    emit('PermissionRequest','first')
    emit('PermissionRequest','second')
    emit('PreToolUse','unrelated')
    emit('PostToolUse','unrelated')
    assert aggregate_status(store.statuses()).mode == AgentMode.WORKING
    emit('PostToolUse','first')
    assert aggregate_status(store.statuses()).mode == AgentMode.WORKING
    emit('PostToolUseFailure','second')
    assert aggregate_status(store.statuses()).mode == AgentMode.BLOCKED_ERROR
    emit('Stop')
    assert aggregate_status(store.statuses()).mode == AgentMode.COMPLETED
    before = store.statuses(), store.pending_events('phone')
    store.submit(hook_report('claude',{'hook_event_name':'Notification','session_id':'session','message':'still running'}))
    assert (store.statuses(), store.pending_events('phone')) == before


def test_socket_rejects_bad_and_slow_clients_without_losing_valid_reports(store):
    import tempfile
    with tempfile.TemporaryDirectory(prefix='ps-',dir='/tmp') as directory:
        path=Path(directory)/'s';server=ProgramServer(store,path);server.start()
        try:
            for body in [b'[]',b'{"v":1,"type":"program_status","scope":"s","status":{"state":[]}}',
                         *[json.dumps({**report('working'), 'type':kind, 'reports':[report('working')]}).encode()
                           for kind in ('program_status_batch', 'scope_end', 'scope_reset')], b'{']:
                with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
                    client.settimeout(2);client.connect(str(path));client.sendall(body)
                    if body!=b'{':client.shutdown(socket.SHUT_WR)
                    assert json.loads(client.recv(4096))['ok'] is False
            assert submit_report(report('working'),path=path)['seq']==1
        finally:server.stop()


def test_shared_update_validation_and_field_normalization():
    from sidepulse.program_status import validate_status
    for case in FIXTURE['normalization']:
        assert validate_status(case['input'])==case['expected']
    store = ProgramState()
    store.submit(report('working'))
    before = store.statuses(), store.pending_events('phone')
    for case in FIXTURE['invalid']:
        with pytest.raises(StatusError):
            store.receive_update('sender', case['update'])
        assert (store.statuses(), store.pending_events('phone')) == before


def test_stream_accepts_only_individual_osc_json_updates(store):
    import io, threading
    from sidepulse.relay import RelayConfig, listen_for_status_updates
    from unittest.mock import MagicMock
    stop = threading.Event()
    update = dict(v=1, type='program_status', publisher='remote', scope='session', seq=1,
                  updated_at=2000000, status={'state':'working'})
    messages = [{'type': 'agent_event', 'provider':'claude', 'line':{}},
                {'v':1,'type':'program_status_snapshot','publisher':'old','revision':1,'scopes':{}}, update]
    response = MagicMock()
    response.__enter__.return_value = io.BytesIO(b''.join(b'data: ' + json.dumps(m).encode() + b'\n\n' for m in messages))
    seen = []
    def receive(value):
        seen.append(value); store.receive_update('remote', value); stop.set()
    with patch('urllib.request.urlopen', return_value=response):
        listen_for_status_updates(RelayConfig(server='https://example.com', receiver_channel='receiver-channel'), stop, receive)
    assert seen == [update] and len(store.statuses()) == 1


@pytest.mark.parametrize('ack', [b'', b'{}', b'OK OTHER'])
def test_bridge_bad_ack_is_not_success(ack):
    from unittest.mock import MagicMock
    from sidepulse.program_transport import publish_update
    response = MagicMock(); response.__enter__.return_value.read.return_value = ack
    with patch('urllib.request.urlopen', return_value=response), pytest.raises(StatusError):
        publish_update('https://example.com', 'test-channel', FIXTURE['cases'][0]['updates'][0][0])


def test_phone_delivery_is_independent_of_ledger_size(store):
    from sidepulse.settings import AgentMonitorSettings
    from sidepulse.program_status import fits_phone_push
    config = SimpleNamespace(server='https://example.com', outbound_channel=None, receiver_channel=None)
    phone = SimpleNamespace(status_protocol=2, link_id='phone', platform='ios', server=config.server, token='test')
    relay = ProgramRelay(store)
    for i in range(100):
        store.submit({**report('working', msg='🟢' * 500), 'scope': f'session-{i}'})
    with patch('sidepulse.relay.load_relay_config', return_value=config), patch('sidepulse.links.load_phone_links', return_value=[phone]), patch('sidepulse.settings.load_settings', return_value=AgentMonitorSettings()), patch('sidepulse.program_transport.publish_update') as send:
        relay.sync()
        assert send.call_count == 100
        assert all(fits_phone_push(c.args[2]) and 'scopes' not in c.args[2] for c in send.call_args_list)
        assert len(store.statuses()) == 64
        store.submit(report('clear'))
        relay.sync()
        assert send.call_args.args[2]['status']['state'] == 'clear'


@pytest.mark.parametrize('unavailable', [False, True])
def test_cursor_entrypoint_only_submits_individual_osc_json(store, tmp_path, unavailable):
    import io
    from contextlib import redirect_stdout
    from sidepulse.cursor_hook import main
    payload = {'conversation_id': 'cursor-session', 'tool_name': 'shell', 'error_message': 'tool failed'}
    output = io.StringIO()
    with patch('sys.stdin', io.StringIO(json.dumps(payload))), redirect_stdout(output), patch('sidepulse.program_transport.submit_status', side_effect=OSError('offline') if unavailable else lambda report, **kw: store.submit(report)) as submit, patch('sidepulse.cursor_hook.write_hook_status_audit'):
        assert main(['--event', 'postToolUseFailure', '--log', str(tmp_path / 'cursor.jsonl')]) == 0
    assert output.getvalue() == '{}\n'
    assert submit.call_args.args[0]['type'] == 'program_status'
    import sidepulse.cursor_hook as cursor
    assert not hasattr(cursor, "send_hook_event")
    if not unavailable:
        assert len(store.statuses()) == 1
        assert store.statuses()[0].provider == 'cursor'
        assert store.statuses()[0].mode.value == 'blocked_error'


def test_permission_invocations_replace_one_session_state(store):
    from sidepulse.collector import aggregate_status
    def emit(event, invocation=None):
        payload = {'hook_event_name': event, 'session_id': 'same-args', 'tool_name': 'Bash', 'tool_input': {'command': 'echo hello'}}
        if invocation: payload['tool_use_id'] = invocation
        store.submit(hook_report('claude', payload))
    emit('PermissionRequest', 'call-1'); emit('PermissionRequest', 'call-2')
    emit('PostToolUse', 'call-1')
    assert aggregate_status(store.statuses()).mode.value == 'working'
    emit('PostToolUse', 'call-2')
    assert aggregate_status(store.statuses()).mode.value == 'working'
    emit('PermissionRequest')
    emit('PostToolUse', 'call-3')  # Request omitted the ID, completion supplied it.
    assert aggregate_status(store.statuses()).mode.value == 'working'
