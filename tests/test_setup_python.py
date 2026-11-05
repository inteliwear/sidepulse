"""Exercise Python discovery and bootstrap through the actual setup script."""
from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/setup.sh"


class SetupPythonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="sidepulse-python-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.install_root = self.root / "install with spaces"
        self.log = self.root / "calls"
        self.env = {
            **os.environ,
            "HOME": str(self.root),
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "PYTHON_BIN": "python3",
            "SIDEPULSE_INSTALL_ROOT": str(self.install_root),
            "SIDEPULSE_TEST_LOG": str(self.log),
        }
        self.stub("uname", 'printf "Darwin\\n"')
        self.stub("xcode-select", "exit 0")
        self.stub("xcrun", "exit 0")
        for name in ("python3", "python3.10", "python3.11", "python3.12", "python3.13", "python3.14"):
            self.stub(name, "exit 1")
        self.managed_python = self.root / "managed python"
        self.good_python(self.managed_python)
        self.uv_source = self.root / "uv-source"
        self.stub(self.uv_source, '''
printf '%s|%s\\n' "$UV_PYTHON_INSTALL_DIR" "$*" >> "$SIDEPULSE_TEST_LOG"
if [ "$2" = install ]; then
    exit "${SIDEPULSE_TEST_UV_FAIL:-0}"
fi
printf '%s\\n' "$SIDEPULSE_TEST_MANAGED_PYTHON"
''')
        self.env["SIDEPULSE_TEST_UV_SOURCE"] = str(self.uv_source)
        self.env["SIDEPULSE_TEST_MANAGED_PYTHON"] = str(self.managed_python)
        self.installer = self.root / "installer"
        self.installer.write_text('''#!/bin/sh
mkdir -p "$UV_UNMANAGED_INSTALL"
cp "$SIDEPULSE_TEST_UV_SOURCE" "$UV_UNMANAGED_INSTALL/uv"
printf 'bootstrap\\n' >> "$SIDEPULSE_TEST_LOG"
''')
        self.env["SIDEPULSE_TEST_INSTALLER"] = str(self.installer)
        self.stub("curl", '''
printf 'curl\\n' >> "$SIDEPULSE_TEST_LOG"
[ "${SIDEPULSE_TEST_CURL_FAIL:-0}" = 0 ] || exit 1
[ "$1" = -fsSL ] && [ "$2" = https://astral.sh/uv/install.sh ] && [ "$3" = -o ] || exit 2
cp "$SIDEPULSE_TEST_INSTALLER" "$4"
''')

    def stub(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/sh\nset -eu\n" + body + "\n")
        path.chmod(0o755)
        return path

    def good_python(self, path):
        # Stop at virtualenv creation, after discovery but before package or
        # service changes. A successful -c models a supported interpreter.
        return self.stub(path, f'''
if [ "$1" = -c ]; then exit 0; fi
printf '%s\\n' {shlex.quote(str(path))} >> "$SIDEPULSE_TEST_LOG"
[ "$1" = -m ] && [ "$2" = venv ] || exit 2
exit 42
''')

    def run_setup(self, **env):
        return subprocess.run(
            ["/bin/sh", str(SCRIPT)], env={**self.env, **env},
            capture_output=True, text=True, timeout=10,
        )

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_supported_default_needs_no_download(self):
        python = self.good_python(self.bin / "python3")
        self.assertEqual(self.run_setup().returncode, 1)
        self.assertEqual(self.calls(), [str(python)])

    def test_missing_developer_tools_explains_recovery_before_any_install(self):
        self.stub("xcode-select", "exit 2")
        result = self.run_setup()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Apple Command Line Tools", result.stderr)
        self.assertIn("xcode-select --install", result.stderr)
        self.assertIn("wait for installation to finish", result.stderr)
        self.assertIn("Run this SidePulse installer again", result.stderr)
        self.assertEqual(self.calls(), [])
        self.assertFalse(self.install_root.exists())

    def test_broken_tool_selection_explains_how_to_select_installed_tools(self):
        self.stub("xcrun", "exit 1")
        result = self.run_setup()
        self.assertEqual(result.returncode, 1)
        self.assertIn("sudo xcode-select --switch /Library/Developer/CommandLineTools", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_old_default_finds_versioned_python(self):
        python = self.good_python(self.bin / "python3.12")
        self.assertEqual(self.run_setup().returncode, 1)
        self.assertEqual(self.calls(), [str(python)])

    def test_missing_python_bootstraps_private_uv_and_python_on_macos(self):
        result = self.run_setup(PYTHON_BIN="missing-python")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Could not create a Python virtual environment", result.stderr)
        self.assertEqual(self.calls(), [
            "curl", "bootstrap",
            f"{self.install_root}/python|python install --no-config --no-bin 3.13",
            f"{self.install_root}/python|python find --no-config --no-project --managed-python 3.13",
            str(self.managed_python),
        ])

    def test_private_uv_is_reused(self):
        uv = self.install_root / "uv/uv"
        uv.parent.mkdir(parents=True)
        self.stub(uv, self.uv_source.read_text())
        self.assertEqual(self.run_setup().returncode, 1)
        self.assertNotIn("curl", self.calls())
        self.assertEqual(self.calls()[-1], str(self.managed_python))

    def test_download_failure_stops_before_virtualenv_creation(self):
        result = self.run_setup(SIDEPULSE_TEST_CURL_FAIL="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Could not install Python", result.stderr)
        self.assertEqual(self.calls(), ["curl"])
        self.assertFalse((self.install_root / "venv").exists())

    def test_python_install_failure_stops_before_discovery(self):
        result = self.run_setup(SIDEPULSE_TEST_UV_FAIL="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Could not install Python", result.stderr)
        self.assertFalse(any("python find" in call for call in self.calls()))

    def test_bootstrapped_interpreter_is_checked(self):
        result = self.run_setup(SIDEPULSE_TEST_MANAGED_PYTHON="missing-python")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Could not install Python", result.stderr)
        self.assertNotIn(str(self.managed_python), self.calls())

    def test_linux_requires_python_without_downloading(self):
        self.stub("uname", 'printf "Linux\\n"')
        result = self.run_setup()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Install it and run setup again", result.stderr)
        self.assertEqual(self.calls(), [])
