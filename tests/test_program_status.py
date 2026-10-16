import copy
import json
import socket
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sidepulse.program_status import ProgramStore, StatusError, validate_snapshot, apply_report
from sidepulse.program_transport import ProgramServer, submit_report, durable_submit, ProgramRelay
from sidepulse.program_adapter import hook_report
from sidepulse.program_presentation import ProgramMonitor

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/program-status.json').read_text())

@pytest.fixture
def store(tmp_path):
    s = ProgramStore(tmp_path / 'status.sqlite3')
    yield s
    s.close()

def report(state, **fields):
    return {'v':1,'type':'program_status','scope':'session','status':{'state':state,**fields}}

def test_shared_fixtures(store):
    for case in FIXTURE['cases']:
        store.db.execute('DELETE FROM snapshots');store.db.commit()
        for snapshot, expected in zip(case['snapshots'],case['expected']):
            store.receive('sender', snapshot)
            statuses = [s for s in store.statuses() if s.mode.value != 'completed' or s.updated_at.timestamp()*1000 >= FIXTURE['now']-1200000]
            from sidepulse.collector import aggregate_status
            from sidepulse.program_status import MODES
            representative = aggregate_status(tuple(statuses)).representative
            assert aggregate_status(tuple(statuses)).mode == MODES[expected],case['name']
        if representative:
            if 'expected_msg' in case: assert representative.message == case['expected_msg']
            if 'expected_app' in case: assert representative.provider == (case['expected_app'] or 'program')
    for case in FIXTURE['invalid']:
        with pytest.raises(StatusError): validate_snapshot(case['snapshot'])

def test_full_replacement_clear_subtree_and_session_end(store):
    store.submit(report('working',app='deploy',msg='hello',progress=30))
    store.submit(report('blocked',kind='permission'))
    assert store.local_snapshot()['scopes']['session']['records'][0]['status'] == {'state':'blocked','kind':'permission'}
    for key in ('a','a/b','ab','c'): store.submit(report('working',id=key))
    store.submit(report('clear',id='a'))
    assert {s['status'].get('id') for s in store.local_snapshot()['scopes']['session']['records']} == {None,'ab','c'}
    store.submit(report('done',id='finished'))
    store.submit({'v':1,'type':'scope_end','scope':'session'})
    assert [s['status']['state'] for s in store.local_snapshot()['scopes']['session']['records']] == ['done']
    store.submit(report('clear'))
    assert store.local_snapshot()['scopes'] == {}

def test_durable_idempotence_ordering_and_invalid_rollback(store):
    r={**report('working'),'request_id':'request-1'}
    a=store.submit(r);assert store.submit(r)==a
    with pytest.raises(StatusError):store.submit({**r,'status':{'state':'done'}})
    with pytest.raises(StatusError):store.submit({**report('error'),'seq':3})
    assert store.local_snapshot()['revision']==1
    path=store.path
    second=ProgramStore(path)
    try: assert second.submit(r)==a
    finally:second.close()
    store.submit(report('clear'))
    assert store.submit({**report('done'),'seq':3})['seq']==3

def test_many_completed_scopes_do_not_exhaust_live_scope_limit(store):
    for i in range(100):
        store.submit({**report('working'),'scope':f's-{i}'})
        store.submit({'v':1,'type':'scope_end','scope':f's-{i}'})
    assert store.local_snapshot()['scopes']=={}

def test_concurrent_local_reports_and_phone_forwarding(store):
    with ThreadPoolExecutor(max_workers=8) as pool:
        a=list(pool.map(lambda i:store.submit({**report('working'),'scope':f's-{i}'}),range(20)))
    assert sorted(x['revision'] for x in a)==list(range(1,21))
    remote=FIXTURE['cases'][0]['snapshots'][0]
    assert store.receive('remote',remote)
    assert not store.receive('remote',remote)
    assert len(store.phone_snapshot()['scopes'])==21

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

