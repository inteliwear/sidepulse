"""Status-only agent-status publishing, when a chat log is not wanted.

SidePulse's codex/claude/grok hook integrations write agent session content
(prompts, assistant messages, tool inputs) to local JSONL decision logs. The
Cursor integration deliberately does **not**: it publishes only a status
transition (``working`` / ``done`` / ``ask`` / ``blocked`` / ``idle``) to the
status-bar unix socket. Nothing is written to disk and no message content
ever crosses the wire, so there is nothing to clear in more than one place.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

from .ipc import send_hook_event

VALID_STATUS = ("working", "done", "ask", "blocked", "idle")


def publish_status(
    status: str,
    *,
    provider: str = "cursor",
    session_id: Optional[str] = None,
    cwd: Optional[str] = None,
) -> bool:
    """Publish a status-only transition to the status-bar socket.

    The event carries an explicit ``sidepulse_status`` token and no message
    text, so the status bar can derive the aggregate mode without storing any
    chat content. Returns ``True`` when the status bar accepted the event,
    ``False`` when it is not running or the status token is invalid.
    """
    if status not in VALID_STATUS:
        return False
    line = {
        "logged_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "hook_event_name": "Notification",
        "session_id": session_id or provider,
        "cwd": cwd or os.getcwd(),
        "sidepulse_status": status,
    }
    return send_hook_event(provider, line)
