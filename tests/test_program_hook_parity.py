"""Existing hook behavior must survive migration to normalized accounting.

The existing collector is the baseline interpreter. Deliberate OSC differences
(no inferred completion from silence, explicit lifecycle, receiver-owned rendering)
are separate from hook transport, metadata, and interpretation compatibility.
"""
import os
import io
import json
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from sidepulse.collector import LiveAgentMonitor
from sidepulse.models import AgentMode
from sidepulse.program_adapter import hook_report
from sidepulse.program_presentation import ProgramMonitor
from sidepulse.program_status import ProgramState
from sidepulse.providers import parse_log_line


@pytest.fixture
def runtime(tmp_path):
    store = ProgramState()
    legacy = LiveAgentMonitor(sources=(), recovery_sources=(), latest_state_path=None)
    monitor = ProgramMonitor(store)
    yield store, legacy, monitor
    store.close()


def emit(runtime, event, provider='claude', **fields):
    store, legacy, monitor = runtime
    line = {'hook_event_name': event, 'session_id': 'session-12345678',
            'logged_at': datetime.now(timezone.utc).isoformat(), **fields}
    report = hook_report(provider, line)
    if report is not None:
        store.submit(report)
    legacy.ingest_record(parse_log_line(provider, json.dumps(line)))
    return monitor.snapshot(), legacy.snapshot()


@pytest.mark.parametrize('provider', ['claude', 'codex', 'grok', 'cursor', 'junie'])
def test_hook_sequences_match_existing_interpreter_and_preserve_metadata(runtime, provider):
    events = [
        ('UserPromptSubmit', {'prompt': 'Repair the widget', 'cwd': '/work/widget', 'agent_origin': 'VS Code'}),
        ('PreToolUse', {'tool_name': 'Bash', 'tool_input': {'command': 'test'}}),
        ('PostToolUse', {'tool_name': 'Bash', 'tool_response': {'exit_code': 1}}),
        ('UserPromptSubmit', {'prompt': 'Continue'}),
        ('PostToolUseFailure', {'message': 'Tool failed'}),
        ('PostToolUse', {'sidepulse_status': 'waiting_for_input'}),
        ('PostToolUse', {'sidepulse_mode': 'long_task_progress'}),
        ('PermissionDenied', {'message': 'Permission denied'}),
        ('Notification', {'notification_type': 'permission_prompt', 'message': 'Permission required'}),
        ('UserPromptSubmit', {}),
        ('Notification', {'notification_type': 'idle_prompt', 'message': 'Task completed'}),
        ('Notification', {'message': 'Still working'}),  # Cannot revive Done.
        ('UserPromptSubmit', {}),
        ('Stop', {'last_assistant_message': 'Which option should I use?'}),
        ('Interrupt', {}),
        ('SessionEnd', {}),
    ]
    for event, fields in events:
        actual, expected = emit(runtime, event, provider, **fields)
        # Idle visibility in the legacy UI is separately time-based.
        if event == 'Interrupt':
            assert actual.aggregate.mode == AgentMode.IDLE_READY
            continue
        assert actual.aggregate.mode == expected.aggregate.mode, (provider, event)
        assert len(actual.statuses) == len(expected.statuses) == 1, (provider, event)
        a, e = actual.statuses[0], expected.statuses[0]
        for field in ('provider', 'session_id', 'origin', 'display_name'):
            assert getattr(a, field) == getattr(e, field), (provider, event, field)
        assert a.cwd == '/work/widget'  # Sparse JSON updates retain session-opening context.


def test_metadata_survives_sparse_events_without_running_legacy_monitor(runtime):
    store, _, _ = runtime
    for event, fields in [('UserPromptSubmit', {'prompt': 'Repair widget', 'cwd': '/work/widget', 'agent_origin': 'VS Code'}), ('PreToolUse', {'tool_name': 'Bash'}), ('Stop', {})]:
        store.submit(hook_report('claude', {'hook_event_name': event, 'session_id': 'real-session-id', **fields}))
    status = store.statuses()[0]
    assert status.session_id == 'real-session-id'
    assert status.provider == 'claude'
    assert status.cwd == '/work/widget'
    assert status.origin == 'VS Code'
    assert 'Repair widget' in status.display_name
    from sidepulse.program_status import phone_update
    assert all('context' not in phone_update(event) for _, event in store.pending_events('phone'))


