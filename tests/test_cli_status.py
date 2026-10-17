"""The terminal views share daemon data and never run a second LED writer."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import io
import json
from unittest.mock import patch

import pytest

from sidepulse.cli import sidepulse_main
from sidepulse.collector import MonitorSnapshot
from sidepulse.install import cursor_hook_command, hook_command
from sidepulse.models import AgentMode, AgentStatus, AggregateStatus


@pytest.fixture
def daemon_snapshot():
    now = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
    working = AgentStatus(provider='claude', agent_id='old-working', display_name='Long task',
                          mode=AgentMode.WORKING, updated_at=now-timedelta(hours=4), event_name='PreToolUse')
    blocked = replace(working, agent_id='approval', display_name='Approval',
                      mode=AgentMode.WAITING_FOR_INPUT, updated_at=now)
    inactive = replace(working, agent_id='inactive', display_name='Idle session',
                       mode=AgentMode.IDLE_READY, stale=True)
    snapshot = MonitorSnapshot(AggregateStatus(blocked.mode, 2, 1, blocked),
                               (working, blocked), (inactive,), (), now)
    return {'snapshot': snapshot.to_dict(), 'output_errors': {}}


def invoke(args, daemon_snapshot, *, tty=False):
    output = io.StringIO()
    with patch('sidepulse.program_transport.daemon_command', return_value=daemon_snapshot) as command, \
            patch('sys.stdout', output), patch.object(output, 'isatty', return_value=tty):
        assert sidepulse_main(args) == 0
    command.assert_called_once_with({'type': 'get_state'}, path=None)
    return output.getvalue()


@pytest.mark.parametrize('tty', [False, True])
@pytest.mark.parametrize('flags', [[], ['--all'], ['--json'], ['--all', '--json']])
def test_status_and_live_once_show_identical_daemon_data(daemon_snapshot, flags, tty):
    status = invoke(['status', '--no-color', *flags], daemon_snapshot, tty=tty)
    live = invoke(['live', '--once', '--no-color', *flags], daemon_snapshot, tty=tty)
    assert status == live
    assert '\x1b' not in status
    if '--json' in flags:
        value = json.loads(status)
        assert value == daemon_snapshot['snapshot']
        assert len(status.splitlines()) == 1
    else:
        assert 'Long task' in status  # A working session must not age out of just one view.
        assert 'Approval' in status
        assert ('Idle session' in status) == ('--all' in flags)
        assert 'Sources' not in status


def test_live_polls_same_daemon_without_ansi_in_pipes(daemon_snapshot, capsys):
    changed = json.loads(json.dumps(daemon_snapshot))
    changed['snapshot']['statuses'] = []
    with patch('sidepulse.program_transport.daemon_command', side_effect=[daemon_snapshot, changed]) as command, \
            patch('sidepulse.cli.time.sleep', side_effect=[None, KeyboardInterrupt]):
        assert sidepulse_main(['live', '--json', '--interval', '0.25']) == 0
    output = capsys.readouterr()
    frames = [json.loads(line) for line in output.out.splitlines()]
    assert frames == [daemon_snapshot['snapshot'], changed['snapshot']]
    assert '\x1b' not in output.out
    assert command.call_count == 2


def test_live_restores_terminal_cursor_when_daemon_fails(capsys):
    output = io.StringIO()
    with patch('sidepulse.program_transport.daemon_command', side_effect=OSError('daemon unavailable')), \
            patch('sys.stdout', output), patch.object(output, 'isatty', return_value=True):
        assert sidepulse_main(['live']) == 1
    assert output.getvalue().startswith('\x1b[?25l')
    assert output.getvalue().endswith('\x1b[?25h')
    assert 'daemon unavailable' in capsys.readouterr().err


@pytest.mark.parametrize('args', [
    ['agent-monitor', 'status'], ['agent-status'], ['battery', 'status'],
    ['battery', 'leds'], ['battery', 'configure'], ['leds'], ['watch'],
    ['status', '--claude-log', '/tmp/events'], ['live', '--recent-seconds', '120'],
    ['status', '--stale-after', '60'], ['live', '--max-lines', '5'],
    ['live', '--tool-running-timeout', '1'],
])
def test_removed_commands_and_offline_modes_are_rejected(args, capsys):
    with pytest.raises(SystemExit) as exc:
        sidepulse_main(args)
    assert exc.value.code == 2
    assert capsys.readouterr().err


@pytest.mark.parametrize('value', ['0', '-1', 'nan', 'inf', '-inf', 'invalid'])
def test_live_rejects_invalid_poll_intervals(value, capsys):
    with pytest.raises(SystemExit) as exc:
        sidepulse_main(['live', '--interval='+value])
    assert exc.value.code == 2
    assert 'positive finite' in capsys.readouterr().err


@pytest.mark.parametrize('command', ['status', 'live'])
def test_stopped_daemon_has_no_log_fallback(command, tmp_path, capsys):
    with patch('sidepulse.program_transport.status_socket_path', return_value=tmp_path/'absent.sock'):
        assert sidepulse_main([command, '--json']) == 1
    output = capsys.readouterr()
    assert output.out == ''
    assert 'daemon unavailable' in output.err.lower()
    assert 'Traceback' not in output.err


def test_setup_uninstall_only_removes_hooks():
    with patch('sidepulse.cli.uninstall_claude_hooks', return_value=type('Result', (), {
        'changed': True, 'provider': 'claude', 'config_path': '/tmp/config',
        'log_path': '/tmp/events', 'backup_path': None})()) as uninstall, \
            patch('sidepulse.cli.install_hook_results') as install, \
            patch('sidepulse.service_launch.install_service') as service:
        assert sidepulse_main(['setup', 'claude', '--uninstall', '--dry-run']) == 0
    uninstall.assert_called_once_with(log_path=None, dry_run=True)
    install.assert_not_called()
    service.assert_not_called()


def test_frozen_provider_and_cursor_commands_use_single_cli(tmp_path):
    with patch('sidepulse.install.sys.frozen', True, create=True):
        commands = [hook_command('claude', tmp_path/'events'),
                    cursor_hook_command('beforeShellExecution', tmp_path/'events')]
    for command in commands:
        assert ' hook-log --provider ' in command
        assert 'agent-monitor' not in command
