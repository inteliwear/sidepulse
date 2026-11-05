"""Exercise stream framing and real PTY behavior without touching live devices."""
from __future__ import annotations

import os
import select
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from sidepulse.cli import sidepulse_main
from sidepulse.program_status import ProgramState
from sidepulse.program_transport import ProgramServer
from sidepulse.terminal_wrap import CAPABILITY_REPLY, OscCapture, StatusForwarder


def sequence(body, end=b'\x1b\\'):
    return b'\x1b]7501;' + body + end


@pytest.mark.parametrize('end', [b'\x1b\\', b'\x07'])
def test_every_stream_split_preserves_binary_output_and_captures_reports(end):
    working = sequence(b'state=working:app=demo:progress=42', end)
    done = sequence(b'state=done:msg=Y2Fmw6k=', end)
    data = b'\xff\x1b[31mhello' + working + b'\r\n' + done + b'\x1b[0m'
    for split in range(len(data) + 1):
        statuses, replies = [], []
        scanner = OscCapture(statuses.append, replies.append)
        assert scanner.feed(data[:split]) + scanner.feed(data[split:]) + scanner.finish() == data
        assert statuses == [{'state': 'working', 'app': 'demo', 'progress': 42}, {'state': 'done', 'msg': 'café'}]
        assert not replies


def test_queries_are_answered_once_without_reaching_outer_terminal():
    statuses, replies = [], []
    scanner = OscCapture(statuses.append, replies.append)
    data = b'a' + sequence(b'?') + b'b' + sequence(b'?', b'\x07') + b'c'
    assert b''.join(scanner.feed(bytes([byte])) for byte in data) == b'abc'
    assert replies == [CAPABILITY_REPLY, CAPABILITY_REPLY]
    assert not statuses


@pytest.mark.parametrize('data', [
    sequence(b'state=working:msg=a'),
    sequence(b'state=working:title=AA=='),
    sequence(b'state=unknown'),
    sequence(b'state=working:app=\xff'),
    sequence(b'?:states=working'),
    b'\x1b]7501;state=working',  # Never accept an unterminated stream report.
    b'\x1b]0;title ' + sequence(b'state=blocked'),
    b'\x1bPtmux;\x1b' + sequence(b'state=blocked') + b'\x1b\\',
    b'\x1b_hidden ' + sequence(b'state=blocked') + b'\x1b\\',
])
def test_invalid_incomplete_and_nested_sequences_cannot_publish(data):
    statuses = []
    scanner = OscCapture(statuses.append, lambda _: pytest.fail('Unexpected reply'))
    assert scanner.feed(data) + scanner.finish() == data
    assert not statuses


def test_oversized_sequence_is_bounded_and_recovers_at_terminator():
    statuses = []
    scanner = OscCapture(statuses.append, lambda _: None)
    assert scanner.feed(b'\x1b]7501;state=working:msg=') == b''
    for _ in range(100):
        scanner.feed(b'A' * 1000)
        assert len(scanner.buffer) <= 4096
    scanner.feed(b'\x07' + sequence(b'state=done'))
    assert statuses == [{'state': 'done'}]


@pytest.mark.parametrize('state', ['working', 'blocked', 'idle', 'done', 'error', 'clear'])
def test_forwarder_exit_clears_only_live_states(state):
    reports = []
    worker = StatusForwarder('job', submit=lambda report, **_: reports.append(report))
    worker.start()
    worker.publish({'state': state, 'app': 'demo', 'id': 'child'})
    worker.close()
    assert reports[-1]['status']['state'] == ('clear' if state in {'working', 'blocked', 'idle'} else state)
    assert all(report['scope'] == 'job' for report in reports)
    assert not worker.thread.is_alive()


def test_slow_daemon_coalesces_without_blocking_and_keeps_final_result():
    entered, release = threading.Event(), threading.Event()
    reports = []

    def submit(report, **_):
        reports.append(report)
        entered.set()
        assert release.wait(3)

    worker = StatusForwarder('job', submit=submit)
    worker.start()
    try:
        worker.publish({'state': 'working'})
        assert entered.wait(2)
        for _ in range(10000):
            worker.publish({'state': 'working'})
        worker.publish({'state': 'done'})
        assert len(reports) == 1
    finally:
        release.set()
        worker.close()
    assert [r['status']['state'] for r in reports] == ['working', 'done']


