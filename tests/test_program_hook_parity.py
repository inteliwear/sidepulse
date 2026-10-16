"""Existing hook behavior must survive migration to normalized accounting.

The existing collector is the baseline interpreter. Deliberate OSC differences
(no inferred completion from silence, explicit lifecycle, receiver-owned rendering)
are separate from hook transport, metadata, and interpretation compatibility.
"""
import io
import json
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from sidepulse.collector import LiveAgentMonitor
from sidepulse.models import AgentMode
from sidepulse.program_adapter import hook_reports
from sidepulse.program_presentation import ProgramMonitor
from sidepulse.program_status import ProgramStore
from sidepulse.providers import parse_log_line


@pytest.fixture
def runtime(tmp_path):
    store = ProgramStore(tmp_path / 'status.sqlite3')
    legacy = LiveAgentMonitor(sources=(), recovery_sources=(), latest_state_path=None)
    monitor = ProgramMonitor(legacy, store)
    yield store, legacy, monitor
    store.close()


def emit(runtime, event, provider='claude', **fields):
    store, legacy, monitor = runtime
    line = {'hook_event_name': event, 'session_id': 'session-12345678',
            'logged_at': datetime.now(timezone.utc).isoformat(), **fields}
    reports = hook_reports(provider, line)
    if reports:
        store.submit({'v': 1, 'type': 'program_status_batch', 'reports': reports})
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
        for field in ('provider', 'session_id', 'cwd', 'origin', 'display_name'):
            assert getattr(a, field) == getattr(e, field), (provider, event, field)


def test_metadata_survives_sparse_events_without_running_legacy_monitor(runtime):
    store, _, _ = runtime
    for event, fields in [('UserPromptSubmit', {'prompt': 'Repair widget', 'cwd': '/work/widget', 'agent_origin': 'VS Code'}), ('PreToolUse', {'tool_name': 'Bash'}), ('Stop', {})]:
        reports = hook_reports('claude', {'hook_event_name': event, 'session_id': 'real-session-id', **fields})
        store.submit({'v': 1, 'type': 'program_status_batch', 'reports': reports})
    status = store.statuses()[0]
    assert status.session_id == 'real-session-id'
    assert status.provider == 'claude'
    assert status.cwd == '/work/widget'
    assert status.origin == 'VS Code'
    assert 'Repair widget' in status.display_name
    assert all('context' not in r for s in store.phone_snapshot()['scopes'].values() for r in s['records'])


def test_notifications_survive_migration_without_reviving_completed_work(runtime):
    store, _, monitor = runtime
    emit(runtime, 'UserPromptSubmit')
    actual, _ = emit(runtime, 'Notification', notification_type='permission_prompt', message='Permission required')
    assert actual.aggregate.mode == AgentMode.WAITING_FOR_INPUT
    emit(runtime, 'Stop')
    revision = store.phone_snapshot()['revision']
    emit(runtime, 'Notification', message='Processing')
    assert monitor.snapshot().aggregate.mode == AgentMode.COMPLETED
    assert store.phone_snapshot()['revision'] == revision


def test_internal_codex_session_stays_suppressed_across_sparse_hooks(runtime):
    for event, fields in [('UserPromptSubmit', {'prompt': 'Generate 0 to 3 hyperpersonalized suggestions'}), ('PreToolUse', {'tool_name': 'shell'}), ('Stop', {})]:
        actual, expected = emit(runtime, event, provider='codex', **fields)
        assert actual.statuses == expected.statuses == ()


def test_successful_normalized_hook_still_delivers_to_ui_and_old_relay(tmp_path):
    from sidepulse.hook import hook_log_main
    payload = {'hook_event_name': 'Stop', 'session_id': 'session'}
    with patch('sys.stdin', io.StringIO(json.dumps(payload))), patch('sidepulse.hook.hook_event_socket_disabled', return_value=False), patch('sidepulse.program_transport.durable_submit', return_value={'ok': True}), patch('sidepulse.hook.send_hook_event', return_value=True) as send, patch('sidepulse.relay.candidate_relay_event_socket_paths', return_value=[tmp_path / 'relay.sock']), patch('sidepulse.hook.write_hook_status_audit'):
        assert hook_log_main('claude', tmp_path / 'events.jsonl') == 0
    assert send.call_count == 2


