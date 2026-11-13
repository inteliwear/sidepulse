"""Per-user Windows startup, without elevation or stored passwords."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from .providers import default_state_dir

RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'


def paths():
    directory = default_state_dir() / 'startup'
    key = hashlib.sha256(os.path.normcase(str(directory.resolve())).encode()).hexdigest()[:16]
    return directory, 'SidePulse-' + key


def alive():
    from .program_transport import daemon_command, status_socket_path
    try:
        daemon_command({'type': 'get_state'}, timeout=.5, path=status_socket_path())
        return True
    except (OSError, ValueError):
        return False


def stop():
    from .program_transport import daemon_command, status_socket_path
    try:
        daemon_command({'type': 'service_stop'}, timeout=1, path=status_socket_path())
    except (OSError, ValueError):
        return False
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not alive():
            return True
        time.sleep(.1)
    return False


def start_registered():
    directory, _ = paths()
    config = json.loads((directory / 'service.json').read_text(encoding='utf-8'))
    # This uses the installed interpreter, even when recovery is requested by
    # another Python or a standalone app.
    with (directory / 'service.out.log').open('ab') as output, (directory / 'service.err.log').open('ab') as error:
        subprocess.Popen(config['launcher'], cwd=str(directory), stdin=subprocess.DEVNULL,
            stdout=output, stderr=error, close_fds=True,
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if alive():
            return True
        time.sleep(.1)
    return False


def install(*, start, dry_run):
    import winreg
    from .service_launch import ServiceInstallResult, service_command
    from .windows_support import private_directory
    directory, name = paths()
    path = directory / 'service.json'
    if getattr(sys, 'frozen', False):
        return ServiceInstallResult(path, False, False,
            'Windows startup requires the Python installation; run scripts/setup.ps1')
    command = service_command()
    executable = Path(command[0])
    quiet = executable.with_name('pythonw.exe')
    if executable.name.lower() == 'python.exe' and quiet.is_file():
        executable = quiet
    env = {k: v for k, v in os.environ.items() if k in {
        'HOME', 'USERPROFILE', 'XDG_CONFIG_HOME', 'XDG_STATE_HOME', 'XDG_DATA_HOME',
        'SIDEPULSE_MOUNT_ROOTS'}}
    launcher = [str(executable), str(directory / 'launch.py')]
    data = json.dumps({'command': command, 'environment': env, 'launcher': launcher}, indent=2)
    script = ("import json, os, subprocess\nfrom pathlib import Path\n"
              "config=json.loads(Path(__file__).with_name('service.json').read_text(encoding='utf-8'))\n"
              "os.environ.update(config['environment'])\n"
              "with Path(__file__).with_name('service.out.log').open('ab') as out, "
              "Path(__file__).with_name('service.err.log').open('ab') as err:\n"
              "    raise SystemExit(subprocess.call(config['command'], stdin=subprocess.DEVNULL, stdout=out, stderr=err, creationflags=subprocess.CREATE_NO_WINDOW))\n")
    changed = not path.is_file() or path.read_text(encoding='utf-8') != data
    changed |= not (directory / 'launch.py').is_file() or (directory / 'launch.py').read_text(encoding='utf-8') != script
    startup = subprocess.list2cmdline(launcher)
    if len(startup) > 260:
        return ServiceInstallResult(path, False, False, 'Windows startup command exceeds 260 characters; install into a shorter path')
    if dry_run:
        return ServiceInstallResult(path, changed, False, 'dry run')
    private_directory(directory)
    if start and alive() and not stop():
        return ServiceInstallResult(path, False, False, 'Existing service did not stop; installation retained')
    path.write_text(data, encoding='utf-8')
    (directory / 'launch.py').write_text(script, encoding='utf-8')
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, startup)
    started = start_registered() if start else False
    return ServiceInstallResult(path, changed, started,
        '' if not start or started else 'Service did not respond; inspect service.err.log beside service.json')


def restart():
    from .service_launch import ServiceInstallResult
    directory, _ = paths()
    path = directory / 'service.json'
    if not path.is_file():
        return ServiceInstallResult(path, False, False, 'Service is not installed; run sidepulse setup')
    if alive() and not stop():
        return ServiceInstallResult(path, False, False, 'Existing service did not stop')
    started = start_registered()
    return ServiceInstallResult(path, False, started, '' if started else 'Service did not respond; inspect service.err.log')
