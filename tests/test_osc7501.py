"""OSC ingress uses the same durable JSON path as hooks and third-party reports."""
import base64
import io
import json
import os
import subprocess
import sys
from unittest.mock import patch

import pytest

from sidepulse.osc7501 import parse_osc7501
from sidepulse.program_status import ProgramState, StatusError


def osc(body, terminator='\x1b\\'):
    return '\x1b]7501;' + body + terminator


def b64(value):
    return base64.b64encode(value.encode()).decode()


@pytest.mark.parametrize('terminator', ['\x1b\\', '\x07', ''])
@pytest.mark.parametrize('newline', ['', '\n', '\r\n'])
def test_wire_framing_and_plain_unicode_text(terminator, newline):
    status = parse_osc7501(osc(
        'state=blocked:kind=permission:app=deploy:id=release/approval:'
        f'title={b64("Release ✓")}:msg={b64("Deploy: café?").rstrip("=")}:progress=40',
        terminator) + newline)
    assert status == {'state': 'blocked', 'kind': 'permission', 'app': 'deploy',
                      'id': 'release/approval', 'title': 'Release ✓', 'msg': 'Deploy: café?', 'progress': 40}


@pytest.mark.parametrize('body,expected', [
    ('state=working:state=done:progress=30:kind=permission', {'state': 'done'}),
    ('state=working:progress=00100', {'state': 'working', 'progress': 100}),
    ('state=working:progress=42:progress=wrong', {'state': 'working'}),
    ('state=working:progress=101', {'state': 'working'}),
    ('state=working:progress=-1', {'state': 'working'}),
    ('state=working:progress=1.5', {'state': 'working'}),
    ('state=blocked:kind=unknown:app=a/b', {'state': 'blocked'}),
    (' state = working : broken : =x : x=bad! : State=done : future=a/b', {'state': 'working'}),
    ('state=working:state=bad!', {'state': 'working'}),
    ('state=idle:msg=:title=', {'state': 'idle', 'msg': '', 'title': ''}),
    ('state=clear:id=build/test', {'state': 'clear', 'id': 'build/test'}),
    ('state=error', {'state': 'error'}),
])
def test_spec_pair_and_optional_field_rules(body, expected):
    assert parse_osc7501(osc(body)) == expected


@pytest.mark.parametrize('line', [
    '', 'state=working', '7501;state=working', r'\e]7501;state=working\e\\',
    '\x1b]7501;state=working\x1b', '\x1b]9;state=working\x07',
    'terminal output' + osc('state=working'), osc('state=working') + 'output',
    osc('state=working') + osc('state=done'), osc('state=working') + '\n\n',
    osc('state=working') + '\r',
    osc('state=working', '') + '\n\n', osc('state=working', '') + '\noutput',
    osc('state=working', '') + osc('state=done', ''),
    osc('?'), osc('?:states=working'), osc('msg=SGk='), osc('state=unknown'),
    osc('state=working:msg=a'), osc('state=working:msg=____'),
    osc('state=working:msg=/w=='), osc('state=working:msg=YQ===:msg=SGk='),
    osc('state=working:id=a//b'), osc('state=working:id=/a'),
    osc('state=working:id=a/b/c/d/e/f/g/h/i:id=valid'),
    osc('state=working:id=' + 'x' * 33 + ':id=valid'),
    osc('state=working:app=' + 'x' * 33 + ':app=valid'),
    osc('state=working:' + 'x' * 17 + '=ignored'),
    osc('state=working:msg=' + 'A' * 2733 + ':msg=SGk='),
    osc('state=working:title=' + 'A' * 257),
    osc('state=working:unknown=' + 'x' * 4096),
])
def test_invalid_sequence_is_rejected_as_a_whole(line):
    with pytest.raises(StatusError):
        parse_osc7501(line)