def test_daemon_failure_warns_once_and_worker_can_accept_later_reports(capsys):
    entered = threading.Event()

    def unavailable(*_, **__):
        entered.set()
        raise OSError('Unavailable')

    worker = StatusForwarder('job', submit=unavailable)
    worker.start()
    worker.publish({'state': 'working'})
    assert entered.wait(2)
    worker.publish({'state': 'done'})
    worker.close()
    assert capsys.readouterr().err.count('will continue') == 1


@pytest.mark.parametrize('separator', [[], ['--']])
def test_cli_passes_all_child_flags_and_arguments_without_a_shell(separator):
    args = ['codex', '--model', 'example', 'a b', '$(touch nope)', '--help']
    with patch('sidepulse.terminal_wrap.run_wrapped', return_value=7) as run:
        assert sidepulse_main(['wrap', *separator, *args]) == 7
    run.assert_called_once_with(args)


@pytest.mark.parametrize('args', [[], ['--']])
def test_cli_requires_command(args, capsys):
    assert sidepulse_main(['wrap', *args]) == 2
    assert 'Usage:' in capsys.readouterr().err


LAUNCH = '''
import os
from pathlib import Path
import sidepulse.terminal_wrap as wrap
from sidepulse.program_transport import submit_report
wrap.submit_status = lambda report, **kw: submit_report(report, path=Path(os.environ['TEST_WRAP_SOCKET']), **kw)
raise SystemExit(wrap.run_wrapped(__import__('sys').argv[1:]))
'''


@pytest.fixture
def daemon(tmp_path):
    state = ProgramState(publisher='wrapper-test')
    with tempfile.TemporaryDirectory(prefix='spwrap-', dir='/tmp') as directory:
        server = ProgramServer(state, Path(directory) / 'status.sock')
        server.start()
        try:
            yield server, state
        finally:
            server.stop()
            state.close()


def wrapper_command(code):
    return [sys.executable, '-c', LAUNCH, sys.executable, '-c', code]


def wrapper_env(daemon):
    return {**os.environ, 'TEST_WRAP_SOCKET': str(daemon[0].path)}


@contextmanager
def terminal_process(code, daemon):
    import fcntl
    import termios
    master, slave = os.openpty()
    original = termios.tcgetattr(slave)
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 80, 0, 0))
    process = subprocess.Popen(wrapper_command(code), stdin=slave, stdout=slave, stderr=slave,
                               env=wrapper_env(daemon), start_new_session=True)
    try:
        yield process, master, slave, original
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        os.close(master)
        os.close(slave)


def read_until(master, marker, timeout=5):
    data = bytearray()
    deadline = time.monotonic() + timeout
    while marker not in data and time.monotonic() < deadline:
        ready, _, _ = select.select([master], [], [], 0.1)
        if ready:
            data.extend(os.read(master, 65536))
    assert marker in data, bytes(data)
    return bytes(data)


def latest_updates(state):
    return [event for _, event in state.pending_events('test')]


def assert_terminal_restored(slave, original):
    import termios
    current = termios.tcgetattr(slave)
    expected = list(original)
    # Darwin sets PENDIN when re-enabling canonical input, to reprocess any
    # pending bytes. This transient kernel bit does not change tty settings.
    current[3] &= ~getattr(termios, 'PENDIN', 0)
    expected[3] &= ~getattr(termios, 'PENDIN', 0)
    assert current == expected


def test_real_pty_query_binary_output_keyboard_resize_and_terminal_restore(daemon):
    import fcntl
    import termios
    code = r'''
import os, sys, tty
tty.setraw(0)
os.write(1, b'\x1b]7501;?\x1b\\')
reply = bytearray()
while not reply.endswith(b'\x1b\\'):
    reply.extend(os.read(0, 1))
assert bytes(reply) == b'\x1b]7501;?:states=idle,working,done,blocked,error:kinds=permission,question,auth\x1b\\', reply
assert os.get_terminal_size().columns == 80
os.write(1, b'READY')
assert os.read(0, 4) == b'ping'
assert os.get_terminal_size().columns == 100
os.write(2, b'STDERR')
for byte in b'\x1b]7501;state=done:app=demo:msg=SGVsbG8=\x1b\\':
    os.write(1, bytes([byte]))
os.write(1, b'\xffFINISHED')
sys.exit(7)
'''
    with terminal_process(code, daemon) as (process, master, slave, original):
        start = read_until(master, b'READY')
        assert b'7501;' not in start  # Query was not forwarded to the outer terminal.
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 30, 100, 0, 0))
        os.kill(process.pid, signal.SIGWINCH)
        # Wait for the child to observe the resize before it checks keyboard input.
        time.sleep(0.1)
        os.write(master, b'ping')
        output = read_until(master, b'FINISHED')
        assert b'STDERR' in output and b'\xffFINISHED' in output
        assert process.wait(timeout=5) == 7
        assert_terminal_restored(slave, original)
    updates = latest_updates(daemon[1])
    assert len(updates) == 1
    assert updates[0]['status'] == {'state': 'done', 'app': 'demo', 'msg': 'Hello'}