def test_newer_legacy_fallback_is_not_hidden(runtime):
    store, legacy, monitor = runtime
    emit(runtime, 'UserPromptSubmit')
    line = {'hook_event_name': 'PermissionRequest', 'session_id': 'session-12345678', 'logged_at': (datetime.now(timezone.utc) + timedelta(milliseconds=2)).isoformat()}
    # Simulate normalized submission failing while existing event delivery succeeds.
    legacy.ingest_record(parse_log_line('claude', json.dumps(line)))
    assert monitor.snapshot().aggregate.mode == AgentMode.WAITING_FOR_INPUT


def test_remote_legacy_and_snapshot_delivery_do_not_duplicate_sessions(runtime):
    from sidepulse.relay import annotate_remote_line
    store, legacy, monitor = runtime
    line = {'hook_event_name': 'UserPromptSubmit', 'session_id': 'remote-session', 'prompt': 'Remote task', 'cwd': '/remote/project'}
    with patch('sidepulse.program_adapter.socket.gethostname', return_value='remote-host'):
        reports = hook_reports('claude', line)
    from sidepulse.program_status import apply_report
    snapshot = {'v': 1, 'type': 'program_status_snapshot', 'publisher': 'remote-publisher', 'revision': 0, 'scopes': {}}
    for report in reports: snapshot = apply_report(snapshot, report, int(datetime.now(timezone.utc).timestamp() * 1000))
    legacy.ingest_record(parse_log_line('claude', json.dumps(annotate_remote_line('claude', line, 'remote-host'))))
    store.receive('remote', snapshot)
    actual = monitor.snapshot()
    assert len(actual.statuses) == 1
    assert actual.statuses[0].provider == 'claude'
    assert actual.statuses[0].session_id == 'remote-session'
    cleared = {**snapshot, 'revision': snapshot['revision'] + 1, 'scopes': {}}
    store.receive('remote', cleared)
    assert monitor.snapshot().statuses == ()


def test_camel_case_tool_invocation_identity_is_preserved():
    reports = hook_reports('grok', {'hookEventName': 'PermissionRequest', 'sessionId': 's', 'toolUseId': 'invocation', 'toolName': 'Bash'})
    other = hook_reports('grok', {'hook_event_name': 'PermissionRequest', 'session_id': 's', 'tool_use_id': 'invocation', 'tool_name': 'Bash'})
    assert reports[0]['status']['id'] == other[0]['status']['id']


def test_existing_hook_visibility_policy_does_not_rewrite_explicit_records(runtime):
    from datetime import timedelta
    from sidepulse.settings import AgentMonitorSettings
    actual, expected = emit(runtime, 'SessionStart')
    assert actual.statuses == expected.statuses == ()
    emit(runtime, 'UserPromptSubmit')
    emit(runtime, 'PostToolUse', tool_name='Bash')
    store, _, monitor = runtime
    future = datetime.now(timezone.utc) + timedelta(seconds=121)
    with patch('sidepulse.collector.datetime') as clock:
        clock.now.return_value = future
        actual = monitor.snapshot()
    assert actual.aggregate.mode == AgentMode.COMPLETED  # Existing hook presentation policy.
    assert store.statuses()[0].mode == AgentMode.WORKING  # No synthetic OSC Done.


def test_phone_custom_patterns_never_resume_direct_presentation(runtime):
    from pathlib import Path
    from dataclasses import replace
    from sidepulse.settings import AgentMonitorSettings, AgentAnimationSetting, AGENT_ANIMATION_SOLID_GREEN
    try:
        from sidepulse import status_bar
    except SystemExit as exc:
        pytest.skip(str(exc))
    store, _, monitor = runtime
    emit(runtime, 'UserPromptSubmit')
    link = status_bar.IOSLink('Phone', 'a' * 64, status_protocol=1)
    device = status_bar.StatusBarDevice(device_id=status_bar.linked_phone_device_id(link), name='Phone', root=Path('ios/phone'), target=Path('ios/phone'), connected=True, display='agent', remote_link=link)
    fake = SimpleNamespace(settings=AgentMonitorSettings(), monitor=monitor, program_store=store,
        status_bar_devices=lambda remember=True: [device], ensure_device_selection=lambda: None,
        last_led_error=None, device_errors={}, remote_led_program_by_device={device.device_id: 'off'},
        last_led_display_kind_by_device={}, reset_led_controllers_for_device=lambda _: None,
        active_led_display_kind_for_device=lambda item, battery: item.display)
    with patch('sidepulse.status_bar.send_ios_program', return_value='OK') as send, patch('sidepulse.status_bar.program_for_agent_mode', return_value='off'):
        status_bar.StatusBarController.sync_leds_now(fake, AgentMode.WORKING, None, 'agent')
        send.assert_not_called()
        assert device.device_id not in fake.remote_led_program_by_device
        fake.settings = replace(fake.settings, agent_animations={'working': AgentAnimationSetting(style=AGENT_ANIMATION_SOLID_GREEN)})
        status_bar.StatusBarController.sync_leds_now(fake, AgentMode.WORKING, None, 'agent')
        send.assert_not_called()