def test_notifications_survive_migration_without_reviving_completed_work(runtime):
    store, _, monitor = runtime
    emit(runtime, 'UserPromptSubmit')
    actual, _ = emit(runtime, 'Notification', notification_type='permission_prompt', message='Permission required')
    assert actual.aggregate.mode == AgentMode.WAITING_FOR_INPUT
    emit(runtime, 'Stop')
    events = store.pending_events('phone')
    emit(runtime, 'Notification', message='Processing')
    assert monitor.snapshot().aggregate.mode == AgentMode.COMPLETED
    assert store.pending_events('phone') == events


def test_internal_codex_session_stays_suppressed_across_sparse_hooks(runtime):
    for event, fields in [('UserPromptSubmit', {'prompt': 'Generate 0 to 3 hyperpersonalized suggestions'}), ('PreToolUse', {'tool_name': 'shell'}), ('Stop', {})]:
        actual, expected = emit(runtime, event, provider='codex', **fields)
        assert actual.statuses == expected.statuses == ()


@pytest.mark.parametrize('failure', [None, 'adapter', 'persistence'])
def test_hook_only_submits_osc_json_even_on_failure(tmp_path, failure):
    from sidepulse.hook import hook_log_main
    import sidepulse.hook as hook
    payload = {'hook_event_name': 'Stop', 'session_id': 'session'}
    original = hook_report
    with patch('sys.stdin', io.StringIO(json.dumps(payload))), patch('sidepulse.hook.hook_event_socket_disabled', return_value=False), patch('sidepulse.program_adapter.hook_report', side_effect=RuntimeError('bad conversion') if failure == 'adapter' else original), patch('sidepulse.program_transport.submit_status', side_effect=OSError('unavailable') if failure == 'persistence' else None) as submit, patch('sidepulse.hook.write_hook_status_audit'):
        assert hook_log_main('claude', tmp_path / 'events.jsonl') == 0
    assert submit.called == (failure != 'adapter')
    if submit.called:
        assert submit.call_args.args[0]['type'] == 'program_status'
        assert submit.call_args.args[0]['status']['state'] == 'done'
    assert not hasattr(hook, 'send_hook_event')
    assert (tmp_path / 'events.jsonl').exists()  # Diagnostics only.