@pytest.mark.parametrize('interrupt,exit_code', [('keyboard', 130), (signal.SIGINT, 130),
                                               (signal.SIGTERM, 143), (signal.SIGHUP, 129)])
def test_interrupt_forwarding_clears_work_and_restores_terminal(daemon, interrupt, exit_code):
    code = r'''
import os, signal, time
signal.signal(signal.SIGINT, signal.SIG_DFL)
os.write(1, b'\x1b]7501;state=working\x07READY')
while True: time.sleep(1)
'''
    with terminal_process(code, daemon) as (process, master, slave, original):
        read_until(master, b'READY')
        if interrupt == 'keyboard':
            os.write(master, b'\x03')
        else:
            process.send_signal(interrupt)
        assert process.wait(timeout=5) == exit_code
        assert_terminal_restored(slave, original)
    assert latest_updates(daemon[1])[-1]['status'] == {'state': 'clear'}


def test_stopped_child_suspends_wrapper_and_resumes_with_terminal_restored(daemon):
    code = '''
import os, signal
os.kill(os.getpid(), signal.SIGSTOP)
os.write(1, b'RESUMED')
'''
    with terminal_process(code, daemon) as (process, master, slave, original):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            waited, status = os.waitpid(process.pid, os.WUNTRACED | os.WNOHANG)
            if waited:
                assert os.WIFSTOPPED(status)
                break
            time.sleep(0.01)
        else:
            pytest.fail('Wrapper did not suspend')
        assert_terminal_restored(slave, original)
        process.send_signal(signal.SIGCONT)
        read_until(master, b'RESUMED')
        assert process.wait(timeout=5) == 0
        assert_terminal_restored(slave, original)


def test_real_commands_without_daemon_keep_output_and_exit_code(tmp_path):
    env = {**os.environ, 'TEST_WRAP_SOCKET': str(tmp_path / 'absent.sock')}
    result = subprocess.run(wrapper_command(
        r"import os; os.write(1, b'\x1b]7501;state=done\x07output'); raise SystemExit(9)"),
        stdin=subprocess.DEVNULL, capture_output=True, env=env, timeout=5)
    assert result.returncode == 9
    assert result.stdout == sequence(b'state=done', b'\x07') + b'output'
    assert b'wrapped command will continue' in result.stderr


def test_piped_input_reaches_canonical_reader_without_echo(daemon):
    result = subprocess.run(wrapper_command('import sys; print("got:" + sys.stdin.read(), end="")'),
        input=b'hello', capture_output=True, env=wrapper_env(daemon), timeout=5)
    assert result.returncode == 0
    assert result.stdout == b'got:hello'
    assert result.stderr == b''
    assert not latest_updates(daemon[1])


def test_terminal_eof_does_not_kill_command_before_its_real_exit(daemon):
    result = subprocess.run(wrapper_command(
        'import os, time; [os.close(fd) for fd in (0, 1, 2)]; time.sleep(0.7); os._exit(11)'),
        stdin=subprocess.DEVNULL, capture_output=True, env=wrapper_env(daemon), timeout=5)
    assert result.returncode == 11


def test_distinct_invocations_keep_separate_sessions(daemon):
    for _ in range(2):
        result = subprocess.run(wrapper_command(r"import os; os.write(1, b'\x1b]7501;state=done\x07')"),
            stdin=subprocess.DEVNULL, capture_output=True, env=wrapper_env(daemon), timeout=5)
        assert result.returncode == 0
    assert len({entry['scope'] for entry in latest_updates(daemon[1])}) == 2


def test_missing_executable_returns_127_without_a_traceback(tmp_path):
    result = subprocess.run([sys.executable, '-c', LAUNCH, '/missing-sidepulse-test-command'],
        stdin=subprocess.DEVNULL, capture_output=True,
        env={**os.environ, 'TEST_WRAP_SOCKET': str(tmp_path / 'absent.sock')}, timeout=5)
    assert result.returncode == 127
    assert b'Traceback' not in result.stdout + result.stderr
