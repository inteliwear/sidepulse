#!/usr/bin/env python3
"""Native Windows installation probes; failures remain failures in the evidence.

Run in the disposable test VM, or in an explicitly selected Windows CI scope.
This does not start a substitute service or claim authenticated agent coverage.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import sysconfig
import time
import traceback
import uuid

SOURCE = Path(__file__).resolve().parents[2]
MAIN_SPEC = 'git+https://github.com/inteliwear/sidepulse.git@main'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def guard(scope):
    if sys.platform != 'win32' or platform.system() != 'Windows':
        raise RuntimeError('Native Windows is required; WSL and host macOS are not Windows coverage')
    if scope == 'ci':
        if os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('RUNNER_OS') != 'Windows':
            raise RuntimeError('CI scope requires an actual Windows GitHub Actions runner')
    else:
        marker = Path.home() / 'SidePulseHarness/vm.json'
        if not marker.is_file() or json.loads(marker.read_text()).get('kind') != 'sidepulse-disposable-windows':
            raise RuntimeError('Missing disposable Windows VM marker')
        if Path.home().name.lower() != 'test':
            raise RuntimeError('VM probes own only the disposable test user profile')
        if ctypes.windll.shell32.IsUserAnAdmin():
            raise RuntimeError('Run the product probes without administrator elevation')
        model = subprocess.check_output([
            'powershell.exe', '-NoProfile', '-Command',
            '(Get-CimInstance Win32_ComputerSystem).Model'], text=True).strip()
        if not any(name in model.lower() for name in ('qemu', 'virtual machine', 'virtualbox')):
            raise RuntimeError(f'Unrecognized virtual machine model: {model}')


class Probe:
    def __init__(self, root, stage, *, installer=False):
        self.root, self.stage = root.resolve(), stage
        self.installer = installer and stage != 'main'
        self.output = self.root / 'evidence' / (time.strftime('%Y%m%d-%H%M%S-') + stage + '-' + uuid.uuid4().hex[:6])
        self.output.mkdir(parents=True, exist_ok=False)
        self.venv = self.root / 'venv'
        self.python = self.venv / 'Scripts/python.exe'
        self.cli = self.venv / 'Scripts/sidepulse.exe'
        self.home = self.root / 'home'
        self.home.mkdir(parents=True, exist_ok=True)
        # Product configuration belongs only to this harness directory. This
        # also gives CI the same isolation as the disposable VM test account.
        self.env = {**os.environ, 'USERPROFILE': str(self.home), 'HOME': str(self.home),
                    'XDG_CONFIG_HOME': str(self.home / '.config'),
                    'XDG_STATE_HOME': str(self.home / '.local/state'),
                    'XDG_DATA_HOME': str(self.home / '.local/share'),
                    'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'}
        self.env.pop('PYTHONPATH', None)
        self.rows = []
        context = SOURCE / 'build-context.json'
        if context.is_file():
            (self.output / context.name).write_bytes(context.read_bytes())

    def command(self, name, argv, *, timeout=120, check=True, cwd=None):
        argv = [str(v) for v in argv]
        with (self.output / (name + '.log')).open('w', encoding='utf-8') as log:
            log.write(json.dumps({'argv': argv}) + '\n')
            log.flush()
            with subprocess.Popen(argv, cwd=cwd or self.home, env=self.env, stdout=log,
                                  stderr=subprocess.STDOUT) as process:
                try:
                    code = process.wait(timeout)
                except subprocess.TimeoutExpired:
                    subprocess.run(['taskkill.exe', '/PID', str(process.pid), '/T', '/F'],
                                   stdout=log, stderr=log, check=False)
                    process.wait(timeout=30)
                    raise RuntimeError(f'{name} timed out after {timeout}s') from None
        if check and code:
            raise RuntimeError(f'{name} exited {code}; see {name}.log')
        return code

    def check(self, name, action):
        try:
            detail = action()
        except Exception as exc:
            self.rows.append({'name': name, 'status': 'failed', 'error': str(exc),
                              'traceback': traceback.format_exc()})
        else:
            self.rows.append({'name': name, 'status': 'passed', 'detail': detail})
        self.save()

    def python_check(self, name, code):
        self.command(name, [self.python, '-c', code])
        return {'log': name + '.log'}

    def install(self):
        if self.stage in ('fresh', 'main'):
            if self.venv.exists():
                raise RuntimeError('Fresh install requires a new harness root; existing venv retained')
            if not self.installer:
                self.command('create-venv', [sys.executable, '-m', 'venv', self.venv])
        elif not self.python.is_file():
            raise RuntimeError('Upgrade/reinstall requires a previously installed harness venv')
        if self.stage == 'upgrade':
            if not (self.root / 'main-installed.json').is_file():
                raise RuntimeError('Upgrade requires a successful official-main installation in this root')
            self.snapshot('before-upgrade')
        spec = MAIN_SPEC if self.stage == 'main' else str(SOURCE)
        context = SOURCE / 'build-context.json'
        if self.stage != 'main' and context.is_file():
            # A source bundle lacks .git; preserve the actual frozen revision.
            revision = json.loads(context.read_text())['revision']
            self.env['SETUPTOOLS_SCM_PRETEND_VERSION_FOR_SIDEPULSE'] = '0+windowstest'
            self.env['SETUPTOOLS_SCM_PRETEND_METADATA_FOR_SIDEPULSE'] = f'{{node="g{revision}"}}'
        if self.installer:
            import base64
            quote = lambda value: "'" + str(value).replace("'", "''") + "'"
            # Invoke the reviewed script without changing any execution policy.
            script = ("& ([scriptblock]::Create([IO.File]::ReadAllText(" +
                      quote(SOURCE / 'scripts/setup.ps1') + "))) -SourceDirectory " +
                      quote(SOURCE) + ' -InstallDirectory ' + quote(self.root) +
                      ' -PythonExecutable ' + quote(sys.executable))
            self.command('powershell-installer', ['powershell.exe', '-NoProfile', '-EncodedCommand',
                base64.b64encode(script.encode('utf-16le')).decode('ascii')], timeout=900)
        else:
            self.command('pip-install', [self.python, '-m', 'pip', 'install', '--upgrade',
                                        '--disable-pip-version-check', spec], timeout=900)
        self.snapshot('installed')
        if self.stage == 'main':
            (self.root / 'main-installed.json').write_text(json.dumps({
                'spec': MAIN_SPEC, 'evidence': str(self.output), 'at': time.time()}), encoding='utf-8')
        return {'spec': spec, 'python': str(self.python)}

    def snapshot(self, name):
        self.python_check(name, '''
import importlib.metadata as m, json, os, platform, sys, sysconfig
d = m.distribution('sidepulse')
print(json.dumps({'version': d.version, 'python': sys.version,
    'executable': sys.executable, 'platform': platform.platform(),
    'machine': platform.machine(), 'python_platform': sysconfig.get_platform(),
    'processor_architecture': os.environ.get('PROCESSOR_ARCHITECTURE'),
    'native_processor_architecture': os.environ.get('PROCESSOR_ARCHITEW6432'),
    'direct_url': d.read_text('direct_url.json')}, indent=2))
''')

    def source_match(self):
        manifest = SOURCE / 'source-manifest.json'
        if not manifest.is_file():
            # CI's checkout is already frozen by its recorded Git revision.
            files = SOURCE / 'src/sidepulse'
            hashes = {'src/' + str(p.relative_to(SOURCE / 'src')).replace('\\', '/'):
                      digest(p) for p in files.rglob('*') if p.is_file()
                      and '__pycache__' not in p.parts and p.name != '_build_info.py'}
        else:
            hashes = {k: v for k, v in json.loads(manifest.read_text()).items()
                      if k.startswith('src/') and not k.endswith('/_build_info.py')}
        expected = self.output / 'expected-source.json'
        expected.write_text(json.dumps(hashes), encoding='utf-8')
        return self.python_check('installed-source', f'''
import hashlib, json, pathlib, sidepulse
expected = json.loads(pathlib.Path({str(expected)!r}).read_text())
root = pathlib.Path(sidepulse.__file__).resolve().parent.parent
actual = {{name: hashlib.sha256((root / name[4:]).read_bytes()).hexdigest() for name in expected}}
assert actual and actual == expected, [name for name in expected if actual.get(name) != expected[name]]
print(json.dumps({{'non_generated_files': len(actual), 'hashes': actual}}, indent=2))
''')

    def managed_hooks(self, name):
        return self.python_check(name, '''
import json, pathlib, tomllib
from sidepulse.providers import CLAUDE_EVENTS, CODEX_EVENTS
try:
    from sidepulse.hook_commands import observer_script
except ImportError:  # Public main predates the Windows command encoding.
    observer_script = lambda value: value
home = pathlib.Path.home()
configs = {'claude': json.loads((home / '.claude/settings.json').read_text()),
           'codex': tomllib.loads((home / '.codex/config.toml').read_text())}
result = {}
for provider, events in [('claude', CLAUDE_EVENTS), ('codex', CODEX_EVENTS)]:
    result[provider] = {}
    for event in events:
        entries = configs[provider].get('hooks', {}).get(event, [])
        commands = [hook['command'] for entry in entries for hook in entry.get('hooks', [])
                    if hook.get('type') == 'command' and 'sidepulse' in observer_script(hook.get('command', ''))]
        assert len(commands) == 1, (provider, event, commands)
        result[provider][event] = commands[0]
print(json.dumps(result, indent=2))
''')

    def seed_migration(self):
        marker = self.root / 'migration-seed.json'
        if marker.exists():
            raise RuntimeError('Migration preferences were already seeded; refusing to replace them')
        self.python_check('seed-user-config', '''
import json, pathlib
home = pathlib.Path.home()
claude = home / '.claude/settings.json'
data = json.loads(claude.read_text())
data.setdefault('env', {})['SIDEPULSE_MIGRATION_TEST'] = 'preserve-me'
data.setdefault('hooks', {}).setdefault('Stop', []).append({
    'hooks': [{'type': 'command', 'command': 'echo unrelated-windows-hook'}]})
claude.write_text(json.dumps(data, indent=2) + '\\n')
codex = home / '.codex/config.toml'
with codex.open('a') as stream:
    stream.write('\\n[[hooks.Stop]]\\n[[hooks.Stop.hooks]]\\ntype = "command"\\ncommand = "echo unrelated-windows-hook"\\n')
settings = home / '.config/sidepulse/settings.json'
settings.parent.mkdir(parents=True, exist_ok=True)
data = json.loads(settings.read_text()) if settings.exists() else {}
data['idle_timeout_seconds'] = 321
settings.write_text(json.dumps(data, indent=2) + '\\n')
print('Seeded user settings and an unrelated hook for each provider')
''')
        marker.write_text(json.dumps({'python': str(self.python.resolve()),
            'python_sha256': digest(self.python), 'evidence': str(self.output)}, indent=2) + '\n')
        return {'marker': str(marker)}

    def migration(self):
        marker = self.root / 'migration-seed.json'
        before = json.loads(marker.read_text())
        assert Path(before['python']) == self.python.resolve(), 'Upgrade changed the interpreter path'
        assert before['python_sha256'] == digest(self.python), 'Upgrade replaced the interpreter'
        return self.python_check('preserved-user-config', '''
import json, pathlib, tomllib
home = pathlib.Path.home()
claude = json.loads((home / '.claude/settings.json').read_text())
assert claude['env']['SIDEPULSE_MIGRATION_TEST'] == 'preserve-me'
codex = tomllib.loads((home / '.codex/config.toml').read_text())
for provider, config in [('claude', claude), ('codex', codex)]:
    commands = [hook['command'] for entry in config['hooks']['Stop'] for hook in entry.get('hooks', [])]
    assert commands.count('echo unrelated-windows-hook') == 1, (provider, commands)
assert json.loads((home / '.config/sidepulse/settings.json').read_text())['idle_timeout_seconds'] == 321
print('Preserved interpreter, user settings, and unrelated hooks exactly once')
''')

    def run(self, full_suite=False):
        if self.stage == 'unit':
            # Rerun corrected platform-aware tests against the exact already
            # installed product. A changed product source must not be hidden
            # by silently reinstalling it for a test-only rerun.
            self.check('installed-source', self.source_match)
            if self.rows[-1]['status'] == 'passed':
                self.full_suite()
            return self.save()
        self.check('install', self.install)
        if self.rows[-1]['status'] != 'passed':
            return self.save()
        self.check('installed-cli', lambda: self.command('cli-version', [self.cli, '--version']))
        self.check('cli-help', lambda: self.command('cli-help', [self.cli, '--help']))
        if self.stage != 'main':
            self.check('installed-source', self.source_match)
        self.check('setup', lambda: self.command('setup', [self.cli, 'setup']))
        self.check('managed-hooks', lambda: self.managed_hooks('managed-hooks'))
        self.check('setup-repeat', lambda: self.command('setup-repeat', [self.cli, 'setup']))
        self.check('managed-hooks-after-repeat', lambda: self.managed_hooks('managed-hooks-after-repeat'))
        if self.stage in ('main', 'fresh'):
            self.check('seed-migration-preferences', self.seed_migration)
        else:
            self.check('preserved-user-config', self.migration)
        self.check('installed-background-service', lambda: self.command('service-status', [self.cli, 'service', 'status']))
        device = self.home / ('device-directory-fixture-' + uuid.uuid4().hex)
        device.mkdir(exist_ok=True)
        self.check('native-file-write', lambda: self.command('device-write',
            [self.cli, 'write', 'off', '--device', device]))
        self.check('native-file-bytes', lambda: self.python_check('device-file-bytes',
            f'from pathlib import Path; p=Path({str(device / "LEDS.LED")!r}); '
            'assert p.read_text().strip() == "off"; print(repr(p.read_bytes()))'))
        self.check('native-ipc', lambda: self.python_check('native-ipc', '''
import json, pathlib, tempfile
from sidepulse.program_transport import ProgramServer, daemon_command
from sidepulse.program_status import ProgramState
with tempfile.TemporaryDirectory(prefix='sp-ipc-') as directory:
    path = pathlib.Path(directory) / 'status.sock'
    server = ProgramServer(ProgramState(), path=path)
    try:
        server.start()
        reply = daemon_command({'type': 'get_state'}, path=path)
        assert reply['ok'] is True
        print(json.dumps(reply))
    finally:
        server.stop()
'''))
        if full_suite:
            self.full_suite()
        return self.save()

    def full_suite(self):
        self.check('test-dependencies', lambda: self.command('pytest-install',
            [self.python, '-m', 'pip', 'install', 'pytest', 'pytest-subtests'], timeout=600))
        if self.rows[-1]['status'] == 'passed':
            self.check('full-python-suite', lambda: self.command('full-suite',
                [self.python, '-m', 'pytest', str(SOURCE / 'tests'), '-q',
                 '--junitxml=' + str(self.output / 'junit.xml')], timeout=1800, cwd=SOURCE))

    def save(self):
        result = {'schema': 1, 'scope': 'native-windows', 'stage': self.stage,
                  'platform': platform.platform(), 'machine': platform.machine(),
                  'python_platform': sysconfig.get_platform(),
                  'install_method': ('not_run' if self.stage == 'unit' else
                                     'native-powershell-installer' if self.installer else 'pip-package'),
                  'at': time.time(), 'checks': self.rows,
                  'failures': sum(r['status'] == 'failed' for r in self.rows),
                  'real_agents_run': False, 'visual_comparison_run': False,
                  'physical_usb_run': False,
                  'limitations': ['Directory file fixture is not a mounted removable volume.',
                                  'No substitute daemon is used to pass the service check.']}
        (self.output / 'result.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=['fresh', 'main', 'upgrade', 'repeat', 'unit'])
    parser.add_argument('--scope', choices=['vm', 'ci'], default='vm')
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--full-suite', action='store_true')
    parser.add_argument('--installer', action='store_true', help='Exercise scripts/setup.ps1 for the branch installation')
    args = parser.parse_args()
    guard(args.scope)
    root = args.root.resolve()
    if root == Path.home() or Path.home() not in root.parents:
        parser.error('Harness root must be a new subdirectory of the test user profile')
    probe = Probe(root, args.stage, installer=args.installer)
    result = probe.run(full_suite=args.full_suite)
    print(json.dumps({'evidence': str(probe.output), 'failures': result['failures']}))
    return int(result['failures'] > 0)


if __name__ == '__main__':
    raise SystemExit(main())
