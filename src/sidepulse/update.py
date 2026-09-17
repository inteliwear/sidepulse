from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path


SETUP_URL = "https://sidepulse.io/setup.sh"


def update_installation(*, dry_run: bool = False) -> int:
    print(f"Updating with: curl -fsSL {SETUP_URL} | bash", flush=True)
    if dry_run:
        print("Would run the same installer as one-command setup, using its install path and environment overrides.")
        return 0
    try:
        # Download completely before executing: a failed or interrupted curl
        # must not run a partial installer or appear to succeed via a pipe.
        with tempfile.TemporaryDirectory(prefix="sidepulse-update-") as directory:
            script = Path(directory) / "setup.sh"
            subprocess.run(
                ["curl", "-fsSL", "-H", "Cache-Control: no-cache", SETUP_URL,
                 "-o", str(script)], check=True,
            )
            subprocess.run(["bash", str(script)], check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"SidePulse update failed: {exc}", file=sys.stderr)
        return 1
    return 0