def test_no_daemon_submission_is_persisted(store):
    with patch('sidepulse.program_transport.submit_report',side_effect=FileNotFoundError),patch('sidepulse.program_transport.ProgramStore',return_value=store),patch.object(store,'close'):
        assert durable_submit(report('working'))['seq']==1

def test_hooks_scope_children_and_explicit_end():
    root=hook_report('claude',{'hook_event_name':'UserPromptSubmit','session_id':'abc'})
    child=hook_report('claude',{'hook_event_name':'PreToolUse','session_id':'abc','agent_id':'child','tool_name':'Bash'})
    assert root['status']['state']=='working'
    assert child['scope']==root['scope']
    assert child['status']['id'].startswith('agents/')
    from sidepulse.program_adapter import hook_reports
    ended = hook_reports('claude',{'hook_event_name':'SessionEnd','session_id':'abc'})
    assert ended[0]['status']['state'] == 'done'
    assert ended[-1]['type'] == 'scope_end'

def test_parallel_permission_waits_survive_other_tools(store):
    from sidepulse.program_adapter import hook_reports
    from sidepulse.collector import aggregate_status
    from sidepulse.models import AgentMode
    def emit(event, command=None):
        line={'hook_event_name':event,'session_id':'session','tool_name':'Bash','tool_input':{'command':command}}
        for r in hook_reports('claude',line):store.submit(r)
    emit('UserPromptSubmit')
    emit('PermissionRequest','first')
    emit('PermissionRequest','second')
    emit('PreToolUse','unrelated')
    emit('PostToolUse','unrelated')
    assert aggregate_status(store.statuses()).mode == AgentMode.WAITING_FOR_INPUT
    emit('PostToolUse','first')
    assert aggregate_status(store.statuses()).mode == AgentMode.WAITING_FOR_INPUT
    emit('PostToolUseFailure','second')
    assert aggregate_status(store.statuses()).mode == AgentMode.BLOCKED_ERROR
    emit('Stop')
    assert aggregate_status(store.statuses()).mode == AgentMode.COMPLETED
    before = store.local_snapshot()
    for r in hook_reports('claude',{'hook_event_name':'Notification','session_id':'session','message':'still running'}): store.submit(r)
    assert store.local_snapshot() == before

def test_clear_ordering_survives_process_restart_and_json_key_order(store):
    initial={**report('working'),'request_id':'same'}
    a=store.submit(initial)
    assert store.submit(dict(reversed(list(initial.items()))))==a
    store.submit(report('clear'))
    restarted=ProgramStore(store.path)
    try:
        with pytest.raises(StatusError):restarted.submit({**report('working'),'seq':1})
        assert restarted.submit({**report('working'),'seq':3})['seq']==3
    finally:restarted.close()

def test_explicit_states_bypass_legacy_staleness_and_count_sessions(store):
    from sidepulse.collector import MonitorSnapshot, aggregate_status
    from sidepulse.models import AgentMode
    store.receive('remote',FIXTURE['cases'][3]['snapshots'][0])
    legacy=MonitorSnapshot(aggregate_status(()),(),(),(),datetime.now(timezone.utc))
    monitor=ProgramMonitor(SimpleNamespace(snapshot=lambda **kw:legacy),store)
    result=monitor.snapshot()
    assert result.aggregate.mode==AgentMode.WAITING_FOR_INPUT
    assert result.aggregate.active_count==1
    store.receive('remote',dict(FIXTURE['cases'][0]['snapshots'][0],revision=2))
    assert monitor.snapshot().aggregate.mode==AgentMode.WORKING

def test_receiver_removal_clears_phone_snapshot(store):
    store.receive('remote',FIXTURE['cases'][0]['snapshots'][0])
    revision=store.phone_snapshot()['revision']
    store.retain_senders({'local'})
    assert store.phone_snapshot()['scopes']=={}
    assert store.phone_snapshot()['revision']>revision

