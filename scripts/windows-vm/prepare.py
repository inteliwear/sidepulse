#!/usr/bin/env python3
"""Prepare source in a normal test user's native Windows VM; no SidePulse install."""
from __future__ import annotations

import ctypes
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys
import sysconfig
import tarfile


def prepare(archive, context_path):
    if sys.platform != 'win32' or Path.home().name.lower() != 'test':
        raise RuntimeError('Preparation is restricted to the disposable native Windows test profile')
    if ctypes.windll.shell32.IsUserAnAdmin():
        raise RuntimeError('Run preparation without administrator elevation')
    model = subprocess.check_output(['powershell.exe', '-NoProfile', '-Command',
        '(Get-CimInstance Win32_ComputerSystem).Model'], text=True).strip()
    if not any(name in model.lower() for name in ('qemu', 'virtual machine', 'virtualbox')):
        raise RuntimeError('A virtual machine is required')
    context = json.loads(context_path.read_text())
    sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    if sha != context['bundle_sha256']:
        raise RuntimeError('Source archive differs from frozen build context')
    root = Path.home() / 'SidePulseHarness'
    marker = root / 'vm.json'
    previous = json.loads(marker.read_text()) if marker.exists() else {}
    if previous and previous.get('kind') != 'sidepulse-disposable-windows':
        raise RuntimeError('Existing VM marker is not owned by this harness')
    source = root / ('source-' + sysconfig.get_platform() + '-' + sha[:16])
    if source.exists():
        raise RuntimeError('Source already prepared; choose the saved path rather than overwriting it')
    root.mkdir(exist_ok=True)
    source.mkdir()
    with tarfile.open(archive) as tar:
        members = tar.getmembers()
        if any(not m.isfile() or Path(m.name).is_absolute() or '..' in Path(m.name).parts
               or ':' in m.name or '\\' in m.name for m in members):
            raise RuntimeError('Unsafe source archive member')
        tar.extractall(source, members=members, filter='data')
    manifest = json.loads((source / 'source-manifest.json').read_text())
    mismatch = [name for name, expected in manifest.items()
                if hashlib.sha256((source / name).read_bytes()).hexdigest() != expected]
    if mismatch:
        raise RuntimeError(f'Extracted source differs: {mismatch}')
    observation = {'model': model, 'platform': platform.platform(), 'python': sys.version,
                   'python_executable': sys.executable, 'machine': platform.machine(),
                   'python_platform': sysconfig.get_platform(), 'source': str(source),
                   'build_context': context}
    marker.write_text(json.dumps({'kind': 'sidepulse-disposable-windows',
        'model': model, 'platform': platform.platform(), 'python': sys.version,
        'machine': platform.machine(), 'python_platform': sysconfig.get_platform(),
        'source': str(source), 'build_context': context,
        'prepared_sources': [*previous.get('prepared_sources', []), observation]}, indent=2) + '\n')
    print('Prepared source:', source)
    print('Python:', sys.executable)
    print('Source preparation did not install or change SidePulse.')
    print('For a new clean baseline, verify it with baseline.py before shutdown and cloning.')


if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit('Usage: prepare.py source.tar.gz source-context.json')
    prepare(Path(sys.argv[1]), Path(sys.argv[2]))
