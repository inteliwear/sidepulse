"""The daemon owns live state and physical output; clients use its socket."""
import os
import copy
import json
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, Mock

import pytest
from sidepulse.models import AgentMode
from sidepulse.program_status import ProgramState, StatusError, validate_update, fits_phone_push, phone_update
from sidepulse.program_transport import (ProgramServer, ProgramRelay, submit_report, submit_status,
    daemon_command, sender_identity, write_daemon_program)
from sidepulse.program_presentation import DaemonMonitor, ProgramMonitor
from sidepulse.service import SidePulseService
from sidepulse.settings import AgentMonitorSettings
from sidepulse.device_writer import DeviceCandidate


def report(scope='session', state='working', **fields):
    return {'v':1, 'type':'program_status', 'scope':scope, 'status':{'state':state, **fields}}


def update(scope='session', state='working', seq=1, publisher='remote', **fields):
    return {**report(scope, state, **fields), 'publisher':publisher, 'seq':seq, 'updated_at':1000}


@pytest.fixture
def daemon():
    service = SidePulseService()
    with tempfile.TemporaryDirectory(prefix='sp-', dir=None if os.name == 'nt' else '/tmp') as directory:
        server = ProgramServer(service.program_state, Path(directory) / 'status.sock', on_command=service.handle_command)
        server.start()
        try:
            yield service, server
        finally:
            server.stop()


def test_state_never_opens_database_or_state_files():
    with patch('sqlite3.connect', side_effect=AssertionError('database opened')), patch.object(Path, 'open', side_effect=AssertionError('state file opened')):
        state = ProgramState(publisher='mac')
        state.submit(report())
        assert ProgramMonitor(state).snapshot().aggregate.mode == AgentMode.WORKING
        state.receive_update('other', update(state='blocked'))
        assert len(state.pending_events('phone')) == 2
        assert not hasattr(state, 'db')


def test_daemon_restart_is_fresh_but_next_wire_sequence_is_newer():
    first = ProgramState()
    first.submit(report())
    old = first.pending_events('phone')[0][1]
    second = ProgramState()
    assert second.publisher == first.publisher
    assert not second.statuses() and not second.pending_events('phone')
    second.submit(report(state='blocked'))
    new = second.pending_events('phone')[0][1]
    assert new['publisher'] == old['publisher']
    assert old['seq'] < new['seq'] <= 9007199254740991


def test_multiple_sources_replay_guards_and_lru_share_one_limit():
    state = ProgramState()
    for i in range(64): assert state.receive_update(f'sender-{i}', update())
    assert state.receive_update('sender-0', {**update(seq=2), 'updated_at':1})
    assert not state.receive_update('sender-1', update())
    assert state.receive_update('new-source', update())
    sources = {entry.origin for entry in state.statuses()}
    assert len(sources) == 64 and 'sender-0' in sources and 'sender-1' not in sources
    assert not state.receive_update('sender-1', update())
    assert state.receive_update('sender-1', update(seq=2))
    sources = {entry.origin for entry in state.statuses()}
    assert 'sender-1' in sources and 'sender-2' not in sources
    assert state.receive_update('sender-1', update(state='clear',seq=3))
    assert not state.receive_update('sender-1', update(seq=2))
    assert len(state.statuses()) == 63


def test_active_count_keeps_sessions_from_distinct_sources_separate(daemon):
    service, server = daemon
    event = {**update(), 'context': {'provider':'claude', 'session_id':'same-name', 'origin':'VS Code'}}
    for sender in ('computer-a', 'computer-b'):
        service.program_state.receive_update(sender, event)
    view = DaemonMonitor(server.path).snapshot()
    assert len(view.statuses) == view.aggregate.active_count == 2
    service.program_state.receive_update('computer-a', {**event, 'seq':2, 'status':{'state':'done'}})
    view = DaemonMonitor(server.path).snapshot()
    assert len(view.statuses) == view.aggregate.active_count == 1
    service.program_state.receive_update('computer-b', {**event, 'seq':2, 'status':{'state':'clear'}})
    assert DaemonMonitor(server.path).snapshot().aggregate.active_count == 0


