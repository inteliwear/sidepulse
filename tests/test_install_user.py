from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class UserInstallerTests(unittest.TestCase):
    def test_installer_uses_isolated_venv_and_user_bin(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = root / "scripts" / "install-user.sh"
        text = script.read_text()
        self.assertIn('"$PYTHON_BIN" -m venv "$VENV_DIR"', text)
        self.assertIn('"$VENV_DIR/bin/python" -m pip install', text)
        self.assertNotIn("--break-system-packages", text)

    def test_installer_shell_syntax(self) -> None:
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            ["sh", "-n", str(root / "scripts" / "install-user.sh")],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_curl_setup_installs_github_checkout_in_local_venv(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = root / "scripts" / "setup.sh"
        text = script.read_text()
        self.assertIn('$HOME/.local/share/sidepulse', text)
        self.assertIn('git+https://github.com/inteliwear/sidepulse.git', text)
        self.assertIn('SIDEPULSE_INSTALL_SPEC', text)
        self.assertIn("sys.version_info < (3, 10)", text)
        self.assertIn('"$VENV/bin/sidepulse" setup', text)
        self.assertNotIn("sudo", text)

    def test_curl_setup_shell_syntax(self) -> None:
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            ["sh", "-n", str(root / "scripts" / "setup.sh")],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
