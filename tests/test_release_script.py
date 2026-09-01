from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


class ReleaseScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.script = cls.root / "scripts" / "release.sh"

    def test_shell_syntax(self) -> None:
        result = subprocess.run(
            ["bash", "-n", str(self.script)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_help_describes_safe_scope(self) -> None:
        result = subprocess.run(
            ["bash", str(self.script), "--help"],
            cwd=self.root,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("wheel", result.stdout)
        self.assertIn("source archive", result.stdout)
        self.assertIn("1.YYYYMMDD.SECONDS", result.stdout)
        self.assertIn("does not tag, publish, or push", result.stdout)

    def test_unknown_option_fails_without_building(self) -> None:
        result = subprocess.run(
            ["bash", str(self.script), "--not-a-real-option"],
            cwd=self.root,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown option", result.stderr)

    def test_invalid_calendar_version_fails_without_building(self) -> None:
        result = subprocess.run(
            ["bash", str(self.script), "--version", "1.20260230.3600"],
            cwd=self.root,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("real UTC date", result.stderr)


if __name__ == "__main__":
    unittest.main()