def test_cross_connection_snapshot_revision_is_atomic(store):
    second=ProgramStore(store.path)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            future=pool.submit(lambda:[second.submit(report('working',progress=i%100)) for i in range(200)])
            for _ in range(200):
                snap=store.phone_snapshot()
                if snap['scopes']:
                    scope=next(iter(snap['scopes'].values()))
                    assert scope['seq']==snap['revision']
            future.result()
    finally:second.close()

def test_publisher_and_scope_identity_cannot_collide(store):
    a=copy.deepcopy(FIXTURE['cases'][0]['snapshots'][0]);a['publisher']='a:b';a['scopes']={'c':a['scopes']['session']}
    b=copy.deepcopy(a);b['publisher']='a';b['scopes']={'b:c':b['scopes']['c']}
    store.receive('sender',a);store.receive('sender',b)
    assert len({s.session_id for s in store.statuses()})==2
    assert len({s.agent_id for s in store.statuses()})==2

def test_cli_flags_json_ack_and_invalid_input(tmp_path):
    import os,subprocess,sys
    env=dict(os.environ,XDG_STATE_HOME=str(tmp_path/'state'))
    command=[sys.executable,'-c','from sidepulse.cli import sidepulse_main; raise SystemExit(sidepulse_main())']
    result=subprocess.run(command+['report','--scope','cli','--state','blocked','--kind','auth','--msg','Sign in'],env=env,capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)['seq']==1
    payload={'v':1,'type':'program_status','scope':'cli','status':{'state':'clear'}}
    result=subprocess.run(command+['report','--json'],input=json.dumps(payload),env=env,capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)['seq']==2
    result=subprocess.run(command+['report','--json'],input=json.dumps(dict(payload,v=True)),env=env,capture_output=True,text=True)
    assert result.returncode==1 and 'Unsupported protocol version' in result.stderr

def test_reordered_full_snapshots_converge_without_resurrecting_clears(store):
    import random
    from sidepulse.program_status import apply_report
    history=[]
    current={'v':1,'type':'program_status_snapshot','publisher':'p','revision':0,'scopes':{}}
    randomizer=random.Random(7501)
    for i in range(120):
        state=randomizer.choice(['working','blocked','done','error','idle','clear'])
        current=apply_report(current,{**report(state,id=randomizer.choice(['a','a/b','ab','c'])),'scope':randomizer.choice(['x','y','z'])},i)
        history.append(current)
    final=copy.deepcopy(current)
    randomizer.shuffle(history)
    for snapshot in history+history[:20]:store.receive('sender',snapshot)
    row=store.db.execute("SELECT body FROM snapshots WHERE sender='sender'").fetchone()
    assert json.loads(row[0])==final

def test_socket_rejects_bad_and_slow_clients_without_losing_valid_reports(store):
    import tempfile
    with tempfile.TemporaryDirectory(prefix='ps-',dir='/tmp') as directory:
        path=Path(directory)/'s';server=ProgramServer(store,path);server.start()
        try:
            for body in [b'[]',b'{"v":1,"type":"program_status","scope":"s","status":{"state":[]}}',b'{']:
                with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
                    client.settimeout(2);client.connect(str(path));client.sendall(body)
                    if body!=b'{':client.shutdown(socket.SHUT_WR)
                    assert json.loads(client.recv(4096))['ok'] is False
            assert submit_report(report('working'),path=path)['seq']==1
        finally:server.stop()

def test_shared_field_normalization():
    from sidepulse.program_status import validate_status
    for case in FIXTURE['normalization']:
        assert validate_status(case['input'])==case['expected']

def test_hook_batch_is_atomic_retryable_and_rolls_back_receipts(store):
    from sidepulse.program_adapter import hook_reports
    reports = hook_reports('claude', {'hook_event_name': 'Stop', 'session_id': 'atomic'})
    invalid = copy.deepcopy(reports)
    invalid[-1]['status']['state'] = 'invalid'
    batch = {'v': 1, 'type': 'program_status_batch', 'reports': invalid}
    with pytest.raises(StatusError):
        store.submit(batch)
    assert store.local_snapshot()['revision'] == 0
    assert store.db.execute('SELECT count(*) FROM receipts').fetchone()[0] == 0
    batch['reports'] = reports
    ack = store.submit(batch)
    assert store.submit(batch) == ack
    assert store.local_snapshot()['revision'] == 2
    assert len(store.statuses()) == 1
    assert store.statuses()[0].mode.value == 'completed'