def test_headless_battery_display_never_controls_phone_colors(runtime):
    from sidepulse.service import SidePulseService
    from sidepulse.settings import AgentMonitorSettings
    from dataclasses import replace
    store, _, monitor = runtime
    emit(runtime, 'UserPromptSubmit')
    phone = SimpleNamespace(status_protocol=1, link_id='phone', name='Phone')
    service = SimpleNamespace(program_store=store, monitor=monitor, last_program_by_target={})
    settings = replace(AgentMonitorSettings(), led_display='battery')
    with patch('sidepulse.service.status_bar_monitor_available', return_value=False), patch('sidepulse.service.load_settings', return_value=settings), patch('sidepulse.service.discover_devices', return_value=[]), patch('sidepulse.service.read_battery_snapshot', return_value=object()), patch('sidepulse.service.program_for_battery', return_value='battery-program'), patch('sidepulse.links.send_ios_program', return_value='OK') as send:
        SidePulseService.sync_outputs(service)
    send.assert_not_called()


def test_headless_oversized_snapshot_never_falls_back_to_phone_colors(runtime):
    from sidepulse.service import SidePulseService
    from sidepulse.settings import AgentMonitorSettings
    store, _, monitor = runtime
    report = {'v': 1, 'type': 'program_status', 'scope': 'capacity', 'status': {'state': 'working'}}
    store.submit(report)
    phone = SimpleNamespace(status_protocol=1, link_id='phone', name='Phone')
    service = SimpleNamespace(program_store=store, monitor=monitor, last_program_by_target={'phone:phone': 'working-program'})
    with patch('sidepulse.service.status_bar_monitor_available', return_value=False), patch('sidepulse.service.load_settings', return_value=AgentMonitorSettings()), patch('sidepulse.service.discover_devices', return_value=[]), patch('sidepulse.service.program_for_agent_mode', return_value='working-program'), patch('sidepulse.links.send_ios_program', return_value='OK') as send:
        SidePulseService.sync_outputs(service)
        assert 'phone:phone' not in service.last_program_by_target
        send.assert_not_called()
        for i in range(3):
            store.submit({**report, 'status': {'state': 'working', 'id': str(i), 'msg': 'x' * 1500}})
        SidePulseService.sync_outputs(service)
        send.assert_not_called()
        assert 'phone:phone' not in service.last_program_by_target


def test_relay_forwards_explicit_state_independent_of_hook_visibility(runtime):
    from sidepulse.program_transport import ProgramRelay
    from sidepulse.settings import AgentMonitorSettings
    store, legacy, monitor = runtime
    line = {'hook_event_name': 'UserPromptSubmit', 'session_id': 'quiet-session',
            'logged_at': (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()}
    store.submit({'v': 1, 'type': 'program_status_batch', 'reports': hook_reports('claude', line)})
    legacy.ingest_record(parse_log_line('claude', json.dumps(line)))
    assert monitor.snapshot().aggregate.mode == AgentMode.WORKING
    config = SimpleNamespace(server='https://example.com', receiver_channel=None, outbound_channel=None)
    phone = SimpleNamespace(status_protocol=1, link_id='phone', platform='android', server=config.server, token='test')
    with patch('sidepulse.relay.load_relay_config', return_value=config), patch('sidepulse.settings.load_settings', return_value=AgentMonitorSettings(idle_timeout_seconds=10)), patch('sidepulse.links.load_phone_links', return_value=[phone]), patch('sidepulse.program_transport.publish_snapshot') as send:
        ProgramRelay(store).sync()
    send.assert_called_once_with(config.server, phone.token, store.phone_snapshot(), phone=True)
    assert store.phone_snapshot()['scopes']  # Forward explicit state without desktop visibility heuristics.
