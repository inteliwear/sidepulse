#!/usr/bin/env python3
"""Run the full Linux suite from the same frozen source used for VM installs."""
import argparse
import json
import os
from pathlib import Path
import platform
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if platform.system() != 'Linux':
        parser.error('Run inside the disposable Linux container or VM')
    root = Path(__file__).resolve().parents[2]
    context = json.loads((root / 'build-context.json').read_text())
    args.output.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env['SETUPTOOLS_SCM_PRETEND_VERSION_FOR_SIDEPULSE'] = '0+linuxtest'
    env['SETUPTOOLS_SCM_PRETEND_METADATA_FOR_SIDEPULSE'] = '{node="g' + context['revision'] + '"}'
    commands = [('package-install', [sys.executable, '-m', 'pip', 'install', str(root) + '[test]']),
                ('unit', [sys.executable, '-m', 'pytest', 'tests', '-q',
                          '--junitxml=' + str(args.output / 'junit.xml')])]
    report = {'platform': platform.platform(), 'python': sys.version, 'architecture': platform.machine(),
              'build_context': context, 'commands': [], 'vm_integration': 'not_run', 'live_agents': 'not_run'}
    code = 0
    for name, argv in commands:
        with (args.output / (name + '.log')).open('w') as log:
            result = subprocess.run(argv, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT)
        report['commands'].append({'name': name, 'exit_code': result.returncode})
        if result.returncode:
            code = result.returncode
            break
    (args.output / 'environment.json').write_text(json.dumps(report, indent=2) + '\n')
    return code


if __name__ == '__main__':
    raise SystemExit(main())
