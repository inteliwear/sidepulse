from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(sys.platform == "darwin", "requires macOS Disk Arbitration")
class SdEjectGuardProcessTests(unittest.TestCase):
    def test_restarts_after_disk_arbitration_daemon_changes(self) -> None:
        source = Path(__file__).resolve().parents[1] / "src/sidepulse/resources/sd_eject_guard.c"
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            pidfile = base / "diskarbitrationd.pid"
            binary = base / "sd_eject_guard"
            pidfile.write_text("111\n", encoding="ascii")
            subprocess.run(
                [
                    "clang",
                    "-O2",
                    f'-DSD_EJECT_GUARD_DA_PIDFILE="{pidfile}"',
                    "-o",
                    str(binary),
                    str(source),
                    "-framework",
                    "DiskArbitration",
                    "-framework",
                    "CoreFoundation",
                ],
                check=True,
            )

            process = subprocess.Popen(
                [str(binary)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
            try:
                assert process.stdout is not None
                self.assertIn("watching Disk Arbitration PID 111", process.stdout.readline())
                pidfile.write_text("222\n", encoding="ascii")
                _, error = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0)
                self.assertIn("changed from 111 to 222; restarting guard", error)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
