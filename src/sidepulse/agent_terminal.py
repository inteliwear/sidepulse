from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from .models import parse_datetime
from .origin import is_junie_process, process_ancestry, process_basename
from .providers import detect_log_path
from .settings import TERMINAL_APP_GHOSTTY, TERMINAL_APP_ITERM, TERMINAL_APP_TERMINAL
from .text_utils import read_recent_lines


@dataclass(frozen=True)
class AgentTerminal:
    tty: str
    app: str | None
    cwd: str = ""
    title: str = ""


def junie_session_terminal(session_id: str | None) -> AgentTerminal | None:
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
        terminal = live_agent_terminal("junie", pid, logged_at)
        if terminal is not None:
            return terminal
        unavailable_processes.add(pid)
    return None


def claude_session_terminal(session_id: str | None) -> AgentTerminal | None:
    if not session_id:
        return None
    config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    candidates: list[tuple[datetime, int, dict]] = []
    try:
        session_files = list((config_dir.expanduser() / "sessions").glob("*.json"))
    except (OSError, RuntimeError):
        return None

    for path in session_files:
        if not path.stem.isdecimal():
            continue
        try:
            row = json.loads(path.read_text())
            if not isinstance(row, dict) or row.get("sessionId") != session_id:
                continue
            pid = row.get("pid")
            started_ms = row.get("startedAt")
            if type(pid) is not int or pid <= 1 or str(pid) != path.stem or type(started_ms) is not int:
                continue
            started_at = datetime.fromtimestamp(started_ms / 1000, timezone.utc)
        except (OSError, ValueError, OverflowError):
            continue
        candidates.append((started_at, pid, row))

    # A second resume can leave two processes with the same Claude session ID.
    # Favor the original instance while it is still attached to a terminal.
    for started_at, pid, row in sorted(candidates, key=lambda candidate: candidate[0]):
        terminal = live_agent_terminal("claude", pid, started_at)
        if terminal is not None:
            cwd, title = row.get("cwd"), row.get("name")
            return replace(
                terminal,
                cwd=cwd if isinstance(cwd, str) else "",
                title=title if isinstance(title, str) else "",
            )
    return None


def live_agent_terminal(provider: str, pid: int, logged_at: datetime) -> AgentTerminal | None:
    ancestry = process_ancestry(pid)
    if not ancestry:
        return None
    process = ancestry[0]
    if provider == "junie" and not is_junie_process(process):
        return None
    if provider == "claude" and process_basename(process) not in {"claude", "claude-code"}:
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

    return AgentTerminal(f"/dev/{tty}", app)
