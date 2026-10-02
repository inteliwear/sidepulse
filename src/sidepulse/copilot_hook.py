"""Adapt Copilot CLI/app lifecycle hooks to SidePulse's status vocabulary."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .providers import canonical_event_name, normalize_event_payload


EVENT_ALIASES = {
    "userPromptSubmitted": "UserPromptSubmit",
    "agentStop": "Stop",
    "errorOccurred": "StopFailure",
}


def normalize_payload(event_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    event = EVENT_ALIASES.get(event_name) or canonical_event_name(event_name) or event_name
    timestamp = payload.get("timestamp")
    # Copilot's native wire format uses epoch milliseconds, whereas the
    # PascalCase compatibility format uses an ISO timestamp.
    if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool):
        timestamp = datetime.fromtimestamp(timestamp / 1000, timezone.utc).isoformat()
    normalized = normalize_event_payload(payload, event, timestamp)
    normalized["hook_event_name"] = event
    if timestamp is not None:
        normalized["timestamp"] = timestamp
    for source, target in (
        ("toolArgs", "tool_input"),
        ("toolResult", "tool_response"),
        ("transcriptPath", "transcript_path"),
        ("initialPrompt", "prompt"),
        ("response", "last_assistant_message"),
    ):
        if target not in normalized and source in payload:
            normalized[target] = payload[source]
    tool_input = normalized.get("tool_input")
    if isinstance(tool_input, str):
        try:
            normalized["tool_input"] = json.loads(tool_input)
        except json.JSONDecodeError:
            pass

    # Copilot app/IDE hosts emit SessionStart after UserPromptSubmitted. A
    # supplied initial prompt means the agent is already processing that turn.
    if event == "SessionStart" and normalized.get("prompt"):
        normalized["sidepulse_mode"] = "working"
    if event == "PreToolUse" and normalized.get("tool_name") in {
        "ask_user", "AskUserQuestion", "askUserQuestion",
    }:
        normalized["sidepulse_mode"] = "waiting_for_input"
    if event == "Notification":
        if normalized.get("notification_type") in {"permission_prompt", "elicitation_dialog"}:
            normalized["sidepulse_mode"] = "waiting_for_input"
    if event == "SessionEnd":
        reason = payload.get("reason")
        if reason in {"abort", "user_exit"}:
            normalized["hook_event_name"] = "Interrupt"
        elif reason in {"error", "timeout"}:
            normalized["hook_event_name"] = "StopFailure"
    if event == "StopFailure" or event == "PostToolUseFailure":
        error = payload.get("error")
        if error is not None and "message" not in normalized:
            normalized["message"] = error if isinstance(error, str) else json.dumps(error)
    return normalized
