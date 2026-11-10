"""Codex questions need attention; recoverable tool failures do not."""
import json
from pathlib import Path

import pytest

from sidepulse.program_adapter import hook_report
from sidepulse.program_presentation import ProgramMonitor
from sidepulse.program_status import ProgramState
from sidepulse.collector import LiveAgentMonitor
from sidepulse.providers import parse_log_line


def emit(state, event, tool=None, **fields):
    payload = {"hook_event_name": event, "session_id": "question-session", **fields}
    if tool:
        payload["tool_name"] = tool
    report = hook_report("codex", payload)
    state.submit(report)
    return state.statuses()[0].mode.value


def test_recorded_codex_question_sequence_and_forwarded_view():
    fixture = json.loads((Path(__file__).parent / "fixtures/codex-async-question.json").read_text())
    sender, receiver = ProgramState(), ProgramState()
    legacy = LiveAgentMonitor(sources=(), recovery_sources=(), latest_state_path=None)
    try:
        cursor = 0
        for row in fixture["sequence"]:
            report = hook_report("codex", row["event"])
            sender.submit(report)
            legacy.ingest_record(parse_log_line("codex", json.dumps(row["event"])))
            for serial, update in sender.pending_events("receiver"):
                if serial > cursor:
                    assert "unless_kind" not in update
                    assert "unless_state" not in update
                    receiver.receive_update("desktop", update)
                    cursor = serial
            expected = row["expected_mode"]
            assert sender.statuses()[0].mode.value == expected, row["event"]
            assert ProgramMonitor(receiver).snapshot().aggregate.mode.value == expected, row["event"]
            assert legacy.snapshot().aggregate.mode.value == expected, row["event"]
    finally:
        sender.close()
        receiver.close()


@pytest.mark.parametrize("tool", ["request_user_input", "functions.request_user_input"])
def test_blocking_question_finishes_on_tool_answer(tool):
    state = ProgramState()
    try:
        assert emit(state, "PreToolUse", tool) == "waiting_for_input"
        assert emit(state, "PostToolUse", tool, tool_response={"answers": {"color": "red"}}) == "working"
    finally:
        state.close()


@pytest.mark.parametrize("tool", ["request_user_input_async", "functions.request_user_input_async"])
@pytest.mark.parametrize("response", [{"accepted": True}, '{"accepted":true}'])
def test_async_acknowledgement_does_not_release_wait(response, tool):
    state = ProgramState()
    try:
        emit(state, "PreToolUse", tool)
        assert emit(state, "PostToolUse", tool, tool_response=response) == "waiting_for_input"
        before = state.pending_events("phone")
        assert emit(state, "PreToolUse", "clocksleep") == "waiting_for_input"
        assert emit(state, "PostToolUse", "clocksleep") == "waiting_for_input"
        assert emit(state, "PostToolUse", "Bash", tool_response={'exit_code': 1}) == "waiting_for_input"
        assert emit(state, "PostToolUseFailure", "Bash") == "waiting_for_input"
        assert state.pending_events("phone") == before
        assert emit(state, "Notification", message="Still working") == "waiting_for_input"
        assert emit(state, "PermissionRequest", "Bash") == "waiting_for_input"
        assert emit(state, "PostToolUse", "Bash") == "waiting_for_input"
        assert emit(state, "UserPromptSubmit", prompt="red") == "working"
        assert emit(state, "PreToolUse", "Bash") == "tool_running"
    finally:
        state.close()


def test_explicit_status_can_override_question_wait():
    state = ProgramState()
    legacy = LiveAgentMonitor(sources=(), recovery_sources=(), latest_state_path=None)
    try:
        for event in [
            {"hook_event_name": "PreToolUse", "tool_name": "request_user_input_async"},
            {"hook_event_name": "PostToolUse", "tool_name": "Bash", "sidepulse_status": "working"},
        ]:
            event["session_id"] = "question-session"
            state.submit(hook_report("codex", event))
            legacy.ingest_record(parse_log_line("codex", json.dumps(event)))
        assert state.statuses()[0].mode.value == "working"
        assert legacy.snapshot().aggregate.mode.value == "working"
    finally:
        state.close()


