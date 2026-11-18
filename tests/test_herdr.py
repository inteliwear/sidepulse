import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from sidepulse import herdr
from sidepulse.program_adapter import hook_report
from sidepulse.program_status import ProgramState, session_link_identity
from sidepulse.program_transport import ProgramServer, daemon_command
from sidepulse.terminal_wrap import StatusForwarder

pytestmark = pytest.mark.skipif(sys.platform == 'win32', reason='Herdr uses Unix sockets')


@pytest.fixture
def integration(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path))
    monkeypatch.setenv('HERDR_SOCKET_PATH', str(tmp_path / 'herdr.sock'))
    monkeypatch.setenv('HERDR_PANE_ID', 'w1:p1')
    monkeypatch.setenv('HERDR_PLUGIN_STATE_DIR', str(tmp_path))
    monkeypatch.delenv('HERDR_PLUGIN_EVENT_JSON', raising=False)
    state = ProgramState(publisher='test-publisher')
    server = ProgramServer(state)
    server.start()
    agent = {'terminal_id': 'term-1', 'pane_id': 'w1:p1', 'agent': 'claude',
             'agent_status': 'working', 'agent_session': {'kind': 'id', 'value': 'native'},
             'cwd': str(tmp_path), 'terminal_title_stripped': 'My agent'}
    snapshot = {'agents': [agent], 'panes': [agent]}

    def request(endpoint, method, timeout, **params):
        assert endpoint == os.environ['HERDR_SOCKET_PATH']
        if method == 'pane.get':
            assert params['pane_id'] == 'w1:p1'
            return {'pane': agent}
        assert method == 'session.snapshot'
        return {'snapshot': snapshot}

    monkeypatch.setattr(herdr, '_request', request)
    try:
        yield state, agent, snapshot
    finally:
        server.stop()
        state.close()


def _scope(row):
    return session_link_identity({'session_link': row.session_link})[1]


def _hook(session='native'):
    return hook_report('claude', {'session_id': session, 'hook_event_name': 'PermissionRequest',
                                 'tool_name': 'Bash', 'tool_input': {'command': 'pwd'}})


@pytest.mark.parametrize('direct_first', [False, True])
def test_hook_and_herdr_share_one_forwarded_scope(integration, direct_first):
    state, agent, snapshot = integration
    hook = _hook()
    if direct_first:
        state.submit(hook)
    herdr.sync()
    if not direct_first:
        state.submit(hook)
    row, = state.statuses()
    assert row.session_id == 'native'
    assert row.mode.value == 'waiting_for_input'
    assert _scope(row) == hook['scope']

    # The shell keeps its inherited pane alias after moving across workspaces.
    agent['pane_id'] = 'w2:p3'
    agent['agent_status'] = 'idle'
    herdr.sync()
    assert state.statuses() == (row,)
    (_, update), = state.pending_events('phone')
    assert update['scope'] == hook['scope']
    assert update['status']['kind'] == 'permission'

    snapshot['panes'].clear()
    snapshot['agents'].clear()
    herdr.sync()
    assert not state.statuses()
    (_, update), = state.pending_events('phone')
    assert update['status'] == {'state': 'clear'}


def test_herdr_reports_unhooked_agents_and_clears_released_pane(integration):
    state, agent, snapshot = integration
    agent.update(agent='pi', agent_session=None, agent_status='blocked')
    herdr.sync()
    row, = state.statuses()
    assert row.provider == 'pi'
    assert row.mode.value == 'waiting_for_input'
    herdr.sync()
    assert state.statuses() == (row,)

    agent['agent_status'] = 'done'
    herdr.sync()
    assert state.statuses()[0].mode.value == 'completed'
    snapshot['agents'].clear()
    herdr.sync()
    assert not state.statuses()


def test_release_event_clears_direct_status(integration, monkeypatch):
    state, _, snapshot = integration
    state.submit(_hook())
    snapshot['agents'].clear()
    # A temporarily undetected agent can still be reporting via direct hooks.
    herdr.sync()
    assert state.statuses()
    monkeypatch.setenv('HERDR_PLUGIN_EVENT_JSON',
                       '{"event":"pane_agent_detected","data":{"pane_id":"w1:p1","released":true}}')
    herdr.sync()
    assert not state.statuses()


def test_reused_pane_does_not_keep_direct_status_for_previous_agent(integration):
    state, agent, _ = integration
    state.submit(_hook())
    agent.update(agent_session={'kind': 'id', 'value': 'next'})
    herdr.sync()
    row, = state.statuses()
    assert row.session_id == 'next'
    assert row.mode.value == 'working'


def test_wrapped_osc_uses_pane_scope(integration):
    state, _, _ = integration
    herdr.sync()
    forwarder = StatusForwarder('wrap-test', submit=lambda value, **kwargs: state.submit(value))
    forwarder.start()
    try:
        forwarder.publish({'app': 'claude-code', 'state': 'error', 'msg': 'More detail'})
    finally:
        forwarder.close()
    row, = state.statuses()
    assert _scope(row) == herdr.current_pane_scope()
    assert row.message == 'More detail'
    state.submit(_hook())
    herdr.sync()
    row, = state.statuses()
    assert row.mode.value == 'waiting_for_input'


