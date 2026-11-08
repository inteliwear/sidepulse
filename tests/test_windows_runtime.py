"""Windows runtime checks, including real cross-process and authenticated IPC."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import types
import time

import pytest

from sidepulse import install
from sidepulse.hook_commands import observer_script
from sidepulse.providers import extract_log_paths_from_command


def test_windows_hook_encoding_round_trips_paths_and_only_replaces_managed_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(install, 'os', types.SimpleNamespace(name='nt', environ=os.environ))
    log = tmp_path / "space & O'Brien" / 'claude.jsonl'
    config = tmp_path / 'settings.json'
    config.write_text(json.dumps({'hooks': {'Stop': [{'hooks': [
        {'type': 'command', 'command': 'echo keep-user-hook'}]}]}}))
    install.install_claude_hooks(log, config, python_executable="C:\\Python & O'Brien\\python.exe")
    install.install_claude_hooks(log, config, python_executable="C:\\Python & O'Brien\\python.exe")
    data = json.loads(config.read_text())
    commands = [h['command'] for entry in data['hooks']['Stop'] for h in entry['hooks']]
    assert commands[0] == 'echo keep-user-hook'
    assert len(commands) == 2
    assert "O''Brien" in observer_script(commands[1])
    assert list(extract_log_paths_from_command(commands[1])) == [log]


def test_windows_codex_reinstall_removes_encoded_managed_blocks(tmp_path, monkeypatch):
    monkeypatch.setattr(install, 'os', types.SimpleNamespace(name='nt', environ=os.environ))
    config, log = tmp_path / 'config.toml', tmp_path / 'codex.jsonl'
    install.install_codex_hooks(log, config)
    install.install_codex_hooks(log, config)
    from sidepulse.providers import tomllib
    data = tomllib.loads(config.read_text())
    for event in install.CODEX_EVENTS:
        assert len(data['hooks'][event]) == 1
        assert len(data['hooks'][event][0]['hooks']) == 1
        assert 'hook_entry.py' in observer_script(data['hooks'][event][0]['hooks'][0]['command'])


windows = pytest.mark.skipif(sys.platform != 'win32', reason='Requires native Windows APIs')


@windows
def test_windows_device_lock_is_owned_by_the_process(tmp_path):
    from sidepulse.windows_support import file_lock
    path = tmp_path / 'lock'
    code = '''from pathlib import Path
from sidepulse.windows_support import file_lock
import sys
try:
    with file_lock(Path(sys.argv[1]), blocking=False): pass
except OSError: raise SystemExit(7)
'''
    with file_lock(path):
        assert subprocess.run([sys.executable, '-c', code, str(path)]).returncode == 7
    assert subprocess.run([sys.executable, '-c', code, str(path)]).returncode == 0


@windows
def test_windows_local_server_rejects_unauthenticated_clients_and_duplicate_owner(tmp_path):
    from sidepulse.program_status import ProgramState, StatusError
    from sidepulse.program_transport import ProgramServer, daemon_command
    path = tmp_path / 'private' / 'program-status.sock'
    server = ProgramServer(ProgramState(publisher='00000000-0000-0000-0000-000000000001'), path)
    server.start()
    try:
        assert daemon_command({'type': 'get_state'}, path=path)['ok'] is True
        endpoint = json.loads(path.read_text())
        with socket.create_connection(('127.0.0.1', endpoint['port']), timeout=1) as client:
            client.sendall(json.dumps({'v': 1, 'type': 'get_state'}).encode())
            client.shutdown(socket.SHUT_WR)
            assert json.loads(client.recv(4096))['ok'] is False
        duplicate = ProgramServer(ProgramState(publisher='00000000-0000-0000-0000-000000000001'), path)
        with pytest.raises(StatusError, match='already running'):
            duplicate.start()
        assert daemon_command({'type': 'get_state'}, path=path)['ok'] is True
        # The credential inherits only the user and SYSTEM, not Everyone or Users.
        acl = subprocess.check_output(['icacls.exe', str(path)], text=True)
        assert 'S-1-1-0' not in acl and 'BUILTIN\\Users:' not in acl and 'Everyone:' not in acl
    finally:
        server.stop()
    assert not path.exists()


@windows
def test_windows_generated_hook_executes_and_fails_open_when_python_disappears(tmp_path):
    log = tmp_path / "space & O'Brien" / 'claude.jsonl'
    log.parent.mkdir()
    payload = json.dumps({'hook_event_name': 'UserPromptSubmit', 'session_id': 'native-hook-test',
                          'cwd': str(tmp_path), 'prompt': 'Windows hook test'})
    command = install.windows_hook_command(sys.executable, 'claude', log)
    result = subprocess.run(command, shell=True, input=payload, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert log.is_file() and 'native-hook-test' in log.read_text()
    command = install.windows_hook_command(str(tmp_path / 'missing/python.exe'), 'claude', log)
    assert subprocess.run(command, shell=True, input=payload, text=True, capture_output=True, timeout=15).returncode == 0


@windows
def test_windows_conpty_preserves_osc_and_child_exit_status(tmp_path):
    # A real ConPTY child emits one completed OSC state. The wrapper must relay
    # its bytes and return the actual exit code, without needing a daemon.
    child = "import sys; print('conpty-marker'); print('\\x1b]7501;state=done:app=windows-test\\x1b\\\\', flush=True); sys.exit(7)"
    result = subprocess.run([sys.executable, '-m', 'sidepulse', 'wrap', '--', sys.executable, '-c', child],
                            capture_output=True, timeout=60)
    assert result.returncode == 7, result.stderr.decode(errors='replace')
    assert b'conpty-marker' in result.stdout
    assert b'\x1b]7501;state=done:app=windows-test' in result.stdout


def test_windows_terminal_environment_identifies_native_cli(monkeypatch):
    from sidepulse import origin
    monkeypatch.setattr(origin, 'process_ancestry', lambda _: ())
    result = origin.detect_agent_origin('codex', env={'WT_SESSION': 'test-terminal-session'})
    assert result.kind == 'codex_cli'
    assert result.source == 'env:WT_SESSION'


@windows
def test_windows_conpty_answers_capability_query_inside_the_child():
    child = r'''import ctypes as c, os, sys
from ctypes import wintypes as w
k=c.WinDLL('kernel32',use_last_error=True)
k.GetStdHandle.argtypes=[w.DWORD];k.GetStdHandle.restype=w.HANDLE
k.SetConsoleMode.argtypes=[w.HANDLE,w.DWORD]
h=k.GetStdHandle((-10)&0xffffffff)
assert k.SetConsoleMode(h,0x200)
os.write(1,b'\x1b]7501;?\x1b\\')
reply=os.read(0,4096)
print('reply='+repr(reply),flush=True)
assert b'states=idle,working,done,blocked,error' in reply
'''
    result = subprocess.run([sys.executable, '-m', 'sidepulse', 'wrap', '--', sys.executable, '-c', child],
                            capture_output=True, timeout=60)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert b'reply=' in result.stdout
    assert b'\x1b]7501;?\x1b\\' not in result.stdout


@windows
def test_windows_nested_conpty_relays_keyboard_bytes_without_protocol_text(tmp_path):
    from winpty import PTY, Backend
    # A second real ConPTY supplies an interactive console to the wrapper.
    # Redirected stdin cannot expose Win32-input-mode corruption in getwch().
    child = tmp_path / 'keyboard_child.py'
    child.write_text(r'''import ctypes as c, os, sys
from ctypes import wintypes as w
k=c.WinDLL('kernel32',use_last_error=True)
k.GetStdHandle.argtypes=[w.DWORD];k.GetStdHandle.restype=w.HANDLE
k.SetConsoleMode.argtypes=[w.HANDLE,w.DWORD]
assert k.SetConsoleMode(k.GetStdHandle((-10)&0xffffffff),0x200)
k.SetConsoleCP(65001)
os.write(1,b'KEYBOARD_READY\r\n')
expected='A\u96ea\x1b[A\r'.encode('utf-8')
data=b''
while len(data)<len(expected): data+=os.read(0,4096)
assert data==expected,repr(data)
os.write(1,b'KEYBOARD_EXACT\r\n')
sys.exit(7)
''')
    terminal = PTY(100, 30, backend=Backend.ConPTY)
    terminal.spawn(sys.executable, cmdline=' ' + subprocess.list2cmdline([
        '-m', 'sidepulse', 'wrap', '--', sys.executable, str(child)]))
    output, sent = '', False
    deadline = time.monotonic() + 60
    try:
        while time.monotonic() < deadline:
            output += terminal.read(blocking=False) or ''
            if 'KEYBOARD_READY' in output and not sent:
                terminal.write('A\u96ea\x1b[A\r')
                sent = True
            if terminal.iseof() or not terminal.isalive():
                break
            time.sleep(.01)
        assert sent, output
        assert not terminal.isalive(), 'Interactive wrapper did not exit: ' + output
        assert terminal.get_exitstatus() == 7, output
        assert 'KEYBOARD_EXACT' in output, output
    finally:
        if terminal.isalive():
            os.kill(terminal.pid, 15)


def test_device_program_preserves_lf_bytes(tmp_path):
    from sidepulse.device_writer import write_text_synced
    target = tmp_path / 'LEDS.LED'
    program = '#FF0000\n100ms\nrepeat\n'
    write_text_synced(target, program)
    assert target.read_bytes() == program.encode('utf-8')


@windows
def test_windows_pairing_and_relay_files_are_private_from_creation(tmp_path):
    from sidepulse.links import IOSLink, save_ios_links
    from sidepulse.relay import ensure_receiver_config
    from privacy import assert_private_file
    links, relay = tmp_path / 'credentials' / 'links.json', tmp_path / 'relay' / 'relay.json'
    save_ios_links([IOSLink('fixture', 'a' * 64)], links)
    ensure_receiver_config(relay)
    for path in (links, relay):
        assert_private_file(path)