@pytest.mark.parametrize("guard", [None, [], {}, "unknown"])
def test_invalid_kind_guard_cannot_mutate_accounting(guard):
    from sidepulse.program_status import StatusError
    state = ProgramState()
    try:
        emit(state, "UserPromptSubmit")
        before = state.statuses(), state.pending_events("phone")
        with pytest.raises(StatusError, match="guard"):
            state.submit({"v": 1, "type": "program_status", "scope": "other",
                          "status": {"state": "working"}, "unless_kind": guard})
        assert (state.statuses(), state.pending_events("phone")) == before
    finally:
        state.close()


@pytest.mark.parametrize("event,fields,expected", [
    ("UserPromptSubmit", {"prompt": "red"}, "working"),
    ("Interrupt", {}, "idle_ready"),
    ("Stop", {"last_assistant_message": "Finished"}, "completed"),
    ("SessionEnd", {}, "completed"),
    ("PostToolUseFailure", {"tool_name": "request_user_input_async"}, "working"),
    ("PostToolUse", {"tool_name": "request_user_input_async", "tool_response": {"accepted": False}}, "working"),
])
def test_question_wait_has_explicit_release_paths(event, fields, expected):
    state = ProgramState()
    try:
        emit(state, "PreToolUse", "request_user_input_async")
        emit(state, "PostToolUse", "request_user_input_async", tool_response={"accepted": True})
        assert emit(state, event, **fields) == expected
    finally:
        state.close()


def test_question_guard_does_not_hold_permission_or_other_session():
    state = ProgramState()
    try:
        emit(state, "PermissionRequest", "Bash")
        assert emit(state, "PostToolUse", "Bash") == "working"
        emit(state, "PreToolUse", "request_user_input_async")
        other = hook_report("codex", {"hook_event_name": "PreToolUse", "session_id": "other", "tool_name": "Bash"})
        state.submit(other)
        assert {s.session_id: s.mode.value for s in state.statuses()} == {
            "question-session": "waiting_for_input", "other": "tool_running"}
    finally:
        state.close()


@pytest.mark.parametrize('event,fields', [
    ('PostToolUse', {'tool_response': {'exit_code': 1}}),
    ('PostToolUse', {'tool_response': {'success': False}}),
    ('PostToolUse', {'tool_response': {'interrupted': True}}),
    ('PostToolUse', {'tool_response': 'Traceback (most recent call last):\nAssertionError: missing sensor'}),
    ('PostToolUseFailure', {}),
    ('PermissionDenied', {}),
])
def test_codex_tool_failures_stay_working_locally_and_when_forwarded(event, fields):
    sender, receiver = ProgramState(), ProgramState()
    legacy = LiveAgentMonitor(sources=(), recovery_sources=(), latest_state_path=None)
    try:
        payload = {'hook_event_name': event, 'session_id': 'recovering-session', 'tool_name': 'Bash', **fields}
        report = hook_report('codex', payload)
        assert report['status']['state'] == 'working'
        sender.submit(report)
        legacy.ingest_record(parse_log_line('codex', json.dumps(payload)))
        for _, update in sender.pending_events('receiver'):
            assert update['status']['state'] == 'working'
            receiver.receive_update('desktop', update)
        assert sender.statuses()[0].mode.value == 'working'
        assert legacy.snapshot().aggregate.mode.value == 'working'
        assert ProgramMonitor(receiver).snapshot().aggregate.mode.value == 'working'
    finally:
        sender.close()
        receiver.close()


@pytest.mark.parametrize('event,fields,expected', [
    ('StopFailure', {}, 'error'),
    ('PostToolUse', {'sidepulse_status': 'error'}, 'error'),
    ('PermissionRequest', {}, 'blocked'),
    ('PreToolUse', {'tool_name': 'request_user_input_async'}, 'blocked'),
])
def test_codex_explicit_failures_and_input_requests_still_need_attention(event, fields, expected):
    report = hook_report('codex', {'hook_event_name': event, 'session_id': 'session', **fields})
    assert report['status']['state'] == expected


@pytest.mark.parametrize('provider', ['claude', 'grok', 'cursor', 'junie'])
def test_other_providers_retain_tool_error_reporting(provider):
    report = hook_report(provider, {'hook_event_name': 'PostToolUse', 'session_id': 'session',
                                  'tool_name': 'Bash', 'tool_response': {'exit_code': 1}})
    assert report['status']['state'] == 'error'
