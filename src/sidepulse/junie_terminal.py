from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone

from .models import parse_datetime
from .origin import is_junie_process, process_ancestry
from .providers import detect_log_path
from .settings import TERMINAL_APP_GHOSTTY, TERMINAL_APP_ITERM, TERMINAL_APP_TERMINAL
from .text_utils import read_recent_lines


@dataclass(frozen=True)
class JunieTerminal:
    tty: str
    app: str | None


def junie_session_terminal(session_id: str | None) -> JunieTerminal | None:
    if not session_id:
        return None
    try:
        lines = read_recent_lines(detect_log_path("junie"), 5000)
    except OSError:
        return None

    unavailable_processes: set[int] = set()
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        pid = row.get("sidepulse_junie_process_id")
        if row.get("session_id") != session_id:
            if type(pid) is int and row.get("session_id"):
                unavailable_processes.add(pid)
            continue
        if row.get("hook_event_name") == "SessionEnd":
            if type(pid) is not int:
                return None
            unavailable_processes.add(pid)
            continue
        if type(pid) is not int or pid <= 1 or pid in unavailable_processes:
            continue
        logged_at = parse_datetime(
            row.get("logged_at"), fallback=datetime.min.replace(tzinfo=timezone.utc)
        )
        terminal = live_junie_terminal(pid, logged_at)
        if terminal is not None:
            return terminal
        unavailable_processes.add(pid)
    return None


def live_junie_terminal(pid: int, logged_at: datetime) -> JunieTerminal | None:
    ancestry = process_ancestry(pid)
    if not ancestry or not is_junie_process(ancestry[0]):
        return None
    try:
        result = subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "uid=", "-o", "tty=", "-o", "lstart="],
            capture_output=True,
            check=True,
            text=True,
            timeout=0.4,
            env={**os.environ, "LC_ALL": "C"},
        )
        uid, tty, started = result.stdout.strip().split(None, 2)
        start_time = datetime.strptime(started, "%a %b %d %H:%M:%S %Y").astimezone()
    except (OSError, subprocess.SubprocessError, ValueError):
        return None

    if uid != str(os.getuid()) or start_time > logged_at or not re.fullmatch(r"ttys[0-9]+", tty):
        return None

    app = None
    for info in ancestry[1:]:
        command = info.command.casefold()
        if "/terminal.app/" in command:
            app = TERMINAL_APP_TERMINAL
        elif "/iterm.app/" in command or "/iterm2.app/" in command:
            app = TERMINAL_APP_ITERM
        elif "/ghostty.app/" in command:
            app = TERMINAL_APP_GHOSTTY
        if app:
            break

    return JunieTerminal(f"/dev/{tty}", app)