def test_hook_batches_cannot_interleave(store):
    second = ProgramStore(store.path)
    def emit(db, n):
        return db.submit({'v': 1, 'type': 'program_status_batch', 'reports': [
            report('working', msg=str(n)), report('blocked', id='permission', msg=str(n))]})
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(emit, store if n % 2 else second, n) for n in range(50)]
            for future in futures: future.result()
        records = store.local_snapshot()['scopes']['session']['records']
        assert len({r['status']['msg'] for r in records}) == 1
        assert store.local_snapshot()['revision'] == 100
    finally:
        second.close()


def test_phone_capacity_does_not_block_outbound_relay(store):
    from sidepulse.settings import LED_DISPLAY_AGENT
    store.submit(report('working'))
    config = SimpleNamespace(server='https://example.com', outbound_channel='outbound-channel', receiver_channel=None)
    link = SimpleNamespace(status_protocol=1, link_id='phone', server='https://example.com', token='phone-token', platform='android')
    with patch('sidepulse.relay.load_relay_config', return_value=config), patch('sidepulse.links.load_phone_links', return_value=[link]), patch('sidepulse.settings.load_settings', return_value=SimpleNamespace(sync_sessions_to_phone=lambda _: True)), patch.object(store, 'phone_snapshot', side_effect=StatusError('Too many scopes')), patch('sidepulse.program_transport.publish_snapshot') as send:
        ProgramRelay(store).sync()
        send.assert_called_once_with(config.server, config.outbound_channel, store.local_snapshot(), phone=False)


def test_existing_stream_dispatches_status_and_legacy_messages(store):
    import io, threading
    from sidepulse.relay import RelayConfig, listen_for_relay_events, relay_event_message
    from unittest.mock import MagicMock
    stop = threading.Event()
    raw = relay_event_message('claude', {'hook_event_name': 'Stop', 'session_id': 'legacy'})
    messages = [{'type': 'program_status_snapshot'}, raw, FIXTURE['cases'][0]['snapshots'][0]]
    response = MagicMock()
    response.__enter__.return_value = io.BytesIO(b''.join(b'data: ' + json.dumps(m).encode() + b'\n\n' for m in messages))
    seen = []
    def receive(value):
        store.receive('remote', value)
        stop.set()
    with patch('urllib.request.urlopen', return_value=response):
        listen_for_relay_events(RelayConfig(server='https://example.com', receiver_channel='receiver-channel'), stop,
                               lambda *args: seen.append(args), receive)
    assert len(seen) == 1 and len(store.statuses()) == 1


@pytest.mark.parametrize('ack', [b'', b'{}', b'OK OTHER'])
def test_bridge_bad_ack_is_not_success(ack):
    from unittest.mock import MagicMock
    from sidepulse.program_transport import publish_snapshot
    response = MagicMock(); response.__enter__.return_value.read.return_value = ack
    with patch('urllib.request.urlopen', return_value=response), pytest.raises(StatusError):
        publish_snapshot('https://example.com', 'test-channel', FIXTURE['cases'][0]['snapshots'][0])


