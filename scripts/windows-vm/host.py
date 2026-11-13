#!/usr/bin/env python3
"""Register and clone only named, disposable Windows UTM test machines.

Windows Setup remains interactive. A stopped, prepared VM is the reproducible
baseline; every fresh/main test receives its own UTM clone.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import time
import uuid
import zipfile

UTMCTL = '/Applications/UTM.app/Contents/MacOS/utmctl'
KIND = 'sidepulse-disposable-windows-utm'
POWERSHELL = r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'


def call(*args):
    result = subprocess.run([UTMCTL, *map(str, args)], text=True, capture_output=True,
                            check=True, timeout=120)
    # UTM 4.6.5 can report scripting errors on stderr while returning zero.
    # A successful exit alone must not create a false VM/transfer success.
    if 'Error from event:' in result.stderr or 'Error from event:' in result.stdout:
        raise RuntimeError((result.stderr + result.stdout).strip())
    return result.stdout.strip()


def pull(identifier, path):
    result = subprocess.run([UTMCTL, 'file', 'pull', identifier, path],
                            capture_output=True, check=True, timeout=120)
    if b'Error from event:' in result.stderr:
        raise RuntimeError(result.stderr.decode('utf-8', errors='replace').strip())
    return result.stdout


def extract_evidence(data, output):
    with zipfile.ZipFile(io.BytesIO(data)) as zipped:
        members = []
        for entry in zipped.infolist():
            # Windows Compress-Archive uses backslashes. Normalize them before
            # checking traversal, then extract using only the checked path.
            name = entry.filename.replace('\\', '/')
            path = PurePosixPath(name)
            if (path.is_absolute() or '..' in path.parts or ':' in name
                    or not path.parts or path.parts[0] != 'evidence'
                    or stat.S_ISLNK(entry.external_attr >> 16)):
                raise RuntimeError('Unsafe evidence archive path')
            members.append((entry, path.parts, name.endswith('/')))
        output.mkdir(parents=True)
        for entry, parts, directory in members:
            target = output.joinpath(*parts)
            if directory:
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with zipped.open(entry) as source, target.open('xb') as destination:
                    shutil.copyfileobj(source, destination)


def collect(directory, root, output):
    """Collect only a probe's evidence directory, never its home or agent login."""
    if not re.fullmatch(r'SidePulse[A-Za-z0-9_-]+', root):
        raise ValueError('Choose a SidePulse probe root name, without paths')
    if output.exists():
        raise ValueError('Evidence output already exists; previous results are retained')
    vm = load(directory)
    if vm['status'] != 'started':
        raise ValueError('Start the test VM before collecting evidence')
    token = uuid.uuid4().hex
    archive = rf'C:\Windows\Temp\sidepulse-evidence-{token}.zip'
    receipt = rf'C:\Windows\Temp\sidepulse-evidence-{token}.json'
    source = rf'C:\Users\test\{root}\evidence'
    script = f"""$ErrorActionPreference='Stop'
try {{
    if (!(Test-Path -LiteralPath '{source}' -PathType Container)) {{ throw 'Probe evidence directory missing' }}
    Compress-Archive -LiteralPath '{source}' -DestinationPath '{archive}'
    $result=@{{ok=$true; token='{token}'; source='{source}'; sha256=(Get-FileHash -Algorithm SHA256 -LiteralPath '{archive}').Hash.ToLower()}}
}} catch {{
    $result=@{{ok=$false; token='{token}'; error=$_.ToString()}}
}}
$result | ConvertTo-Json | Set-Content -Encoding utf8 '{receipt}.tmp'
Move-Item -LiteralPath '{receipt}.tmp' -Destination '{receipt}'
"""
    call('exec', vm['uuid'], '--cmd', POWERSHELL, '-NoProfile', '-EncodedCommand',
         base64.b64encode(script.encode('utf-16le')).decode('ascii'))
    # This UTM version may return from exec before the guest has finished.
    # A unique completion receipt, rather than its exit code, proves success.
    deadline = time.monotonic() + 180
    while True:
        try:
            result = json.loads(pull(vm['uuid'], receipt).decode('utf-8-sig'))
            break
        except RuntimeError as exc:
            if 'cannot find' not in str(exc).lower() or time.monotonic() >= deadline:
                raise
            time.sleep(1)
    if result.get('token') != token or result.get('ok') is not True:
        raise RuntimeError(f'Guest evidence collection failed: {result}')
    data = pull(vm['uuid'], archive)
    if hashlib.sha256(data).hexdigest() != result['sha256']:
        raise RuntimeError('Collected evidence archive checksum differs')
    extract_evidence(data, output)
    (output / 'collection.json').write_text(json.dumps({**result, 'vm': vm,
        'collected_at': time.time()}, indent=2) + '\n')
    return {'output': str(output), 'source': source, 'sha256': result['sha256']}


