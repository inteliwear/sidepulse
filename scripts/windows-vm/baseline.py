#!/usr/bin/env python3
"""Verify the prepared, normal-user Windows baseline before cloning it."""
from __future__ import annotations

import json
from pathlib import Path
import platform
import subprocess
import time

from probe import guard


def main():
    guard('vm')
    home = Path.home()
    root = home / 'SidePulseHarness'
    marker = json.loads((root / 'vm.json').read_text())
    prior_probes = list(home.glob('SidePulse*/venv/Lib/site-packages/sidepulse-*.dist-info'))
    if prior_probes:
        raise RuntimeError('Baseline contains prior SidePulse probe installations')
    sources = marker['prepared_sources']
    observations = {}
    for source in sources:
        executable = Path(source['python_executable'])
        if not executable.is_relative_to(home):
            raise RuntimeError('Prepared interpreter is outside the test profile')
        result = subprocess.check_output([executable, '-c', '''
import importlib.metadata, json, sysconfig
names = sorted(d.metadata['Name'].lower() for d in importlib.metadata.distributions())
assert 'sidepulse' not in names, 'SidePulse is already installed in the baseline'
print(json.dumps({'python_platform': sysconfig.get_platform(), 'packages': names}))
'''], text=True)
        observation = json.loads(result)
        observations[observation['python_platform']] = {
            **observation, 'executable': str(executable), 'source': source['source']}
    if set(observations) != {'win-arm64', 'win-amd64'}:
        raise RuntimeError('Prepare both Windows Python architectures before this baseline check')
    git = home / 'Tools/Git/cmd/git.exe'
    version = subprocess.check_output([git, '--version'], text=True).strip()
    receipt = root / ('baseline-' + time.strftime('%Y%m%d-%H%M%S') + '.json')
    data = {'kind': 'sidepulse-clean-windows-baseline', 'at': time.time(),
            'platform': platform.platform(), 'user_home': str(home),
            'normal_user_guard_passed': True, 'sidepulse_installed': False,
            'interpreters': observations, 'git': {'path': str(git), 'version': version}}
    with receipt.open('x', encoding='utf-8') as stream:
        json.dump(data, stream, indent=2)
        stream.write('\n')
    print(json.dumps({'baseline_receipt': str(receipt), **data}, indent=2))
    print('Shut down normally, then clone the stopped VM. No SidePulse product checks have run.')


if __name__ == '__main__':
    main()