def test_phone_push_budget_waits_until_complete_snapshot_fits(store):
    from sidepulse.settings import AgentMonitorSettings
    from sidepulse.program_status import fits_phone_push
    config = SimpleNamespace(server='https://example.com', outbound_channel=None, receiver_channel=None)
    phone = SimpleNamespace(status_protocol=1, link_id='phone', platform='ios', server=config.server, token='test')
    relay = ProgramRelay(store)
    store.submit(report('working'))
    with patch('sidepulse.relay.load_relay_config', return_value=config), patch('sidepulse.links.load_phone_links', return_value=[phone]), patch('sidepulse.settings.load_settings', return_value=AgentMonitorSettings()), patch('sidepulse.program_transport.publish_snapshot') as send:
        relay.sync()
        send.assert_called_once()
        for i in range(3):
            store.submit(report('working', id=f'child-{i}', msg='x' * 1500))
        assert not fits_phone_push(store.phone_snapshot())
        relay.sync()
        assert send.call_count == 1
        store.submit(report('clear'))
        relay.sync()
        assert send.call_count == 2
        assert send.call_args.args[2]['scopes'] == {}


def test_relay_retries_failure_and_republishes_complete_snapshot(store):
    config = SimpleNamespace(server='https://example.com', outbound_channel='outbound-channel', receiver_channel=None)
    relay = ProgramRelay(store)
    store.submit(report('working'))
    with patch('sidepulse.relay.load_relay_config', return_value=config), patch('sidepulse.links.load_phone_links', return_value=[]), patch('sidepulse.program_transport.publish_snapshot') as send, patch('sidepulse.program_transport.time.monotonic', return_value=100):
        send.side_effect = StatusError('offline')
        relay.sync(); assert relay.sent == {}
        send.side_effect = None
        relay.sync(); relay.sync()
        assert send.call_count == 2
        with patch('sidepulse.program_transport.time.monotonic', return_value=161): relay.sync()
        assert send.call_count == 3
        assert send.call_args.args[2] == store.local_snapshot()


def test_batch_request_id_deduplicates_children_without_receipts(store):
    batch = {'v': 1, 'type': 'program_status_batch', 'request_id': 'batch-id', 'reports': [report('working'), report('done')]}
    first = store.submit(batch)
    assert store.submit(batch) == first
    assert store.local_snapshot()['revision'] == 2
    with pytest.raises(StatusError):
        store.submit({**batch, 'reports': [report('blocked')]})
    assert store.local_snapshot()['revision'] == 2


@pytest.mark.parametrize('unavailable', [False, True])
def test_cursor_entrypoint_submits_atomic_status_or_legacy_fallback(store, tmp_path, unavailable):
    import io
    from contextlib import redirect_stdout
    from sidepulse.cursor_hook import main
    payload = {'conversation_id': 'cursor-session', 'tool_name': 'shell', 'error_message': 'tool failed'}
    output = io.StringIO()
    with patch('sys.stdin', io.StringIO(json.dumps(payload))), redirect_stdout(output), patch('sidepulse.program_transport.durable_submit', side_effect=OSError('offline') if unavailable else lambda report, **kw: store.submit(report)) as submit, patch('sidepulse.cursor_hook.send_hook_event') as legacy, patch('sidepulse.cursor_hook.write_hook_status_audit'):
        assert main(['--event', 'postToolUseFailure', '--log', str(tmp_path / 'cursor.jsonl')]) == 0
    assert output.getvalue() == '{}\n'
    assert submit.call_args.args[0]['type'] == 'program_status_batch'
    assert legacy.called
    if not unavailable:
        assert len(store.statuses()) == 1
        assert store.statuses()[0].provider == 'cursor'
        assert store.statuses()[0].mode.value == 'blocked_error'


def test_identical_permission_arguments_keep_distinct_invocations(store):
    from sidepulse.program_adapter import hook_reports
    from sidepulse.collector import aggregate_status
    def emit(event, invocation=None):
        payload = {'hook_event_name': event, 'session_id': 'same-args', 'tool_name': 'Bash', 'tool_input': {'command': 'echo hello'}}
        if invocation: payload['tool_use_id'] = invocation
        store.submit({'v': 1, 'type': 'program_status_batch', 'reports': hook_reports('claude', payload)})
    emit('PermissionRequest', 'call-1'); emit('PermissionRequest', 'call-2')
    emit('PostToolUse', 'call-1')
    assert aggregate_status(store.statuses()).mode.value == 'waiting_for_input'
    emit('PostToolUse', 'call-2')
    assert aggregate_status(store.statuses()).mode.value == 'working'
    emit('PermissionRequest')
    emit('PostToolUse', 'call-3')  # Request omitted the ID, completion supplied it.
    assert aggregate_status(store.statuses()).mode.value == 'working'


