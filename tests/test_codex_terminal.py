"""Native dialog closure reconciles one local plan wait through real IPC."""
import copy
import json
import os
from pathlib import Path
import tempfile
import time

import pytest

from sidepulse.program_adapter import hook_report
from sidepulse.program_status import ProgramState
from sidepulse.program_transport import ProgramServer, daemon_command, submit_report
from sidepulse.terminal_wrap import StatusForwarder

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/codex-plan-confirmation.json').read_text())


def pending(tmp_path, state):
    event = copy.deepcopy(FIXTURE['hook'])
    path = tmp_path / ('rollout-' + event['session_id'] + '.jsonl')
    path.write_text(''.join(json.dumps(r) + '\n' for r in FIXTURE['transcript']))
    event.update(transcript_path=str(path), cwd=str(tmp_path))
    state.submit(hook_report('codex', event))
    return event


def wait_for(predicate):
    end = time.monotonic() + 3
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(.01)
    assert predicate()


@pytest.mark.parametrize('race', [False, True])
@pytest.mark.parametrize('previous', ['blocked', 'clear'])
def test_native_idle_reconciles_plan_over_real_socket_without_overwriting_new_turn(tmp_path, monkeypatch, race, previous):
    monkeypatch.chdir(tmp_path)
    state = ProgramState()
    directory = tempfile.TemporaryDirectory(prefix='sp-codex-', dir='/tmp' if os.name == 'posix' else None)
    server = ProgramServer(state, path=Path(directory.name) / 'status.sock')
    server.start()
    event = None
    def query(command, **kw):
        value = daemon_command(command, path=server.path, **kw)
        if race:
            state.submit(hook_report('codex', {**event, 'hook_event_name': 'UserPromptSubmit', 'prompt': 'Implement the plan.'}))
        return value
    forwarder = StatusForwarder('wrap-test', command=['codex'], query=query,
        submit=lambda report, **kw: submit_report(report, path=server.path, **kw))
    try:
        event = pending(tmp_path, state)
        forwarder.start()
        forwarder.publish({'state': 'blocked', 'kind': 'question', 'app': 'codex'})
        wait_for(lambda: len(state.statuses()) == 2)
        if previous == 'clear':
            # Real Codex clears its terminal scope while displaying the plan
            # picker, then emits idle when No or Escape dismisses that picker.
            forwarder.publish({'state': 'clear'})
            wait_for(lambda: len(state.statuses()) == 1)
        forwarder.publish({'state': 'idle', 'app': 'codex'})
        wanted = 'working' if race else 'completed'
        wait_for(lambda: next(s for s in state.statuses() if s.session_id == event['session_id']).mode.value == wanted)
        root = next(s for s in state.statuses() if s.session_id == event['session_id'])
        assert root.event_name == ('UserPromptSubmit' if race else 'PlanDismissed')
        if not race:
            receiver = ProgramState()
            try:
                for _, update in state.pending_events('receiver'):
                    receiver.receive_update('desktop', update)
                assert next(s for s in receiver.statuses() if s.session_id == event['session_id']).mode.value == 'completed'
            finally:
                receiver.close()
    finally:
        forwarder.close()
        server.stop()
        directory.cleanup()
        state.close()


@pytest.mark.parametrize('case', ['ambiguous', 'other-directory', 'before-wrapper', 'other-publisher', 'new-question', 'ordinary-question'])
def test_dialog_closure_does_not_guess_an_unrelated_or_ambiguous_hook_scope(tmp_path, monkeypatch, case):
    from sidepulse.codex_terminal import CodexTerminal
    from sidepulse.program_presentation import ProgramMonitor
    monkeypatch.chdir(tmp_path)
    state = ProgramState()
    terminal = CodexTerminal(['codex'], 'wrap-test')
    try:
        event = pending(tmp_path, state)
        state.submit({'v': 1, 'type': 'program_status', 'scope': 'wrap-test', 'status': {'state': 'blocked', 'app': 'codex'}})
        rows = ProgramMonitor(state).snapshot().to_dict()['statuses']
        root = next(r for r in rows if r['session_id'] == event['session_id'])
        if case == 'ambiguous':
            rows.append({**root, 'session_link': root['session_link'] + '-another', 'session_id': 'other'})
        elif case == 'other-directory':
            root['cwd'] = str(tmp_path / 'elsewhere')
        elif case == 'before-wrapper':
            terminal.started_at = time.time() + 5
        elif case == 'other-publisher':
            root['session_link'] = root['session_link'].replace(state.publisher, 'different-publisher')
        elif case == 'new-question':
            root['event_name'] = 'PreToolUse'
        elif case == 'ordinary-question':
            root['message'] = 'Would you like me to continue?'
        terminal.query = lambda *a, **k: {'snapshot': {'statuses': rows}}
        submitted = []
        terminal.dismiss_plan(lambda *a, **k: submitted.append(a))
        assert submitted == []
    finally:
        state.close()