def test_rejected_update_preserves_state_and_retry_receipts():
    state = ProgramState()
    for i in range(64): state.submit(report(f's-{i}'))
    before = state.statuses(), state.pending_events('phone')
    event = {**report('new', state='invalid'), 'request_id':'retry'}
    with pytest.raises(StatusError): state.submit(event)
    assert (state.statuses(), state.pending_events('phone')) == before
    event['status']['state'] = 'working'
    ack = state.submit(event)
    assert state.submit(copy.deepcopy(event)) == ack
    with pytest.raises(StatusError, match='request_id reused'):
        state.submit({**event, 'status':{'state':'done'}})
    assert len(state.pending_events('phone')) == 64
    assert 's-0' not in {s.display_name for s in state.statuses()}
    assert ack['seq'] == 1


def test_concurrent_hook_updates_remain_ordered_and_complete():
    state = ProgramState()
    def submit(i):
        ack = state.submit(report(state='blocked' if i % 2 else 'working', msg=str(i)))
        statuses = state.statuses()
        assert len(statuses) == 1
        status = statuses[0]
        assert status.mode == (AgentMode.WAITING_FOR_INPUT if int(status.message) % 2 else AgentMode.WORKING)
        return ack['seq']
    with ThreadPoolExecutor(max_workers=8) as pool:
        sequences = list(pool.map(submit, range(100)))
    assert sorted(sequences) == list(range(1, 101))
    events = state.pending_events('phone')
    assert len(events) == 1
    assert [event['status']['msg'] for _, event in events] == [
        str(max(range(100), key=lambda i: sequences[i]))]


def test_ui_reconnect_reads_daemon_without_owning_or_reconstructing_state(daemon):
    service, server = daemon
    first_ui = DaemonMonitor(server.path)
    assert first_ui.snapshot().statuses == ()
    submit_report(report(),path=server.path)
    assert first_ui.snapshot().aggregate.mode == AgentMode.WORKING
    second_ui = DaemonMonitor(server.path)
    assert second_ui.snapshot().statuses == first_ui.snapshot().statuses
    assert not hasattr(second_ui,'store')
    assert not hasattr(second_ui,'program_state')
    assert len(service.program_state.statuses()) == 1
    server.stop()
    with pytest.raises(StatusError,match='daemon unavailable'):
        second_ui.snapshot()


@pytest.mark.parametrize('unicode_text',[False,True])
def test_state_query_can_return_64_long_session_rows(daemon,unicode_text):
    service, server = daemon
    message = '🟢'*512 if unicode_text else 'x'*2048
    for i in range(64):
        body=report(f's-{i}',msg=message)
        if unicode_text: body['context']={'cwd':('/'+'🟢'*50)*20}
        service.program_state.submit(body)
    result = DaemonMonitor(server.path).snapshot()
    assert len(result.statuses) == 64
    assert all(s.message == message for s in result.statuses)


def test_stalled_ui_client_does_not_block_fast_hooks(daemon, program_client):
    import socket
    service, server = daemon
    with program_client(server.path) as stalled:
        stalled.sendall(b'{')
        assert submit_report(report(),path=server.path,timeout=.2)['seq'] == 1
    assert service.program_state.statuses()[0].mode == AgentMode.WORKING


def test_no_daemon_means_no_hook_side_state_or_fallback(tmp_path):
    missing = tmp_path/'missing.sock'
    with patch('sidepulse.program_transport.status_socket_path',return_value=missing):
        with pytest.raises(StatusError,match='daemon unavailable'):
            submit_status(report())
    assert list(tmp_path.iterdir()) == []


