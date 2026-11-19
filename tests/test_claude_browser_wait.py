from unittest.mock import patch

import pytest

from sidepulse.models import AgentMode
from sidepulse.program_adapter import hook_report
from sidepulse.program_status import ProgramState


def browser_report(event, tool='navigate', url='https://example.org/', provider='claude', **fields):
    return hook_report(provider, {
        'session_id': 'browser-session', 'hook_event_name': event,
        'tool_name': 'mcp__Claude_Browser__' + tool, 'tool_use_id': 'call',
        'tool_input': {'url': url}, **fields,
    })


@pytest.mark.parametrize('tool', ['navigate', 'preview_start'])
def test_current_browser_call_raises_and_forwards_one_ask_after_10_seconds(tool):
    state = ProgramState()
    receiver = ProgramState()
    with patch('sidepulse.program_status.time.monotonic', return_value=100) as clock:
        state.submit(browser_report('PreToolUse', tool))
        clock.return_value = 109.99
        state.check_tool_waits()
        assert state.statuses()[0].mode == AgentMode.TOOL_RUNNING
        state.submit(browser_report('PostToolUse', tool_use_id='parallel-call'))
        clock.return_value = 110
        state.check_tool_waits()
        row = state.statuses()[0]
        assert row.mode == AgentMode.WAITING_FOR_INPUT
        assert row.message == 'Check the browser pane — this page is still opening.'
        [(serial, update)] = state.pending_events('receiver')
        receiver.receive_update('sender', update)
        assert receiver.statuses()[0].mode == AgentMode.WAITING_FOR_INPUT
        state.acknowledge_event('receiver', serial)
        state.check_tool_waits()
        assert not state.pending_events('receiver')
        state.submit(browser_report('PostToolUse', tool))
        assert state.statuses()[0].mode == AgentMode.WORKING


@pytest.mark.parametrize('event,expected', [
    ('PostToolUse', AgentMode.WORKING), ('PostToolUseFailure', AgentMode.BLOCKED_ERROR),
    ('SessionEnd', AgentMode.COMPLETED), ('Stop', AgentMode.COMPLETED),
    ('PermissionRequest', AgentMode.WAITING_FOR_INPUT),
])
def test_new_status_cancels_browser_deadline(event, expected):
    state = ProgramState()
    with patch('sidepulse.program_status.time.monotonic', return_value=0) as clock:
        state.submit(browser_report('PreToolUse'))
        state.submit(browser_report(event))
        clock.return_value = 30
        state.check_tool_waits()
        row = state.statuses()[0]
        assert (row.mode, row.event_name, row.seq) == (expected, event, 2)


@pytest.mark.parametrize('after_timeout', [False, True])
@pytest.mark.parametrize('event,fields,expected', [
    ('PostToolUseFailure', {}, AgentMode.BLOCKED_ERROR),
    ('PostToolUse', {'tool_response': {'success': False}}, AgentMode.BLOCKED_ERROR),
    ('PostToolUse', {'sidepulse_mode': 'waiting_for_input'}, AgentMode.WAITING_FOR_INPUT),
    ('PostToolUse', {'sidepulse_mode': 'working'}, AgentMode.WORKING),
])
def test_parallel_tool_error_or_explicit_status_takes_precedence(after_timeout, event, fields, expected):
    state = ProgramState()
    with patch('sidepulse.program_status.time.monotonic', return_value=0) as clock:
        state.submit(browser_report('PreToolUse'))
        if after_timeout:
            clock.return_value = 10
            state.check_tool_waits()
        state.submit(browser_report(event, tool_name='Bash', tool_use_id='other', **fields))
        clock.return_value = 20
        state.check_tool_waits()
        row = state.statuses()[0]
        assert (row.tool_name, row.mode, row.event_name) == ('Bash', expected, event)


def test_second_browser_call_gets_its_own_deadline_and_return():
    state = ProgramState()
    with patch('sidepulse.program_status.time.monotonic', return_value=0) as clock:
        state.submit(browser_report('PreToolUse'))
        clock.return_value = 5
        state.submit(browser_report('PreToolUse', tool_use_id='second'))
        clock.return_value = 10
        state.submit(browser_report('PostToolUse'))
        state.check_tool_waits()
        assert state.statuses()[0].mode == AgentMode.TOOL_RUNNING
        clock.return_value = 15
        state.check_tool_waits()
        assert state.statuses()[0].mode == AgentMode.WAITING_FOR_INPUT
        state.submit(browser_report('PostToolUseFailure', tool_use_id='second'))
        assert state.statuses()[0].mode == AgentMode.BLOCKED_ERROR


def test_retries_and_remote_working_reports_do_not_restart_the_timer():
    sender, receiver = ProgramState(), ProgramState()
    report = browser_report('PreToolUse')
    with patch('sidepulse.program_status.time.monotonic', return_value=0) as clock:
        ack = sender.submit(report)
        [(serial, working)] = sender.pending_events('receiver')
        assert not any(field.startswith('_') for field in working)
        receiver.receive_update('sender', working)
        sender.acknowledge_event('receiver', serial)
        clock.return_value = 9
        assert sender.submit(report) == ack
        clock.return_value = 10
        receiver.check_tool_waits()
        assert receiver.statuses()[0].mode == AgentMode.TOOL_RUNNING
        sender.check_tool_waits()
        assert sender.submit(report) == ack
        [(serial, waiting)] = sender.pending_events('receiver')
        receiver.receive_update('sender', waiting)
        sender.acknowledge_event('receiver', serial)
        clock.return_value = 20
        sender.check_tool_waits()
        assert not sender.pending_events('receiver')
        assert receiver.statuses()[0].mode == AgentMode.WAITING_FOR_INPUT


def test_same_tool_id_in_two_sessions_has_independent_deadlines():
    state = ProgramState()
    with patch('sidepulse.program_status.time.monotonic', return_value=0) as clock:
        state.submit(browser_report('PreToolUse', session_id='a'))
        state.submit(browser_report('PreToolUse', session_id='b'))
        state.submit(browser_report('SessionEnd', session_id='b'))
        clock.return_value = 10
        state.check_tool_waits()
        assert {row.session_id: row.mode for row in state.statuses()} == {
            'a': AgentMode.WAITING_FOR_INPUT, 'b': AgentMode.COMPLETED,
        }


def test_delayed_completion_is_ordered_against_the_tool_start():
    state = ProgramState()
    with patch('sidepulse.program_status.time.monotonic', return_value=0) as clock:
        state.submit(browser_report('PreToolUse'), observed_at=100_000)
        clock.return_value = 10
        state.check_tool_waits()
        state.submit(browser_report('SessionEnd'), observed_at=99_000)
        assert state.statuses()[0].mode == AgentMode.WAITING_FOR_INPUT
        state.submit(browser_report('SessionEnd'), observed_at=109_000)
        assert state.statuses()[0].mode == AgentMode.COMPLETED


@pytest.mark.parametrize('fields', [
    {'tool': 'browser_batch'}, {'url': 'back'}, {'url': 'forward'},
    {'url': None}, {'url': 'http://['}, {'url': 'file:///tmp/page'},
    {'provider': 'codex'}, {'tool_use_id': None},
])
def test_other_tool_calls_do_not_start_browser_wait(fields):
    assert '_tool_wait' not in browser_report('PreToolUse', **fields)
