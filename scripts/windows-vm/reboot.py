#!/usr/bin/env python3
"""Verify real Windows reboot and sign-in startup without starting the service."""
import argparse
import hashlib
import importlib.util
import json
import os
import re
from importlib.metadata import distribution
from pathlib import Path
import subprocess
import sys
import time

spec = importlib.util.spec_from_file_location('windows_probe', Path(__file__).with_name('probe.py'))
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def boot_id():
    return subprocess.check_output(['powershell.exe', '-NoProfile', '-Command',
        '(Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().ToString("o")'],
        text=True, timeout=30).strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['stamp', 'check'])
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--run-id', default='', help='Keep separate reboot baselines for successive frozen inputs')
    args = parser.parse_args()
    if args.run_id and not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', args.run_id):
        parser.error('Use a short alphanumeric reboot run ID')
    probe.guard('vm')
    root = args.root.resolve()
    if Path.home() not in root.parents or not root.name.startswith('SidePulse'):
        raise RuntimeError('Use only a SidePulse probe root beneath the disposable test profile')
    evidence = root / 'evidence'
    evidence.mkdir(exist_ok=True)
    suffix = '-' + args.run_id if args.run_id else ''
    stamp = evidence / ('boot-before' + suffix + '.json')
    result_path = evidence / ('reboot' + suffix + '.json')
    current = boot_id()
    python = root / 'venv/Scripts/python.exe'
    sha = hashlib.sha256(python.read_bytes()).hexdigest()
    if Path(sys.executable).resolve() != python.resolve():
        raise RuntimeError('Use the installed probe interpreter for reboot verification')
    package = distribution('sidepulse')
    product_files = {str(path): hashlib.sha256(package.locate_file(path).read_bytes()).hexdigest()
        for path in package.files or () if str(path).endswith('.py')
        and path.parts[0] in {'sidepulse', 'sidepulse_cli', 'agent_monitor'}}
    if not product_files:
        raise RuntimeError('No installed product files found')
    if args.command == 'stamp':
        with stamp.open('x', encoding='utf-8') as stream:
            json.dump({'boot_id': current, 'at': time.time(), 'python': str(python), 'python_sha256': sha, 'product_files': product_files}, stream, indent=2)
        print('Reboot baseline saved. Close the completed test consoles before a normal reboot.')
        return 0
    before = json.loads(stamp.read_text())
    result = {'scope': 'native-windows-reboot', 'before_boot_id': before['boot_id'],
              'after_boot_id': current, 'at': time.time(), 'checks': [],
              'service_start_called': False}

    def check(name, action):
        try:
            action()
        except Exception as exc:
            result['checks'].append({'name': name, 'status': 'failed', 'error': str(exc)})
        else:
            result['checks'].append({'name': name, 'status': 'passed'})
        result_path.write_text(json.dumps(result, indent=2) + '\n')

    def changed():
        assert current and before['boot_id'] != current, 'Kernel boot identity did not change'
        assert before['python_sha256'] == sha, 'Installed interpreter changed across reboot'
        assert before.get('product_files') == product_files, 'Installed product changed across reboot'
    check('real-os-reboot-and-same-interpreter', changed)
    installation = probe.Probe(root, 'unit')
    os.environ.update(installation.env)
    from sidepulse.program_transport import daemon_command, status_socket_path
    from sidepulse.windows_service import paths, RUN_KEY

    def startup():
        import winreg
        directory, name = paths()
        config = json.loads((directory / 'service.json').read_text(encoding='utf-8'))
        assert Path(config['command'][0]).resolve() == python.resolve(), 'Startup uses another interpreter'
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            command = winreg.QueryValueEx(key, name)[0]
        assert command == subprocess.list2cmdline(config['launcher']), 'Startup registration differs from the installed launcher'
        deadline = time.monotonic() + 90
        while True:
            try:
                reply = daemon_command({'type': 'get_state'}, timeout=.5, path=status_socket_path())
                assert reply['ok'] is True and 'snapshot' in reply
                return
            except (OSError, ValueError):
                if time.monotonic() >= deadline:
                    raise RuntimeError('The registered service did not respond after sign-in; no substitute was started')
                time.sleep(.5)
    check('registered-user-logon-startup', startup)
    check('installed-product-unchanged', installation.source_match)
    check('settings-hooks-and-interpreter-preserved', installation.migration)
    result['failures'] = sum(c['status'] == 'failed' for c in result['checks'])
    result_path.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
    return int(bool(result['failures']))


if __name__ == '__main__':
    raise SystemExit(main())
