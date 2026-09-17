"""Entry point for the self-contained macOS SidePulse application."""

import sys

from sidepulse.cli import sidepulse_main
from sidepulse.status_bar import main as status_bar_main


if __name__ == "__main__":
    # Finder launches the app without arguments. The same executable is exposed
    # as /usr/local/bin/sidepulse by the installer for command-line use.
    raise SystemExit(
        sidepulse_main() if len(sys.argv) > 1 else status_bar_main(show_settings=True)
    )