def test_raw_hook_history_cannot_override_accounted_state(runtime):
    store, history, monitor = runtime
    emit(runtime, 'UserPromptSubmit')
    line = {'hook_event_name': 'PermissionRequest', 'session_id': 'session-12345678',
            'logged_at': (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()}
    history.ingest_record(parse_log_line('claude', json.dumps(line)))
    assert history.snapshot().aggregate.mode == AgentMode.WAITING_FOR_INPUT
    assert monitor.snapshot().aggregate.mode == AgentMode.WORKING


def test_remote_osc_json_preserves_metadata_and_clear(runtime, tmp_path):
    store, _, monitor = runtime
    source = ProgramState()
    try:
        source.submit(hook_report('claude', {'hook_event_name':'UserPromptSubmit', 'session_id':'remote-session',
                                            'prompt':'Remote task', 'cwd':'/remote/project'}))
        for _, update in source.pending_events('receiver'):
            store.receive_update('remote', update)
        actual = monitor.snapshot()
        assert len(actual.statuses) == 1
        assert actual.statuses[0].provider == 'claude'
        assert actual.statuses[0].session_id == 'remote-session'
        source.submit({'v':1, 'type':'program_status', 'scope':update['scope'], 'status':{'state':'clear'}})
        for _, update in source.pending_events('receiver'):
            store.receive_update('remote', update)
        assert monitor.snapshot().statuses == ()
    finally:
        source.close()


def test_camel_case_tool_invocation_identity_is_preserved():
    report = hook_report('grok', {'hookEventName': 'PermissionRequest', 'sessionId': 's', 'toolUseId': 'invocation', 'toolName': 'Bash'})
    other = hook_report('grok', {'hook_event_name': 'PermissionRequest', 'session_id': 's', 'tool_use_id': 'invocation', 'tool_name': 'Bash'})
    assert report['status']['id'] == other['status']['id']


def test_hook_status_does_not_infer_completion_from_silence(runtime):
    from datetime import timedelta
    from sidepulse.settings import AgentMonitorSettings
    actual, expected = emit(runtime, 'SessionStart')
    assert actual.aggregate.mode == AgentMode.IDLE_READY
    emit(runtime, 'UserPromptSubmit')
    emit(runtime, 'PostToolUse', tool_name='Bash')
    store, _, monitor = runtime
    future = datetime.now(timezone.utc) + timedelta(seconds=121)
    with patch('sidepulse.collector.datetime') as clock:
        clock.now.return_value = future
        actual = monitor.snapshot()
    assert actual.aggregate.mode == AgentMode.WORKING  # Explicit state survives silence.
    assert store.statuses()[0].mode == AgentMode.WORKING  # No synthetic OSC Done.


def test_ui_only_controls_daemon_on_explicit_output_action(runtime):
    try:
        from sidepulse import status_bar
    except SystemExit as exc:
        pytest.skip(str(exc))
    fake = SimpleNamespace(leds_enabled=True, last_led_error=None, device_errors={})
    with patch('sidepulse.status_bar.daemon_command', return_value={'ok':True,'output_errors':{}}) as command, patch('sidepulse.status_bar.write_led_program') as write, patch('sidepulse.status_bar.send_ios_program') as phone:
        status_bar.StatusBarController.sync_leds_now(fake, AgentMode.WORKING, None, 'agent')
        command.assert_not_called()
        status_bar.StatusBarController.connect_device(fake)
    assert command.call_args.args[0]['type'] == 'output_control'
    assert 'mode' not in command.call_args.args[0]
    write.assert_not_called(); phone.assert_not_called()


def test_relay_forwards_explicit_state_independent_of_hook_visibility(runtime):
    from sidepulse.program_transport import ProgramRelay
    from sidepulse.settings import AgentMonitorSettings
    store, legacy, monitor = runtime
    line = {'hook_event_name': 'UserPromptSubmit', 'session_id': 'quiet-session',
            'logged_at': (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()}
    store.submit(hook_report('claude', line))
    legacy.ingest_record(parse_log_line('claude', json.dumps(line)))
    assert monitor.snapshot().aggregate.mode == AgentMode.WORKING
    config = SimpleNamespace(server='https://example.com', receiver_channel=None, outbound_channel=None)
    phone = SimpleNamespace(status_protocol=2, link_id='phone', platform='android', server=config.server, token='test')
    with patch('sidepulse.relay.load_relay_config', return_value=config), patch('sidepulse.settings.load_settings', return_value=AgentMonitorSettings(idle_timeout_seconds=10)), patch('sidepulse.links.load_phone_links', return_value=[phone]), patch('sidepulse.program_transport.publish_update') as send:
        relay = ProgramRelay(store)
        relay.sync()
        assert send.call_count == 0 # Enabling forwarding never replays the earlier hook.
        store.submit(hook_report('claude', line))
        relay.sync()
        relay.stop()
    assert send.call_count == 1  # One complete session value per hook.
    assert send.call_args_list[0].args[2]['status']['state'] == 'working'
    assert store.statuses()  # Forward explicit state without desktop visibility heuristics.


@pytest.mark.parametrize('provider', ['claude', 'codex', 'grok', 'cursor', 'junie'])
def test_real_hook_socket_accounting_remote_ui_flow(tmp_path, provider):
    from sidepulse.hook import hook_log_main
    from sidepulse.program_transport import ProgramServer
    import tempfile
    from pathlib import Path
    sender = ProgramState()
    receiver = ProgramState()
    try:
        with tempfile.TemporaryDirectory(prefix='sp-', dir=None if os.name == 'nt' else '/tmp') as directory:
            server = ProgramServer(sender, Path(directory) / 'status.sock')
            server.start()
            try:
                payload = {'hook_event_name':'UserPromptSubmit', 'session_id':'socket-session',
                           'cwd':'/work/widget', 'prompt':'Repair the widget'}
                with patch('sys.stdin', io.StringIO(json.dumps(payload))), patch('sidepulse.hook.hook_event_socket_disabled', return_value=False), patch('sidepulse.program_transport.status_socket_path', return_value=server.path), patch('sidepulse.hook.write_hook_status_audit'):
                    assert hook_log_main(provider, tmp_path / 'diagnostics.jsonl') == 0
                events = sender.pending_events('receiver')
                assert [e['type'] for _, e in events] == ['program_status']
                for _, event in events:
                    receiver.receive_update('remote', event)
                ui = ProgramMonitor(receiver).snapshot()
                assert ui.aggregate.mode == AgentMode.WORKING
                assert len(ui.statuses) == 1
                assert ui.statuses[0].provider == provider
                assert ui.statuses[0].cwd == '/work/widget'
                assert ui.statuses[0].session_id == 'socket-session'
            finally:
                server.stop()
    finally:
        sender.close(); receiver.close()


def test_codex_native_title_updates_without_legacy_monitor(runtime):
    store, _, monitor = runtime
    for title in ['Initial provider title', 'Renamed provider title']:
        with patch('sidepulse.collector.codex_session_title', return_value=title):
            store.submit(hook_report('codex', {'hook_event_name':'UserPromptSubmit', 'session_id':'named-session', 'prompt':'User prompt', 'cwd':'/work/widget'}))
        assert title in monitor.snapshot().statuses[0].display_name


def test_normalized_hook_finds_daemon_with_different_shell_state_directory(tmp_path):
    from sidepulse.hook import hook_log_main
    from sidepulse.program_transport import ProgramServer, status_socket_path
    import tempfile
    from pathlib import Path
    import os
    store = ProgramState()
    try:
        with tempfile.TemporaryDirectory(prefix='sp-', dir=None if os.name == 'nt' else '/tmp') as directory:
            home = Path(directory)
            app_state = home / '.local/state/sidepulse/agent-monitor'
            server = ProgramServer(store, status_socket_path(app_state))
            server.start()
            try:
                with patch.dict(os.environ, {'XDG_STATE_HOME':str(home / 'shell-state')}), patch('pathlib.Path.home', return_value=home), patch('sidepulse.hook.hook_event_socket_disabled', return_value=False), patch('sidepulse.hook.write_hook_status_audit'), patch('sys.stdin', io.StringIO('{"hook_event_name":"UserPromptSubmit","session_id":"different-environment"}')):
                    assert hook_log_main('claude', tmp_path / 'raw-diagnostics.jsonl') == 0
                assert ProgramMonitor(store).snapshot().aggregate.mode == AgentMode.WORKING
                assert not (home / 'shell-state/sidepulse/agent-monitor/program-status.sqlite3').exists()
            finally:
                server.stop()
    finally:
        store.close()


@pytest.mark.parametrize('provider', ['claude', 'codex', 'grok', 'cursor', 'junie'])
def test_hook_control_characters_in_prompt_do_not_reject_status(runtime, provider):
    from sidepulse.program_status import validate_context
    store, _, monitor = runtime
    payload = {'hook_event_name':'UserPromptSubmit', 'session_id':'formatting-session',
               'prompt':'Fix \x1b[31m widget\x7f\x85', 'cwd':'/work/widget',
               'agent_origin':'Terminal\x1b[0m'}
    report = hook_report(provider, payload)
    validate_context(report['context'])
    assert report['context']['session_id'] == payload['session_id']
    assert report['context']['cwd'] == payload['cwd']
    assert all(not (ord(c) < 32 or 127 <= ord(c) <= 159)
               for field in ('title', 'display_name', 'origin')
               for c in report['context'].get(field, ''))
    store.submit(report)
    assert monitor.snapshot().aggregate.mode == AgentMode.WORKING
    assert len(monitor.snapshot().statuses) == 1
    assert monitor.snapshot().statuses[0].session_id == payload['session_id']


def test_long_unicode_and_invalid_unicode_hook_text_is_bounded(runtime):
    from sidepulse.program_status import CONTEXT_LIMITS, validate_update
    store, _, monitor = runtime
    title = '🟢' * 1000 + '\ud800'
    with patch('sidepulse.collector.codex_session_title', return_value=title):
        report = hook_report('codex', {'hook_event_name':'UserPromptSubmit', 'session_id':'unicode-session',
                                      'prompt':'Continue', 'cwd':'/work/widget'})
    store.submit(report)
    assert report['status']['title'] == '🟢' * 48
    assert report['context']['title'] == '🟢' * 128
    # A surrogate near the beginning must also be made encodable, rather than
    # silently losing the whole update before the byte limit is reached.
    report = hook_report('claude', {'hook_event_name':'Stop', 'session_id':'surrogate-session',
                                   'last_assistant_message':'Finished \ud800\x1b' + '🙂' * 1000})
    store.submit(report)
    assert report['status']['msg'] == 'Finished ? ' + '🙂' * 253
    assert monitor.snapshot().statuses
    for _, event in store.pending_events('receiver'):
        assert validate_update(event) == event
        for field, value in event.get('context', {}).items():
            assert len(value.encode('utf-8')) <= CONTEXT_LIMITS[field]


def test_permission_identity_and_clear_survive_cleaned_display_text(runtime):
    from sidepulse.program_adapter import wait_id
    from sidepulse.program_status import CONTEXT_LIMITS
    store, _, monitor = runtime
    payload = {'hook_event_name':'PermissionRequest', 'session_id':'permission-session', 'agent_id':'agent-123',
               'tool_name':'Bash\x1b' + '🟢' * 200, 'tool_input':{'command':'echo hello'},
               'agent_origin':'Terminal\x07' + '🟢' * 200}
    expected_id = wait_id(parse_log_line('claude', json.dumps(payload)))
    with patch('sidepulse.program_adapter.socket.gethostname', return_value='host\x00' + '🟢' * 100):
        report = hook_report('claude', payload)
    assert report['status'] == {'state':'blocked', 'id':expected_id, 'app':'claude-code', 'kind':'permission'}
    assert report['context']['agent_id'] == payload['agent_id']
    for field in ('tool_name', 'origin', 'source_name'):
        text = report['context'][field]
        assert len(text.encode('utf-8')) <= CONTEXT_LIMITS[field]
        assert all(not (ord(c) < 32 or 127 <= ord(c) <= 159) for c in text)
    store.submit(report)
    assert monitor.snapshot().aggregate.mode == AgentMode.WAITING_FOR_INPUT
    completed = hook_report('claude', {**payload, 'hook_event_name':'PostToolUse'})
    assert completed['status']['state'] == 'working'
    store.submit(completed)
    assert monitor.snapshot().aggregate.mode == AgentMode.WORKING


@pytest.mark.parametrize('context', [
    {'display_name':'Bad\x1bname'}, {'title':'🟢' * 129},
    {'origin':'Bad\x00origin'}, {'session_id':'session\x1b'},
])
def test_direct_json_context_stays_strict(runtime, context):
    from sidepulse.program_status import StatusError
    store, _, _ = runtime
    before = store.statuses(), store.pending_events('phone')
    with pytest.raises(StatusError):
        store.submit({'v':1, 'type':'program_status', 'scope':'direct',
                      'status':{'state':'working'}, 'context':context})
    assert (store.statuses(), store.pending_events('phone')) == before
    assert store.pending_events('phone') == []