def test_daemon_drives_physical_led_while_ui_is_connected(daemon, tmp_path):
    service, server = daemon
    target = tmp_path/'LEDS.LED'
    submit_report(report(),path=server.path)
    assert DaemonMonitor(server.path).snapshot().aggregate.mode == AgentMode.WORKING
    with patch('sidepulse.daemon_runtime.discover_devices',return_value=[DeviceCandidate(tmp_path,target,'test')]), patch('sidepulse.service.load_settings',return_value=AgentMonitorSettings()), patch('sidepulse.service.write_led_program',return_value=target) as write:
        service.runtime.devices = [DeviceCandidate(tmp_path,target,'test')]
        service.sync_outputs()
        write.assert_called_once()
        assert write.call_args.kwargs['device_path'] == target
        service.sync_outputs()
        assert write.call_count == 1


def test_ui_led_writes_and_holds_are_executed_by_daemon(daemon,tmp_path):
    service, server = daemon
    target = tmp_path/'LEDS.LED'
    with patch('sidepulse.program_transport.status_socket_path',return_value=server.path), patch('sidepulse.service.write_led_program',return_value=target) as write:
        assert write_daemon_program('#FF0000',device_path=target,hold_seconds=10) == target
    write.assert_called_once_with('#FF0000',device_path=target)
    assert service.manual_pause_until > time.monotonic()
    with patch('sidepulse.daemon_runtime.discover_devices') as discover:
        service.sync_outputs()
    discover.assert_not_called()


def test_ui_output_controls_do_not_replace_session_state(daemon):
    service, server = daemon
    submit_report(report(),path=server.path)
    daemon_command({'type':'output_control','enabled':False,'pause_seconds':5},path=server.path)
    assert not service.outputs_enabled
    assert DaemonMonitor(server.path).snapshot().aggregate.mode == AgentMode.WORKING
    daemon_command({'type':'output_control','enabled':True},path=server.path)
    assert service.outputs_enabled
    with pytest.raises(StatusError):
        daemon_command({'type':'output_control','enabled':False,'pause_seconds':-1},path=server.path)
    assert service.outputs_enabled  # Invalid commands are atomic.


@pytest.mark.parametrize('use_volume', [False, True])
def test_manual_write_restores_automatic_output_after_hold(daemon, tmp_path, use_volume):
    service, _ = daemon
    target = tmp_path / 'LEDS.LED'
    service.runtime.devices = [DeviceCandidate(tmp_path, target, 'test', 'dot')]
    service.program_state.submit(report())
    with patch('sidepulse.service.load_settings', return_value=AgentMonitorSettings()), \
         patch('sidepulse.service.write_led_program', return_value=target) as write:
        service.sync_outputs()
        automatic = write.call_args.args[0]
        service.handle_command({'v':1, 'type':'write_led', 'program':'#FF0000',
                                'target':str(tmp_path if use_volume else target), 'hold_seconds':0})
        service.sync_outputs()
    assert write.call_count == 3
    assert write.call_args.args[0] == automatic


def test_automatic_write_failure_is_reported_and_recovery_clears_it(daemon, tmp_path):
    service, server = daemon
    target = tmp_path / 'LEDS.LED'
    service.runtime.devices = [DeviceCandidate(tmp_path, target, 'test', 'dot')]
    service.program_state.submit(report())
    monitor = DaemonMonitor(server.path)
    with patch('sidepulse.service.load_settings', return_value=AgentMonitorSettings()), \
         patch('sidepulse.service.write_led_program', side_effect=[OSError('Drive unavailable'), target]) as write:
        service.sync_outputs()
        assert monitor.snapshot().aggregate.mode == AgentMode.WORKING
        assert monitor.output_errors == {'dot':'Drive unavailable'}
        service.sync_outputs()
        assert monitor.snapshot().aggregate.mode == AgentMode.WORKING
        assert monitor.output_errors == {}
        service.sync_outputs()
        assert write.call_count == 2