def test_outside_hook_and_named_servers_are_independent(integration, monkeypatch):
    state, _, _ = integration
    herdr.sync()
    local_scope = herdr.current_pane_scope()
    socket_path = os.environ['HERDR_SOCKET_PATH']
    monkeypatch.setenv('HERDR_SOCKET_PATH', socket_path + '.other')
    herdr.sync()
    assert local_scope != herdr.current_pane_scope()
    monkeypatch.delenv('HERDR_PANE_ID')
    state.submit(_hook())
    assert len(state.statuses()) == 3


def test_native_codex_idle_releases_only_its_own_pane_plan(integration, monkeypatch):
    state, agent, _ = integration
    monkeypatch.chdir(agent['cwd'])
    scope = herdr.current_pane_scope()
    other_scope = herdr.terminal_scope(herdr.server_prefix(os.environ['HERDR_SOCKET_PATH']), 'other')
    dismissed = threading.Event()

    def submit(value, **kwargs):
        reply = state.submit(value)
        if value.get('context', {}).get('event_name') == 'PlanDismissed':
            dismissed.set()
        return reply

    forwarder = StatusForwarder('wrap-test', command=['codex'], query=daemon_command, submit=submit)
    for pane in (scope, other_scope):
        state.submit({'v': 1, 'type': 'program_status', 'scope': pane,
                      'status': {'state': 'blocked', 'kind': 'question', 'app': 'codex'},
                      'context': {'provider': 'codex', 'session_id': pane,
                                  'cwd': agent['cwd'], 'event_name': 'Stop'}})
    forwarder.start()
    try:
        forwarder.publish({'app': 'codex', 'state': 'idle'})
        assert dismissed.wait(2)
    finally:
        forwarder.close()
    assert {_scope(row): row.mode.value for row in state.statuses()} == {
        scope: 'completed', other_scope: 'waiting_for_input',
    }


@pytest.fixture
def setup_service(monkeypatch):
    from sidepulse import service_launch

    calls = []

    def start_service(**kwargs):
        calls.append('service')
        return SimpleNamespace(path=Path('/test/service'), started=True, detail='')

    monkeypatch.setattr(service_launch, 'install_service', start_service)
    return calls


@pytest.mark.parametrize('selection', [['herdr'], ['--agent', 'herdr']])
@pytest.mark.parametrize('running', [False, True])
def test_setup_registers_plugin_and_syncs_only_a_running_server(selection, running, setup_service, monkeypatch, capsys):
    from sidepulse.cli import sidepulse_main

    monkeypatch.setenv('HERDR_BIN_PATH', '/path/to/herdr')

    def cli(executable, *args):
        assert executable == '/path/to/herdr'
        setup_service.append(args)
        return '{"running": true}' if running else '{"running": false}'

    monkeypatch.setattr(herdr, '_cli', cli)
    assert sidepulse_main(['setup', *selection, '--no-sd-eject-guard', '--no-status-bar']) == 0
    assert setup_service[:3] == ['service', ('plugin', 'link', str(herdr.PLUGIN_DIR)), ('status', 'server', '--json')]
    assert setup_service[3:] == ([('plugin', 'action', 'invoke', 'sidepulse.status.sync')] if running else [])
    assert ('Initial sync requested' if running else 'sync when Herdr starts') in capsys.readouterr().out


@pytest.mark.parametrize('selection', [[], ['--agent', 'codex', '--agent', 'herdr']])
def test_setup_returns_failure_when_herdr_registration_fails(selection, setup_service, monkeypatch, capsys):
    from sidepulse import cli

    def fail(**kwargs):
        setup_service.append('plugin')
        raise herdr.StatusError('registration rejected')

    monkeypatch.setattr(cli, 'install_hook_results', lambda args: [])
    monkeypatch.setattr(herdr, 'executable_path', lambda: '/test/herdr')
    monkeypatch.setattr(herdr, 'configure_plugin', fail)
    assert cli.sidepulse_main(['setup', *selection, '--no-sd-eject-guard', '--no-status-bar']) == 1
    assert setup_service == ['service', 'plugin']
    assert 'registration rejected' in capsys.readouterr().err


@pytest.mark.parametrize(('selection', 'exit_code'), [([], 0), (['herdr'], 1), (['--agent', 'herdr'], 1)])
def test_setup_skips_missing_herdr_only_when_it_is_not_explicitly_selected(selection, exit_code, setup_service, monkeypatch):
    from sidepulse import cli

    monkeypatch.setattr(cli, 'install_hook_results', lambda args: [])
    monkeypatch.setattr(herdr, 'executable_path', lambda: None)
    assert cli.sidepulse_main(['setup', *selection, '--no-sd-eject-guard', '--no-status-bar']) == exit_code
    assert setup_service == ['service']
