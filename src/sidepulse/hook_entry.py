"""Process entry point for supported SidePulse agent hooks."""

from __future__ import annotations

import sys
from pathlib import Path


def _load_hook_main():
    if __package__:
        from .hook import hook_log_main
        return hook_log_main

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from sidepulse.hook import hook_log_main
    return hook_log_main


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        provider = args[args.index("--provider") + 1]
        log_path = Path(args[args.index("--log") + 1]).expanduser()
    except (ValueError, IndexError):
        return 0

    return _load_hook_main()(provider, log_path)


if __name__ == "__main__":
    raise SystemExit(main())
