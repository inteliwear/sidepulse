"""Run the actual curl installer against offline package fixtures."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
import zipfile
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'setup.sh'


class SetupUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='sidepulse-setup-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.install_root = self.root / 'install with spaces'
        self.bin = self.root / 'bin'
        self.log = self.root / 'calls.jsonl'
        self.home = self.root / 'home'
        self.home.mkdir()
        self.env = {
            **os.environ,
            'HOME': str(self.home),
            'PYTHON_BIN': sys.executable,
            'SIDEPULSE_INSTALL_ROOT': str(self.install_root),
            'SIDEPULSE_BIN_DIR': str(self.bin),
            'SIDEPULSE_TEST_LOG': str(self.log),
            'PIP_NO_INDEX': '1',
            'PIP_DISABLE_PIP_VERSION_CHECK': '1',
        }
        # The fixtures must import their installed wheels, even when the test
        # runner uses PYTHONPATH to exercise an isolated source snapshot.
        self.env.pop('PYTHONPATH', None)
        self.env.pop('PYTHONHOME', None)
        self.old = self.wheel('old')
        self.new = self.wheel('new', dependency=True)
        self.write_wheel(self.new.parent, 'sidepulse_fixture_dependency', {'sidepulse_fixture_dependency/__init__.py': ''})
        self.env['PIP_FIND_LINKS'] = str(self.new.parent)

    def wheel(self, marker, dependency=False):
        directory = self.root / marker
        directory.mkdir()
        cli = textwrap.dedent(f'''
            import json, os, sys
            MARKER = {marker!r}
            def main():
                args = sys.argv[1:]
                with open(os.environ['SIDEPULSE_TEST_LOG'], 'a') as log:
                    log.write(json.dumps([MARKER, args]) + '\\n')
                if args == ['setup'] and os.environ.get('SIDEPULSE_TEST_SETUP_FAIL'):
                    return 1
                if args == ['--version']:
                    print('sidepulse 1.0 ' + MARKER)
                return 0
        ''')
        return self.write_wheel(directory, 'sidepulse', {
            'sidepulse/__init__.py': f'MARKER = {marker!r}\n',
            'sidepulse/cli.py': cli,
        }, dependency=dependency)

    @staticmethod
    def write_wheel(directory, name, files, dependency=False):
        info = f'{name}-1.0.dist-info'
        metadata = f'Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n'
        if dependency:
            metadata += 'Requires-Dist: sidepulse-fixture-dependency==1.0\n'
        files = dict(files)
        files[f'{info}/METADATA'] = metadata
        files[f'{info}/WHEEL'] = 'Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n'
        if name == 'sidepulse':
            files[f'{info}/entry_points.txt'] = '[console_scripts]\nsidepulse = sidepulse.cli:main\nagent-monitor = sidepulse.cli:main\n'
        files[f'{info}/RECORD'] = ''.join(f'{path},,\n' for path in files) + f'{info}/RECORD,,\n'
        wheel = directory / f'{name}-1.0-py3-none-any.whl'
        with zipfile.ZipFile(wheel, 'w') as archive:
            for path, content in files.items():
                archive.writestr(path, content)
        return wheel

    def install(self, spec, **env):
        return subprocess.run(
            ['bash', str(SCRIPT)],
            env={**self.env, 'SIDEPULSE_INSTALL_SPEC': str(spec), **env},
            capture_output=True, text=True, timeout=120,
        )

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_fresh_install_then_same_version_update_loads_new_code_and_dependency(self):
        result = self.install(self.old)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        config = self.home / 'settings.json'
        config.write_text('{"show_menu_bar_icon": false}')
        self.log.write_text('')
        result = self.install(self.new)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls(), [
            ['old', ['service', 'status']], ['old', ['service', 'stop']],
            ['new', ['setup']], ['new', ['--version']],
        ])
        self.assertEqual(config.read_text(), '{"show_menu_bar_icon": false}')
        self.assertEqual((self.bin / 'sidepulse').resolve(), (self.install_root / 'venv/bin/sidepulse').resolve())
        self.assertTrue((self.bin / 'agent-monitor').is_file())
        python = self.install_root / 'venv/bin/python'
        output = subprocess.check_output([
            str(python), '-c', 'import sidepulse_fixture_dependency, sidepulse; print(sidepulse.MARKER)',
        ], text=True, cwd=self.root, env=self.env)
        self.assertEqual(output.strip(), 'new')

    def test_download_or_build_failure_keeps_existing_installation_running(self):
        self.assertEqual(self.install(self.old).returncode, 0)
        self.log.write_text('')
        result = self.install(self.root / 'missing.whl')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), [])
        result = subprocess.run([str(self.bin / 'sidepulse'), '--version'], env=self.env, capture_output=True, text=True)
        self.assertIn('old', result.stdout)

    def test_setup_failure_resumes_previously_stopped_service(self):
        self.assertEqual(self.install(self.old).returncode, 0)
        self.log.write_text('')
        result = self.install(self.new, SIDEPULSE_TEST_SETUP_FAIL='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls()[-1], ['new', ['service', 'start']])
        self.assertNotIn('SidePulse is installed.', result.stdout)

    def test_macos_menu_bar_is_stopped_before_replacement_and_resumed_on_failure(self):
        self.assertEqual(self.install(self.old).returncode, 0)
        stubs = self.root / 'stubs'
        stubs.mkdir()
        (stubs / 'uname').write_text('#!/bin/sh\nprintf "Darwin\\n"\n')
        launchctl = stubs / 'launchctl'
        launchctl.write_text(f'#!{sys.executable}\n' + textwrap.dedent('''
            import json, os, sys
            from pathlib import Path
            state = Path(os.environ['SIDEPULSE_TEST_LAUNCH_STATE'])
            args = sys.argv[1:]
            with open(os.environ['SIDEPULSE_TEST_LOG'], 'a') as log:
                log.write(json.dumps(['launchctl', args]) + '\\n')
            if args[0] == 'print':
                sys.exit(0 if state.exists() else 1)
            if args[0] == 'bootout':
                state.unlink()
            if args[0] == 'bootstrap':
                state.touch()
        '''))
        for path in stubs.iterdir():
            path.chmod(0o755)
        plist = self.home / 'Library/LaunchAgents/io.sidepulse.agentstatus.plist'
        plist.parent.mkdir(parents=True)
        plist.touch()
        state = self.root / 'loaded'
        state.touch()
        self.log.write_text('')
        result = self.install(
            self.new, SIDEPULSE_TEST_SETUP_FAIL='1',
            SIDEPULSE_TEST_LAUNCH_STATE=str(state),
            PATH=f'{stubs}:{os.environ["PATH"]}',
        )
        self.assertNotEqual(result.returncode, 0)
        actions = [(who, args[0]) for who, args in self.calls()]
        self.assertLess(actions.index(('launchctl', 'bootout')), actions.index(('new', 'setup')))
        self.assertEqual(actions[-1], ('launchctl', 'bootstrap'))
        self.assertTrue(state.exists())