@pytest.mark.parametrize('value', ['\n', '\t', '\x00', '\x1b', '\x7f', '\x85', '\x9f'])
def test_control_text_rejected_even_before_a_duplicate(value):
    with pytest.raises(StatusError):
        parse_osc7501(osc(f'state=working:msg={b64(value)}:msg=SGk='))


def test_byte_limits_and_no_mutation_on_rejection(tmp_path):
    message, title = 'é' * 1024, 'é' * 96
    status = parse_osc7501(osc(f'state=working:msg={b64(message)}:title={b64(title)}'))
    assert status['msg'] == message and status['title'] == title
    for field, value in [('msg', message + 'a'), ('title', title + 'a')]:
        with pytest.raises(StatusError):
            parse_osc7501(osc(f'state=working:{field}={b64(value)}'))
    body = 'state=working:ignored='
    exact = osc(body + 'x' * (4096 - len(osc(body))))
    assert parse_osc7501(exact) == {'state': 'working'}
    with pytest.raises(StatusError):
        parse_osc7501(exact.replace('ignored=', 'ignored=x'))

    from sidepulse.cli import sidepulse_main
    store = ProgramState()
    try:
        with patch('sidepulse.program_transport.submit_status', side_effect=store.submit):
            assert sidepulse_main(['report', '--scope', 'job', '--osc', osc('state=working')]) == 0
            before = store.statuses(), store.pending_events('phone')
            assert sidepulse_main(['report', '--scope', 'job', '--osc', osc('state=done:msg=bad')]) == 1
            assert (store.statuses(), store.pending_events('phone')) == before
    finally:
        store.close()


@pytest.mark.parametrize('flags,input_value', [
    (['--osc', osc('state=blocked:kind=permission:msg=SGk=')], None),
    (['--osc'], osc('state=blocked:kind=permission:msg=SGk=', '\x07') + '\n'),
    (['--osc', '-'], osc('state=blocked:kind=permission:msg=SGk=')),
    (['--osc', osc('state=blocked:kind=permission:msg=SGk=', '')], None),
    (['--osc'], osc('state=blocked:kind=permission:msg=SGk=', '') + '\n'),
    (['--osc', '-'], osc('state=blocked:kind=permission:msg=SGk=', '')),
])
def test_cli_without_daemon_reports_unavailable_and_creates_no_database(tmp_path, flags, input_value):
    env = dict(os.environ, HOME=str(tmp_path / 'home'), XDG_STATE_HOME=str(tmp_path / 'state'))
    command = [sys.executable, '-c', 'from sidepulse.cli import sidepulse_main; raise SystemExit(sidepulse_main())']
    args = command + ['report', '--scope', 'job', '--request-id', 'same-request'] + flags
    first = subprocess.run(args, input=input_value, env=env, capture_output=True, text=True)
    assert first.returncode == 1
    assert 'daemon unavailable' in first.stderr.lower()
    assert not list(tmp_path.rglob('*.sqlite3'))



@pytest.mark.parametrize('flags', [
    ['--osc', osc('state=working')],
    ['--scope', 'job', '--osc', osc('state=working'), '--msg', 'override'],
    ['--scope', 'job', '--osc', osc('state=working'), '--state', 'done'],
    ['--scope', 'job', '--osc', osc('state=working'), '--json'],
    ['--scope', 'job', '--end'],
])
def test_cli_rejects_ambiguous_input_before_submission(flags):
    from sidepulse.cli import sidepulse_main
    with patch('sidepulse.program_transport.submit_status') as submit:
        try:
            assert sidepulse_main(['report'] + flags) == 1
        except SystemExit as exc:
            assert exc.code == 2
        submit.assert_not_called()


def test_cli_bounds_stdin_and_returns_error_for_invalid_utf8():
    from sidepulse.cli import sidepulse_main
    for value in [osc('state=working:ignored=' + 'x' * 5000), '\ud800']:
        with patch('sys.stdin', io.StringIO(value)), patch('sidepulse.program_transport.submit_status') as submit:
            assert sidepulse_main(['report', '--scope', 'job', '--osc']) == 1
            submit.assert_not_called()
