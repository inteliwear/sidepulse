"""Run the actual curl installer against offline package fixtures."""
from __future__ import annotations

import errno
import json
import os
import re
import select
import shlex
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import zipfile
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'setup.sh'


@unittest.skipUnless(os.name == "posix", "POSIX shell installer fixtures")
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
        self.tools = self.root / 'tools'
        self.tools.mkdir()
        for name, output in (('uname', 'Linux'), ('id', '501')):
            tool = self.tools / name
            tool.write_text(f'#!/bin/sh\nprintf "%s\\n" "{output}"\n')
            tool.chmod(0o755)
        sudo = self.tools / 'sudo'
        sudo.write_text(f'#!{sys.executable}\n' + textwrap.dedent('''
            import json, os, sys
            args = sys.argv[1:]
            with open(os.environ['SIDEPULSE_TEST_LOG'], 'a') as log:
                log.write(json.dumps(['sudo', args]) + '\\n')
            if args[:2] == ['-H', '-p']:
                with open('/dev/tty', 'w') as terminal:
                    print('Mock administrator prompt', file=terminal)
                command = args[3:]
            elif args[:2] == ['-n', '-H']:
                command = args[2:]
            else:
                sys.exit(2)
            if os.environ.get('SIDEPULSE_TEST_SUDO_FAIL'):
                sys.exit(1)
            os.execv(command[0], command)
        '''))
        sudo.chmod(0o755)
        self.env = {
            **os.environ,
            'HOME': str(self.home),
            'PATH': f'{self.tools}:{os.environ["PATH"]}',
            'HERDR_BIN_PATH': str(self.tools / 'herdr'),
            'PYTHON_BIN': sys.executable,
            'SIDEPULSE_INSTALL_ROOT': str(self.install_root),
            'SIDEPULSE_BIN_DIR': str(self.bin),
            'SIDEPULSE_SETUP_NONINTERACTIVE': '1',
            'SIDEPULSE_TEST_LOG': str(self.log),
            'XDG_STATE_HOME': str(self.root / 'state'),
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
            def sidepulse_main():
                args = sys.argv[1:]
                with open(os.environ['SIDEPULSE_TEST_LOG'], 'a') as log:
                    log.write(json.dumps([MARKER, args]) + '\\n')
                if args[0] == 'setup':
                    print('Configuring SidePulse components')
                    if os.environ.get('SIDEPULSE_TEST_SETUP_FAIL'):
                        print('Component setup failed', file=sys.stderr)
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
            files[f'{info}/entry_points.txt'] = '[console_scripts]\nsidepulse = sidepulse.cli:sidepulse_main\n'
            if directory.name == 'old':
                files[f'{info}/entry_points.txt'] += 'agent-monitor = sidepulse.cli:sidepulse_main\nagent-status-bar = sidepulse.cli:sidepulse_main\n'
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
        self.assertEqual([line for line in result.stdout.splitlines() if line.startswith('[')], [
            '[1/5] Checking system requirements…',
            '[2/5] Preparing the Python environment…',
            '[3/5] Downloading and building packages…',
            '[4/5] Installing SidePulse and command links…',
            '[5/5] Configuring the background service and selected components…',
        ])
        self.assertIn('SidePulse is installed.', result.stdout)
        self.assertNotIn('Configuring SidePulse components', result.stdout)
        self.assertEqual(result.stderr, '')
        setup_log, = (self.root / 'state/sidepulse/setup').iterdir()
        self.assertIn('Configuring SidePulse components', setup_log.read_text())
        self.assertNotIn(str(setup_log), result.stdout)
        config = self.home / 'settings.json'
        config.write_text('{"show_menu_bar_icon": false}')
        self.log.write_text('')
        for name in ('agent-monitor', 'agent-status-bar'):
            (self.bin / name).symlink_to(self.install_root / 'venv/bin' / name)
        result = self.install(self.new)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls(), [
            ['old', ['service', 'status']], ['old', ['service', 'stop']],
            ['new', ['setup']], ['new', ['--version']],
        ])
        self.assertEqual(config.read_text(), '{"show_menu_bar_icon": false}')
        self.assertEqual((self.bin / 'sidepulse').resolve(), (self.install_root / 'venv/bin/sidepulse').resolve())
        for name in ('agent-monitor', 'agent-status-bar'):
            self.assertFalse((self.bin / name).is_symlink())
            self.assertFalse((self.install_root / 'venv/bin' / name).exists())
        python = self.install_root / 'venv/bin/python'
        output = subprocess.check_output([
            str(python), '-c', 'import sidepulse_fixture_dependency, sidepulse; print(sidepulse.MARKER)',
        ], text=True, cwd=self.root, env=self.env)
        self.assertEqual(output.strip(), 'new')

    def test_update_preserves_unrelated_legacy_named_commands(self):
        self.assertEqual(self.install(self.old).returncode, 0)
        other = self.root / 'unrelated-command'
        other.write_text('unrelated')
        (self.bin / 'agent-monitor').symlink_to(other)
        (self.bin / 'agent-status-bar').write_text('custom command')
        result = self.install(self.new)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.bin / 'agent-monitor').resolve(), other.resolve())
        self.assertEqual((self.bin / 'agent-status-bar').read_text(), 'custom command')

    def test_download_or_build_failure_keeps_existing_installation_running(self):
        self.assertEqual(self.install(self.old).returncode, 0)
        self.log.write_text('')
        result = self.install(self.root / 'missing.whl')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('[3/5] Downloading and building packages', result.stdout)
        self.assertNotIn('[4/5]', result.stdout)
        self.assertEqual(self.calls(), [])
        result = subprocess.run([str(self.bin / 'sidepulse'), '--version'], env=self.env, capture_output=True, text=True)
        self.assertIn('old', result.stdout)

    def test_update_reuses_venv_when_shell_python_is_unavailable(self):
        self.assertEqual(self.install(self.old).returncode, 0)
        self.log.write_text('')
        result = self.install(self.new, PYTHON_BIN='missing-python')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(['new', ['--version']], self.calls())

    def test_setup_failure_resumes_previously_stopped_service(self):
        self.assertEqual(self.install(self.old).returncode, 0)
        self.log.write_text('')
        result = self.install(self.new, SIDEPULSE_TEST_SETUP_FAIL='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls()[-1], ['new', ['service', 'start']])
        self.assertNotIn('SidePulse is installed.', result.stdout)
        self.assertIn('Component setup failed', result.stderr)
        log_path = Path(result.stderr.rsplit('Full log: ', 1)[1].strip())
        self.assertIn('Component setup failed', log_path.read_text())

    def run_piped_setup_in_terminal(
        self, *inputs, headless=False, install_keys=b'', platform='Darwin', no_color=False,
        developer_tools=False, sudo_fail=False, skip_guard=False,
    ):
        import pty

        stubs = self.root / 'stubs'
        stubs.mkdir()
        for name, body in (
            ('uname', f'printf "{platform}\\n"'),
            ('xcode-select', 'exit 0' if headless or developer_tools else 'exit 1'),
            ('xcrun', 'exit 0' if headless or developer_tools else 'exit 1'),
        ):
            stub = stubs / name
            stub.write_text(f'#!/bin/sh\n{body}\n')
            stub.chmod(0o755)

        env = {
            **self.env, 'PATH': f'{stubs}:{self.env["PATH"]}',
            'SIDEPULSE_INSTALL_SPEC': str(self.new), 'SIDEPULSE_SETUP_NONINTERACTIVE': '0',
            'TERM': 'xterm-256color', 'NO_COLOR': '1' if no_color else '',
        }
        if sudo_fail:
            env['SIDEPULSE_TEST_SUDO_FAIL'] = '1'
        options = ['--headless'] if headless else []
        if skip_guard:
            options.append('--no-sd-eject-guard')
        terminal_state = '''
import termios

settings = termios.tcgetattr(0)
# BSD marks pending input for reprocessing when canonical mode is restored.
settings[3] &= ~getattr(termios, 'PENDIN', 0)
print(settings)
'''
        run_setup = '''
            script=$1
            python=$2
            terminal_state=$3
            shift 3
            before=$("$python" -c "$terminal_state") || exit 99
            cat "$script" | sh -s -- "$@"
            result=$?
            after=$("$python" -c "$terminal_state") || exit 99
            if [ "$after" != "$before" ]; then
                printf 'Terminal settings changed\nBefore %s\nAfter  %s\n' "$before" "$after"
                exit 99
            fi
            exit "$result"
        '''
        command = ['sh', '-c', run_setup, 'sh', str(SCRIPT), sys.executable, terminal_state, *options]
        pid, terminal = pty.fork()
        if pid == 0:
            try:
                os.execve('/bin/sh', command, env)
            finally:
                os._exit(127)
        output = bytearray()
        deadline = time.monotonic() + 60
        reaped = False

        def wait_for_output(marker, start=0):
            while marker not in output[start:]:
                self.assertTrue(select.select([terminal], [], [], max(0, deadline - time.monotonic()))[0], output)
                output.extend(os.read(terminal, 4096))

        try:
            for answer in inputs:
                wait_for_output(b'q cancels\r\n', start=len(output))
                self.assertFalse(self.install_root.exists())
                self.assertFalse((self.root / 'state').exists())
                os.write(terminal, answer)
            if install_keys:
                wait_for_output(b'[3/5] Downloading and building packages')
                os.write(terminal, install_keys)
            while True:
                self.assertTrue(select.select([terminal], [], [], max(0, deadline - time.monotonic()))[0], output)
                try:
                    chunk = os.read(terminal, 4096)
                except OSError as exc:
                    if exc.errno != errno.EIO:  # Linux signals terminal EOF this way.
                        raise
                    break
                if not chunk:
                    break
                output.extend(chunk)
            _, status = os.waitpid(pid, 0)
            reaped = True
            return os.waitstatus_to_exitcode(status), output.decode()
        finally:
            os.close(terminal)
            if not reaped:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)

    def test_piped_setup_confirms_toggled_components_before_installing(self):
        status, output = self.run_piped_setup_in_terminal(
            b'\x1b[A', b'\x1b[A', b'\x1b[A', b'\x1b[A', b' ',  # Junie.
            b'\x1bOB', b'\x1b[B', b'\x1b[B', b'\x1b[B', b'\x1b[B', b' ',  # SD eject prevention.
            b'\r\x1b[B', b' ',  # Enter on a checkbox does not install. Skip the status-bar indicator.
            b'\x1b[B', b'\x1b[B', b' ', b'\x1b[B', b' ',  # Claude Code and Grok.
            b'\x1b[B', b' ', b' ', b'\x1b[B', b'\x1b[B', b' ', b'\x1b[B', b' ', b'\x1b[B', b' ', b'\x1b[B', b'\r',  # Recheck Cursor, skip both plugins and T3 Code.
            install_keys=b'\x1b[C',
        )
        self.assertEqual(status, 0, output)
        self.assertNotIn('^[[C', output)
        self.assertNotIn('\x1b[C', output)
        output = re.sub(r'\x1b\[[\d;]*m', '', output)
        first_screen = output.split('Up/Down move', 1)[0].splitlines()
        self.assertEqual([line.strip() for line in first_screen if line.strip()], [
            'SidePulse',
            'macOS integrations',
            '[x] SD card ejection prevention (system-wide, important for deep sleep)',
            '[x] Status-bar indicator',
            'Agent hooks',
            '[x] Codex',
            '[x] Claude Code',
            '[x] Grok',
            '[x] Cursor',
            '[x] Junie',
            '[x] OpenCode',
            '[x] Herdr',
            '[x] T3 Code',
            '> Install',
        ])
        for component in (
            'SD card ejection prevention', 'Claude Code', 'Grok', 'Junie', 'OpenCode', 'Herdr', 'T3 Code', 'Status-bar indicator',
        ):
            self.assertIn(f'> [ ] {component}', output)
        self.assertEqual(output.count('SidePulse\r\n'), 1)
        self.assertEqual(self.calls()[0], [
            'new', ['setup', '--agent', 'codex', '--agent', 'cursor', '--no-sd-eject-guard', '--no-status-bar'],
        ])

    def test_piped_linux_setup_can_skip_all_agents(self):
        status, output = self.run_piped_setup_in_terminal(
            b'\x1b[B', b' ', b'\x1b[B', b' ', b'\x1b[B', b' ', b'\x1b[B', b' ', b'\x1b[B', b' ', b'\x1b[B', b' ', b'\x1b[B', b' ', b'\x1b[B', b' ', b'\x1b[B', b'\r',
            platform='Linux',
        )
        self.assertEqual(status, 0, output)
        self.assertIn('Agent hooks', output)
        self.assertNotIn('macOS integrations', output)
        self.assertNotIn('SD card ejection prevention', output)
        self.assertNotIn('Status-bar indicator', output)
        self.assertEqual(self.calls()[0], ['new', ['setup', '--no-agent-monitor']])

    def test_piped_setup_can_cancel_without_installing(self):
        status, output = self.run_piped_setup_in_terminal(b'q')
        self.assertEqual(status, 0, output)
        self.assertIn(
            '\x1b[1mSide\x1b[38;5;174mP\x1b[38;5;179mu\x1b[38;5;108ml'
            '\x1b[38;5;110ms\x1b[38;5;140me\x1b[0m\r\n',
            output,
        )
        self.assertIn('\x1b[1;38;5;110m  > Install\x1b[0m', output)
        self.assertIn('\x1b[38;5;108m    [x]\x1b[0m', output)
        self.assertIn('SidePulse setup cancelled.', output)
        self.assertFalse(self.install_root.exists())
        self.assertFalse((self.root / 'state').exists())

    def test_piped_setup_respects_no_color(self):
        status, output = self.run_piped_setup_in_terminal(b'q', no_color=True)
        self.assertEqual(status, 0, output)
        self.assertIn('\r\nSidePulse\r\n', output)
        self.assertNotRegex(output, r'\x1b\[[\d;]*m')

    def test_piped_headless_setup_installs_defaults_without_terminal_input(self):
        status, output = self.run_piped_setup_in_terminal(headless=True)
        self.assertEqual(status, 0, output)
        self.assertNotIn('Agent hooks', output)
        self.assertIn('sudo -n -H', output)
        self.assertNotIn('Mock administrator prompt', output)
        self.assertNotRegex(output, r'\x1b\[[\d;]*m')
        self.assertIn('SidePulse is installed.', output)
        self.assertEqual(self.calls(), [
            ['sudo', ['-n', '-H', str(self.install_root / 'venv/bin/sidepulse'), 'sdejectguard', 'start', '--scope', 'system']],
            ['new', ['sdejectguard', 'start', '--scope', 'system']],
            ['new', ['sdejectguard', 'uninstall', '--scope', 'user']],
            ['new', ['setup', '--no-sd-eject-guard']],
            ['new', ['--version']],
        ])

    def test_piped_setup_shows_and_runs_one_sudo_command_for_the_system_guard(self):
        self.install_root = self.root / "SidePulse's install with spaces"
        self.env['SIDEPULSE_INSTALL_ROOT'] = str(self.install_root)
        status, output = self.run_piped_setup_in_terminal(b'\r', developer_tools=True)
        self.assertEqual(status, 0, output)
        self.assertIn('Administrator access is required', output)
        calls = self.calls()
        self.assertEqual([call for call in calls if call[0] == 'sudo'], [
            ['sudo', [
                '-H', '-p', '[SidePulse] Administrator password for %p: ',
                str(self.install_root / 'venv/bin/sidepulse'), 'sdejectguard', 'start', '--scope', 'system',
            ]],
        ])
        before_prompt = output.split('Mock administrator prompt', 1)[0]
        self.assertIn('Mock administrator prompt', output)
        shown_commands = [
            shlex.split(line) for line in before_prompt.splitlines() if line.lstrip().startswith('sudo ')
        ]
        self.assertEqual(shown_commands, [['sudo', *args] for name, args in calls if name == 'sudo'])
        self.assertEqual(calls[-3:-1], [
            ['new', ['sdejectguard', 'uninstall', '--scope', 'user']],
            ['new', ['setup', '--no-sd-eject-guard']],
        ])

    def test_headless_setup_keeps_user_guard_when_sudo_fails(self):
        status, output = self.run_piped_setup_in_terminal(headless=True, sudo_fail=True)
        self.assertNotEqual(status, 0)
        self.assertIn('requires non-interactive sudo', output)
        self.assertNotIn('SidePulse is installed.', output)
        self.assertEqual(self.calls(), [
            ['sudo', ['-n', '-H', str(self.install_root / 'venv/bin/sidepulse'), 'sdejectguard', 'start', '--scope', 'system']],
        ])

    def test_headless_setup_can_skip_system_guard_without_sudo(self):
        status, output = self.run_piped_setup_in_terminal(headless=True, skip_guard=True)
        self.assertEqual(status, 0, output)
        self.assertEqual(self.calls(), [
            ['new', ['setup', '--no-sd-eject-guard']],
            ['new', ['--version']],
        ])

    def test_macos_menu_bar_is_stopped_before_replacement_and_resumed_on_failure(self):
        self.assertEqual(self.install(self.old).returncode, 0)
        stubs = self.root / 'stubs'
        stubs.mkdir()
        (stubs / 'uname').write_text('#!/bin/sh\nprintf "Darwin\\n"\n')
        for name in ('xcode-select', 'xcrun'):
            (stubs / name).write_text('#!/bin/sh\nexit 0\n')
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
            PATH=f'{stubs}:{self.env["PATH"]}',
        )
        self.assertNotEqual(result.returncode, 0)
        actions = [(who, args[0]) for who, args in self.calls()]
        self.assertLess(actions.index(('launchctl', 'bootout')), actions.index(('new', 'setup')))
        self.assertEqual(actions[-1], ('launchctl', 'bootstrap'))
        self.assertTrue(state.exists())
