"""Translate Google Antigravity hook input into SidePulse session events."""

from __future__ import annotations

from typing import Any

from .providers import ANTIGRAVITY_EVENTS


def normalize_payload(event: str | None, raw: dict[str, Any]) -> dict[str, Any] | None:
    session = raw.get("conversationId")
    if event not in ANTIGRAVITY_EVENTS or not isinstance(session, str) or not session.strip():
        return None

    transcript = str(raw.get("transcriptPath", "")).replace("\\", "/")
    data_dir = transcript.rpartition("/brain/")[0].rsplit("/", 1)[-1]
    origin = {
        "antigravity": "Antigravity App",
        "antigravity-cli": "Antigravity CLI",
        "antigravity-ide": "Antigravity IDE",
    }.get(data_dir, "Antigravity")
    payload = {"hook_event_name": event, "session_id": session, "agent_origin": origin}
    roots = raw.get("workspacePaths")
    if isinstance(roots, list) and roots and isinstance(roots[0], str):
        payload["cwd"] = roots[0]

    if event == "PreInvocation":
        payload["prompt"] = raw.get("lastUserInput")
    elif event in {"PreToolUse", "PostToolUse"}:
        tool = raw.get("toolCall")
        if isinstance(tool, dict):
            payload["tool_name"] = tool.get("name")
            if tool.get("name") == "ask_question":
                payload["tool_input"] = tool.get("args")
        if event == "PostToolUse" and raw.get("error"):
            payload.update(hook_event_name="PostToolUseFailure", error_details=raw["error"])
    elif event == "Stop":
        reason = raw.get("terminationReason")
        if raw.get("error") or reason in {"error", "max_steps_exceeded"}:
            payload.update(hook_event_name="StopFailure", error_details=raw.get("error") or reason)
        elif raw.get("fullyIdle") is False:
            payload["sidepulse_status"] = "working"
    return payload
