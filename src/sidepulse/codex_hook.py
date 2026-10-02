from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any


TURN_CONTEXT_READ_LIMIT = 8 * 1024 * 1024


def normalize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Distinguish automatic sandbox reviews from requests to a person.

    Codex's PermissionRequest wire payload does not include the reviewer. Read
    only the matching turn's reviewer metadata from a bounded transcript tail.
    The transcript format is not stable, so missing/changed metadata retains
    the existing manual-approval behavior. App-specific and Computer Use
    approvals may have their own reviewer and must not inherit this inference.
    """
    normalized = dict(payload)
    normalized.pop("sidepulse_approvals_reviewer", None)
    if normalized.get("hook_event_name") != "PermissionRequest":
        return normalized
    if not uses_turn_approval_reviewer(normalized.get("tool_name")):
        return normalized

    reviewer = turn_approval_reviewer(normalized)
    if reviewer is not None:
        normalized["sidepulse_approvals_reviewer"] = reviewer
    return normalized


def uses_turn_approval_reviewer(tool_name: object) -> bool:
    if not isinstance(tool_name, str):
        return False
    if tool_name in {"Bash", "apply_patch"}:
        return True
    return tool_name.startswith("mcp__") and not tool_name.startswith(
        ("mcp__codex_apps__", "mcp__cua_repl__")
    )


def turn_approval_reviewer(payload: dict[str, Any]) -> str | None:
    path = payload.get("transcript_path")
    turn_id = payload.get("turn_id")
    if not isinstance(path, str) or not path or not isinstance(turn_id, str) or not turn_id:
        return None

    try:
        # Nonblocking open plus a regular-file check also avoids hanging on a
        # pipe or device supplied in a malformed hook payload.
        fd = os.open(Path(path), os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                return None
            start = max(0, info.st_size - TURN_CONTEXT_READ_LIMIT)
            handle.seek(start)
            data = handle.read(TURN_CONTEXT_READ_LIMIT)
        if data and not data.endswith(b"\n"):
            return None  # An in-progress append may contain a newer setting.
        lines = data.splitlines()
        if start:
            lines = lines[1:]  # The first line may have been cut in the middle.
        for line in reversed(lines):
            if b'"turn_context"' not in line:
                continue
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError):
                return None
            if not isinstance(row, dict) or row.get("type") != "turn_context":
                continue
            context = row.get("payload")
            if not isinstance(context, dict) or context.get("turn_id") != turn_id:
                continue
            reviewer = context.get("approvals_reviewer")
            # Do not inherit an older setting if the latest context changed.
            return reviewer if reviewer in ("user", "auto_review") else None
    except (OSError, ValueError):
        pass
    return None


def is_automatic_permission(provider: str, event: str, payload: dict[str, Any]) -> bool:
    return (
        provider == "codex"
        and event == "PermissionRequest"
        and uses_turn_approval_reviewer(payload.get("tool_name"))
        and payload.get("sidepulse_approvals_reviewer") == "auto_review"
    )