def inventory():
    result = {}
    for line in call('list').splitlines():
        match = re.fullmatch(r'([0-9A-Fa-f-]{36})\s+(\S+)\s+(.+)', line)
        if match:
            identifier, status, name = match.groups()
            result[identifier.upper()] = {'uuid': identifier.upper(), 'status': status, 'name': name}
    return result


def register(directory, identifier):
    if directory.exists():
        raise ValueError('Choose a new metadata directory; existing records are never overwritten')
    vm = inventory()[identifier.upper()]
    if not vm['name'].startswith('SidePulse Windows '):
        raise ValueError('Only explicitly named SidePulse Windows test VMs may be registered')
    directory.mkdir(parents=True, mode=0o700)
    data = {'schema': 1, 'kind': KIND, 'uuid': vm['uuid'], 'name': vm['name'], 'registered_at': time.time()}
    (directory / 'vm.json').write_text(json.dumps(data, indent=2) + '\n')
    return data


def load(directory):
    data = json.loads((directory / 'vm.json').read_text())
    if data.get('schema') != 1 or data.get('kind') != KIND:
        raise ValueError('Not a Windows test VM registered by this harness')
    vm = inventory()[data['uuid']]
    if vm['name'] != data['name'] or not vm['name'].startswith('SidePulse Windows '):
        raise ValueError('VM identity differs from the registered disposable test machine')
    return vm


def clone(source, directory, name):
    vm = load(source)
    if vm['status'] != 'stopped':
        raise ValueError('Shut down the baseline before cloning it')
    if directory.exists():
        raise ValueError('Clone metadata directory already exists')
    if not name.startswith('SidePulse Windows ') or any(v['name'] == name for v in inventory().values()):
        raise ValueError('Choose a unique SidePulse Windows test name')
    call('clone', vm['uuid'], '--name', name)
    candidates = [v for v in inventory().values() if v['name'] == name]
    if len(candidates) != 1:
        raise RuntimeError('Clone identity could not be established; inspect UTM before retrying')
    data = register(directory, candidates[0]['uuid'])
    data.update(baseline_uuid=vm['uuid'], baseline_name=vm['name'])
    (directory / 'vm.json').write_text(json.dumps(data, indent=2) + '\n')
    return data


def stop(directory):
    vm = load(directory)
    if vm['status'] == 'stopped':
        return vm
    if vm['status'] != 'started':
        raise ValueError('VM is transitioning or suspended; inspect UTM rather than forcing it off')
    try:
        # Windows can ignore the virtual ACPI power button. Ask its guest
        # shutdown utility directly when Guest Tools is available. A positive
        # /t value implies /f on Windows, so keep /t at zero and omit /f.
        call('exec', vm['uuid'], '--cmd', r'C:\Windows\System32\shutdown.exe', '/s', '/t', '0')
    except (RuntimeError, subprocess.CalledProcessError):
        call('stop', '--request', vm['uuid'])
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        current = load(directory)
        if current['status'] == 'stopped':
            return current
        time.sleep(2)
    raise RuntimeError('Guest did not shut down normally; disk and running VM retained')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['register', 'status', 'start', 'stop', 'clone', 'collect'])
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--uuid')
    parser.add_argument('--source', type=Path)
    parser.add_argument('--name')
    parser.add_argument('--root', help='Probe root name beneath the test profile; collect only')
    parser.add_argument('--output', type=Path, help='New private evidence output directory')
    args = parser.parse_args()
    directory = args.directory.resolve()
    if args.command == 'register':
        if not args.uuid:
            parser.error('register requires --uuid from the UTM inventory')
        result = register(directory, args.uuid)
    elif args.command == 'clone':
        if not args.source or not args.name:
            parser.error('clone requires --source and --name')
        result = clone(args.source.resolve(), directory, args.name)
    elif args.command == 'stop':
        result = stop(directory)
    elif args.command == 'collect':
        if not args.root or not args.output:
            parser.error('collect requires --root and --output')
        result = collect(directory, args.root, args.output.resolve())
    else:
        result = load(directory)
        if args.command == 'start':
            if result['status'] != 'stopped':
                parser.error('Only a stopped VM can be started by the harness')
            call('start', result['uuid'])
            result = load(directory)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