@pytest.mark.parametrize('command', [
    {'type':'write_led', 'program':'#FF0000'},
    {'type':'output_control', 'enabled':False},
])
def test_successful_explicit_output_clears_only_its_device_error(daemon, tmp_path, command):
    service, _ = daemon
    target = tmp_path / 'LEDS.LED'
    service.runtime.devices = [DeviceCandidate(tmp_path, target, 'test', 'dot')]
    service.output_errors = {'dot':'Previous write failed', 'other':'Still failed'}
    with patch('sidepulse.service.write_led_program', return_value=target):
        service.handle_command({'v':1, 'target':str(tmp_path), **command})
    assert service.output_errors == {'other':'Still failed'}


@pytest.mark.parametrize('platform',['ios','android'])
def test_daemon_forwards_individual_local_and_remote_osc_updates(platform):
    from sidepulse.relay import RelayConfig
    config = RelayConfig('https://example.test',receiver_channel='receiver-channel',outbound_channel='outbound-channel')
    state = ProgramState()
    phone = SimpleNamespace(status_protocol=2,link_id='phone',platform=platform,server=config.server,token='fake-phone')
    relay = ProgramRelay(state)
    with patch('sidepulse.relay.load_relay_config',return_value=config), patch('sidepulse.links.load_phone_links',return_value=[phone]), patch('sidepulse.settings.load_settings',return_value=AgentMonitorSettings()), patch('sidepulse.program_transport.publish_update') as publish:
        relay.sync()
        state.submit(report(msg='🟢'*500))
        state.receive_update(sender_identity(config.server,config.receiver_channel),update(state='blocked'))
        relay.sync()
    desktop = [c.args[2] for c in publish.call_args_list if not c.kwargs['phone']]
    mobile = [c.args[2] for c in publish.call_args_list if c.kwargs['phone']]
    assert len(desktop) == 1 and len(mobile) == 2
    assert len({u['publisher'] for u in mobile}) == 2
    assert all(validate_update(u) == u and fits_phone_push(phone_update(u)) and 'scopes' not in u for u in mobile)


def test_new_phone_and_reenabled_forwarding_never_replay_stored_updates():
    from sidepulse.relay import RelayConfig
    state = ProgramState(); state.submit(report())
    relay = ProgramRelay(state)
    phones=[SimpleNamespace(status_protocol=2,link_id=name,platform='ios',server='https://example.test',token=name) for name in ['one','two']]
    with patch('sidepulse.relay.load_relay_config',return_value=RelayConfig('https://example.test')), patch('sidepulse.links.load_phone_links',return_value=phones), patch('sidepulse.settings.load_settings',return_value=AgentMonitorSettings()) as settings, patch('sidepulse.program_transport.publish_update') as publish:
        relay.sync(); assert publish.call_count == 0 # Ignore the earlier stored Working.
        state.submit(report())
        relay.sync(); assert publish.call_count == 2
        settings.return_value=AgentMonitorSettings().with_phone_session_sync('one',False)
        relay.sync(); assert publish.call_count == 2
        state.submit(report(state='done'))
        relay.sync(); assert publish.call_count == 3
        settings.return_value=AgentMonitorSettings()
        relay.sync(); assert publish.call_count == 3 # Do not replay the Done from while it was off.
        state.submit(report())
        relay.sync(); assert publish.call_count == 5
        assert {call.args[1] for call in publish.call_args_list[-2:]} == {'apns_one', 'apns_two'}
        relay.sync(); assert publish.call_count == 5


@pytest.mark.parametrize('case',json.loads((Path(__file__).parent/'fixtures/program-updates.json').read_text())['cases'],ids=lambda c:c['name'])
def test_shared_mobile_accounting_fixtures(case):
    from datetime import datetime, timezone
    state = ProgramState()
    names = {AgentMode.WORKING:'working', AgentMode.WAITING_FOR_INPUT:'blocked', AgentMode.BLOCKED_ERROR:'error',
             AgentMode.COMPLETED:'done', AgentMode.IDLE_READY:'idle'}
    for updates, expected in zip(case['updates'], case['expected']):
        for update in updates:
            state.receive_update('source', update)
        # Fixture timestamps share the same fixed clock as both mobile suites.
        with patch('datetime.datetime') as clock:
            clock.now.return_value = datetime.fromtimestamp(2000, timezone.utc)
            assert names[ProgramMonitor(state).snapshot().aggregate.mode] == expected