@pytest.mark.parametrize('display', ['agent', 'battery', 'custom'])
@pytest.mark.parametrize('platform', ['ios', 'android'])
def test_phone_session_forwarding_ignores_mac_presentation(store, display, platform):
    from sidepulse.settings import AgentMonitorSettings, AgentAnimationSetting, AGENT_ANIMATION_SOLID_GREEN
    from sidepulse.program_transport import sender_identity
    settings = AgentMonitorSettings(led_display=display, agent_animations={
        'working': AgentAnimationSetting(style=AGENT_ANIMATION_SOLID_GREEN)})
    config = SimpleNamespace(server='https://example.com', outbound_channel=None, receiver_channel='remote-channel')
    phone = SimpleNamespace(status_protocol=1, link_id='phone', platform=platform, server=config.server, token='test')
    store.submit(report('working'))
    remote = {'v': 1, 'type': 'program_status_snapshot', 'publisher': 'remote-mac', 'revision': 1,
              'scopes': {'remote-session': {'seq': 1, 'records': [{'status': {'state': 'blocked', 'kind': 'permission'}, 'updated_at': 1}]}}}
    store.receive(sender_identity(config.server, config.receiver_channel), remote)
    with patch('sidepulse.relay.load_relay_config', return_value=config), patch('sidepulse.links.load_phone_links', return_value=[phone]), patch('sidepulse.settings.load_settings', return_value=settings), patch('sidepulse.program_transport.publish_snapshot') as send:
        ProgramRelay(store).sync()
        send.assert_called_once_with(config.server, 'apns_test' if platform == 'ios' else 'test', store.phone_snapshot(), phone=True)
        assert len(send.call_args.args[2]['scopes']) == 2


def test_phone_sync_toggle_is_per_phone_and_does_not_rewrite_status(store):
    from sidepulse.settings import AgentMonitorSettings
    config = SimpleNamespace(server='https://example.com', outbound_channel=None, receiver_channel=None)
    phones = [SimpleNamespace(status_protocol=protocol, link_id=name, platform='ios', server=config.server, token=name)
              for name, protocol in [('one', 1), ('two', 1), ('old', 0)]]
    store.submit(report('working'))
    before = store.phone_snapshot()
    relay = ProgramRelay(store)
    with patch('sidepulse.relay.load_relay_config', return_value=config), patch('sidepulse.links.load_phone_links', return_value=phones), patch('sidepulse.settings.load_settings', return_value=AgentMonitorSettings()) as settings, patch('sidepulse.program_transport.publish_snapshot') as send, patch('sidepulse.program_transport.time.monotonic', return_value=100):
        relay.sync()
        assert [c.args[1] for c in send.call_args_list] == ['apns_one', 'apns_two']
        settings.return_value = AgentMonitorSettings().with_phone_session_sync('one', False)
        relay.sync()
        assert send.call_count == 2
        settings.return_value = AgentMonitorSettings()
        relay.sync()
        assert send.call_count == 3
        assert send.call_args.args[1] == 'apns_one'
        assert store.phone_snapshot() == before  # No synthetic revision to override phone presentation.
        # A change while paused is delivered on resume; other phones continue updating.
        settings.return_value = AgentMonitorSettings().with_phone_session_sync('one', False)
        store.submit(report('done'))
        relay.sync()
        assert send.call_args.args[1] == 'apns_two'
        settings.return_value = AgentMonitorSettings()
        relay.sync()
        assert send.call_args.args[1] == 'apns_one'
        assert send.call_args.args[2] == store.phone_snapshot()
