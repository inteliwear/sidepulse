"""A proposed plan is waiting for review, even when Codex's Stop text is null."""
import copy
import json
from pathlib import Path

import pytest

from sidepulse.collector import LiveAgentMonitor, mode_for_event
from sidepulse.program_adapter import hook_report
from sidepulse.program_presentation import ProgramMonitor
from sidepulse.program_status import ProgramState
from sidepulse.providers import parse_log_line

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/codex-plan-confirmation.json').read_text())


def rollout(tmp_path, rows=None):
    event = copy.deepcopy(FIXTURE['hook'])
    path = tmp_path / f"rollout-2026-10-27-{event['session_id']}.jsonl"
    path.write_text(''.join(json.dumps(r)+'\n' for r in (rows if rows is not None else FIXTURE['transcript'])))
    event['transcript_path'] = str(path)
    return event, path


def test_real_null_stop_plan_wait_reaches_legacy_and_forwarded_views(tmp_path):
    event, _ = rollout(tmp_path)
    sender, receiver = ProgramState(), ProgramState()
    legacy = LiveAgentMonitor(sources=(), recovery_sources=(), latest_state_path=None)
    try:
        sender.submit(hook_report('codex', event))
        legacy.ingest_record(parse_log_line('codex', json.dumps(event)))
        assert sender.statuses()[0].mode.value == 'waiting_for_input'
        assert legacy.snapshot().aggregate.mode.value == 'waiting_for_input'
        for _, update in sender.pending_events('receiver'):
            receiver.receive_update('desktop', update)
        assert ProgramMonitor(receiver).snapshot().aggregate.mode.value == 'waiting_for_input'
    finally:
        sender.close()
        receiver.close()


@pytest.mark.parametrize('change', ['default', 'wrong-turn', 'new-turn', 'commentary', 'ordinary', 'fenced', 'missing', 'other-session', 'relative', 'subagent'])
def test_only_same_session_current_turn_final_plan_needs_review(tmp_path, change):
    rows = copy.deepcopy(FIXTURE['transcript'])
    if change == 'default': rows[0]['payload']['collaboration_mode']['mode'] = 'default'
    if change == 'wrong-turn': rows[0]['payload']['turn_id'] = 'unrelated-turn'
    if change == 'new-turn': rows.append({'type':'turn_context','payload':{'turn_id':'new-turn','collaboration_mode':{'mode':'plan'}}})
    if change == 'commentary': rows[1]['payload']['phase'] = 'commentary'
    if change == 'ordinary': rows[1]['payload']['content'] = [{'text':'The task is complete.'}]
    if change == 'fenced': rows[1]['payload']['content'] = [{'text':'Example:\n~~~xml\n<proposed_plan>\nExample only\n</proposed_plan>\n~~~'}]
    event, path = rollout(tmp_path, rows)
    if change == 'missing': path.unlink()
    if change == 'other-session': event['session_id'] = '33333333-3333-4333-8333-333333333333'
    if change == 'relative': event['transcript_path'] = path.name
    if change == 'subagent': event['agent_id'] = 'subagent'
    assert mode_for_event(parse_log_line('codex', json.dumps(event))).value == 'completed'


@pytest.mark.parametrize('event_name,fields,expected', [
    ('UserPromptSubmit', {'prompt':'Implement the plan.'}, 'working'),
    ('Stop', {'last_assistant_message':'The task is complete.'}, 'completed'),
    ('Interrupt', {}, 'idle_ready'),
    ('SessionEnd', {}, 'completed'),
    ('StopFailure', {}, 'blocked_error'),
    ('PostToolUse', {'sidepulse_status':'working'}, 'working'),
])
def test_pending_plan_is_released_by_next_action(tmp_path, event_name, fields, expected):
    event, _ = rollout(tmp_path)
    sender = ProgramState()
    legacy = LiveAgentMonitor(sources=(), recovery_sources=(), latest_state_path=None)
    try:
        for payload, wanted in [
            (event, 'waiting_for_input'),
            ({**event, 'hook_event_name':'Notification','message':'Still working'}, 'waiting_for_input'),
            ({**event, 'hook_event_name':event_name, **fields}, expected),
        ]:
            sender.submit(hook_report('codex', payload))
            legacy.ingest_record(parse_log_line('codex', json.dumps(payload)))
            assert sender.statuses()[0].mode.value == wanted
            assert legacy.snapshot().aggregate.mode.value == wanted
    finally:
        sender.close()


def test_bounded_tail_can_recover_current_plan_without_scanning_history(tmp_path, monkeypatch):
    from sidepulse import codex_plan
    monkeypatch.setattr(codex_plan, 'MAX_TRANSCRIPT_BYTES', 4096)
    event, path = rollout(tmp_path)
    tail = path.read_bytes()
    path.write_bytes(b'{"large_old_record":"' + b'x'*20000 + b'"}\n' + tail)
    assert mode_for_event(parse_log_line('codex', json.dumps(event))).value == 'waiting_for_input'
    path.write_bytes(tail + b'{"large_new_record":"' + b'x'*20000 + b'"}\n')
    assert mode_for_event(parse_log_line('codex', json.dumps(event))).value == 'completed'


@pytest.mark.parametrize('text', ['<proposed_plan>\n\n</proposed_plan>',
                                  '`<proposed_plan>`\nExample\n`</proposed_plan>`',
                                  '> <proposed_plan>\n> Example\n> </proposed_plan>',
                                  '```xml\n<proposed_plan>\nExample\n</proposed_plan>\n```'])
def test_plan_examples_and_empty_blocks_are_not_review_requests(text):
    from sidepulse.codex_plan import is_proposed_plan
    assert not is_proposed_plan(text)


def test_aborted_plan_does_not_request_review(tmp_path):
    rows = copy.deepcopy(FIXTURE['transcript'])
    rows.append({'type':'event_msg','payload':{'type':'turn_aborted'}})
    event, _ = rollout(tmp_path, rows)
    assert mode_for_event(parse_log_line('codex', json.dumps(event))).value == 'completed'


@pytest.mark.skipif(__import__('os').name != 'posix', reason='FIFO is POSIX-only')
def test_non_regular_rollout_cannot_block_hook(tmp_path):
    import os
    event, path = rollout(tmp_path)
    path.unlink()
    os.mkfifo(path)
    assert mode_for_event(parse_log_line('codex', json.dumps(event))).value == 'completed'