def test_status_cli_reports_stopped_daemon_without_traceback(tmp_path,capsys):
    from sidepulse.cli import sidepulse_main
    with patch('sidepulse.program_transport.status_socket_path',return_value=tmp_path/'absent.sock'):
        assert sidepulse_main(['status','--json']) == 1
    assert 'daemon unavailable' in capsys.readouterr().err.lower()


def test_daemon_retries_a_temporary_socket_start_failure():
    from sidepulse.relay import RelayConfig
    service=SidePulseService()
    with patch.object(service,'install_signal_handlers'), patch.object(service.program_server,'start',side_effect=[OSError('busy'),setattr(service.program_server, 'running', True)]) as start, patch.object(service.program_server,'stop'), patch.object(service.program_relay,'start'), patch.object(service.program_relay,'stop'), patch.object(service,'sync_receiver'), patch.object(service.runtime,'start'), patch.object(service.runtime,'poll'), patch.object(service.runtime,'close'), patch.object(service,'sync_outputs',side_effect=service.stop), patch.object(service.stop_event,'wait'), patch('sidepulse.service.load_relay_config',return_value=RelayConfig('https://example.test')):
        assert service.run() == 0
    assert start.call_count == 2


def test_forwarded_status_is_complete_and_explicit_clear_removes_session():
    state = ProgramState(publisher='mac')
    state.submit({**report(), 'context':{'title':'Checkout', 'session_id':'checkout'}})
    state.submit({**report(state='blocked',id='permission/one'), 'context':{'session_id':'checkout'}})
    event = state.pending_events('phone')[-1][1]
    from sidepulse.program_status import phone_update
    assert phone_update(event)['status']['title'] == 'Checkout'
    state.submit(report(state='done'))
    event = state.pending_events('fresh-phone')[-1][1]
    assert event['type'] == 'program_status' and event['status']['state'] == 'done'
    receiver = ProgramState()
    receiver.receive_update('sender',event)
    assert receiver.statuses()[0].mode == AgentMode.COMPLETED
    state.submit(report())
    state.submit(report(state='clear'))
    cleared = state.pending_events('fresh-phone')[-1][1]
    assert cleared['type'] == 'program_status' and cleared['status'] == {'state':'clear'}
    receiver.receive_update('sender',cleared)
    assert receiver.statuses() == ()


def test_sender_removal_forwards_clear_without_overwriting_it_with_previous_status():
    state = ProgramState()
    state.submit(report('local'))
    state.receive_update('removed', update('remote', state='working'))
    state.receive_update('retained', update('remote', state='error'))
    receiver = ProgramState()
    events = state.pending_events('phone')
    for _, event in events:
        receiver.receive_update('desktop', event)
    state.acknowledge_event('phone', events[-1][0])
    removed_publisher = events[1][1]['publisher']
    # The offline phone can still show a session the daemon already evicted.
    for i in range(64):
        state.submit(report(f'other-{i}'))
    assert all(item.origin != 'removed' for item in state.statuses())
    state.acknowledge_event('phone', state.pending_events('phone')[-1][0])
    state.retain_senders({'local', 'retained'})
    clears = state.pending_events('phone')
    assert len(clears) == 1
    clear = clears[0][1]
    assert clear['type'] == 'program_status' and clear['status'] == {'state':'clear'}
    assert clear['publisher'] == removed_publisher
    receiver.receive_update('desktop', clear)
    assert sorted(item.mode.value for item in receiver.statuses()) == ['blocked_error', 'working']
    assert not receiver.receive_update('desktop', events[1][1])
