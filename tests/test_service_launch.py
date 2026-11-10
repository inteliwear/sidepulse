from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sidepulse.service_launch import (
    SERVICE_DISPLAY_NAME,
    SERVICE_LABEL,
    build_service_launcher_script,
    install_launch_agent,
    service_command,
)


class MacServiceLaunchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="sidepulse-service-test-")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home with spaces"
        self.launcher = self.home / "data/service" / SERVICE_DISPLAY_NAME
        self.patch_home = patch.object(Path, "home", return_value=self.home)
        self.patch_home.start()
        self.addCleanup(self.patch_home.stop)
        self.patch_launcher = patch(
            "sidepulse.service_launch.service_launcher_path", return_value=self.launcher,
        )
        self.patch_launcher.start()
        self.addCleanup(self.patch_launcher.stop)
        self.patch_state = patch(
            "sidepulse.service_launch.default_state_dir", return_value=self.home / "state",
        )
        self.patch_state.start()
        self.addCleanup(self.patch_state.stop)

    def test_existing_python_agent_migrates_to_named_launcher(self):
        target = self.home / "Library/LaunchAgents" / f"{SERVICE_LABEL}.plist"
        target.parent.mkdir(parents=True)
        target.write_bytes(plistlib.dumps({
            "Label": SERVICE_LABEL, "ProgramArguments": service_command(),
        }))
        with (
            patch("sidepulse.service_launch.subprocess.check_output", return_value="123\n"),
            patch("sidepulse.service_launch.subprocess.run", return_value=SimpleNamespace(returncode=0, stderr="")) as run,
        ):
            result = install_launch_agent(start=True, dry_run=False)

        self.assertTrue(result.changed)
        self.assertTrue(result.started)
        payload = plistlib.loads(target.read_bytes())
        self.assertEqual(payload["Label"], SERVICE_LABEL)
        self.assertEqual(payload["ProgramArguments"], [str(self.launcher)])
        self.assertTrue(payload["KeepAlive"])
        self.assertTrue(os.access(self.launcher, os.X_OK))
        self.assertEqual([call.args[0][:2] for call in run.call_args_list], [
            ["launchctl", "bootout"], ["launchctl", "bootstrap"],
        ])

    @unittest.skipIf(os.name == 'nt', 'macOS/POSIX desktop launcher or UI socket')
    def test_launcher_executes_original_service_command_with_spaces(self):
        interpreter = self.home / "python with spaces"
        interpreter.parent.mkdir(parents=True)
        interpreter.write_text(
            f"#!{sys.executable}\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n"
        )
        interpreter.chmod(0o755)
        with patch("sidepulse.service_launch.sys.executable", str(interpreter)):
            install_launch_agent(start=False, dry_run=False)
        result = subprocess.run([str(self.launcher)], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), ["-m", "sidepulse", "service", "run"])

    def test_dry_run_writes_no_launcher_or_plist(self):
        result = install_launch_agent(start=True, dry_run=True)
        self.assertTrue(result.changed)
        self.assertFalse(result.started)
        self.assertFalse(self.home.exists())

    def test_reinstall_is_unchanged_and_repairs_missing_launcher(self):
        install_launch_agent(start=False, dry_run=False)
        self.assertFalse(install_launch_agent(start=False, dry_run=False).changed)
        self.launcher.unlink()
        self.assertTrue(install_launch_agent(start=False, dry_run=False).changed)
        self.assertTrue(os.access(self.launcher, os.X_OK))

    @unittest.skipIf(os.name == 'nt', 'macOS/POSIX desktop launcher or UI socket')
    def test_reinstall_repairs_launcher_permissions(self):
        install_launch_agent(start=False, dry_run=False)
        self.launcher.chmod(0o644)
        self.assertTrue(install_launch_agent(start=False, dry_run=False).changed)
        self.assertTrue(os.access(self.launcher, os.X_OK))

    def test_frozen_service_uses_application_cli(self):
        with patch("sidepulse.service_launch.sys.frozen", True, create=True):
            self.assertEqual(service_command(), [sys.executable, "service", "run"])
            self.assertNotIn("-m sidepulse", build_service_launcher_script())
