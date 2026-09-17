from __future__ import annotations

import io
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from sidepulse.cli import sidepulse_main
from sidepulse.update import SETUP_URL, update_installation


class UpdateTests(unittest.TestCase):
    def test_every_update_downloads_and_executes_the_internet_script(self):
        downloads = []
        executed = []

        def run(command, **kwargs):
            self.assertTrue(kwargs['check'])
            if command[0] == 'curl':
                self.assertIn(SETUP_URL, command)
                self.assertIn('Cache-Control: no-cache', command)
                script = Path(command[command.index('-o') + 1])
                downloads.append(script)
                script.write_text(f'echo latest-script-{len(downloads)}\n')
            elif command[0] == 'bash':
                executed.append(Path(command[1]).read_text())
            else:
                self.fail(f'Unexpected command: {command}')
            return subprocess.CompletedProcess(command, 0)

        with patch('sidepulse.update.subprocess.run', side_effect=run):
            self.assertEqual(update_installation(), 0)
            self.assertEqual(update_installation(), 0)
        self.assertEqual(executed, ['echo latest-script-1\n', 'echo latest-script-2\n'])
        self.assertTrue(all(not script.exists() for script in downloads))

    def test_partial_download_failure_never_executes_bash(self):
        commands = []

        def run(command, **kwargs):
            commands.append(command)
            Path(command[command.index('-o') + 1]).write_text('partial script')
            raise subprocess.CalledProcessError(22, command)

        with (
            patch('sidepulse.update.subprocess.run', side_effect=run),
            patch('sys.stderr', new_callable=io.StringIO),
        ):
            self.assertEqual(update_installation(), 1)
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0][0], 'curl')

    def test_installer_failure_is_reported(self):
        with (
            patch('sidepulse.update.subprocess.run', side_effect=[
                subprocess.CompletedProcess([], 0),
                subprocess.CalledProcessError(1, ['bash', 'setup.sh']),
            ]),
            patch('sys.stderr', new_callable=io.StringIO) as stderr,
        ):
            self.assertEqual(update_installation(), 1)
            self.assertIn('update failed', stderr.getvalue())

    def test_missing_curl_is_reported(self):
        with (
            patch('sidepulse.update.subprocess.run', side_effect=FileNotFoundError('curl')),
            patch('sys.stderr', new_callable=io.StringIO),
        ):
            self.assertEqual(update_installation(), 1)

    def test_dry_run_does_not_download_or_execute_anything(self):
        with patch('sidepulse.update.subprocess.run') as run:
            self.assertEqual(sidepulse_main(['update', '--dry-run']), 0)
            run.assert_not_called()

    def test_cli_dispatches_update(self):
        with patch('sidepulse.update.update_installation', return_value=7) as update:
            self.assertEqual(sidepulse_main(['update']), 7)
            update.assert_called_once_with(dry_run=False)
