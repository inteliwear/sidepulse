"""Adapt Antigravity lifecycle hooks to SidePulse events."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .hook import routed_hook_payload, write_hook_line, write_hook_status_audit
from .ipc import send_hook_event


EVENT_ALIASES = {
    "PreInvocation": "UserPromptSubmit",
    "PostInvocation": "PostToolUse",
    "PreToolUse": "PreToolUse",
    "PostToolUse": "PostToolUse",
    "Stop": "Stop",
}


def normalize_payload(event_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    normalized["hook_event_name"] = EVENT_ALIASES.get(event_name, event_name)
    normalized["session_id"] = str(payload.get("conversationId") or "")
    normalized["transcript_path"] = str(payload.get("transcriptPath") or "")
    normalized["model"] = str(payload.get("modelName") or "")
    normalized["agent_origin"] = "Antigravity"
    normalized["agent_origin_kind"] = "antigravity_app"
    normalized["agent_origin_source"] = "hook:antigravity"
    normalized["agent_origin_confidence"] = "explicit"

    workspace_paths = payload.get("workspacePaths")
    if isinstance(workspace_paths, list) and workspace_paths:
        normalized["cwd"] = str(workspace_paths[0])

    tool_call = payload.get("toolCall")
    if isinstance(tool_call, dict):
        normalized["tool_name"] = str(tool_call.get("name") or "")
        normalized["tool_input"] = tool_call.get("args")

    error = payload.get("error")
    if isinstance(error, str) and error:
        normalized["message"] = error
        normalized["tool_response"] = {"error": error}

    if event_name == "Stop" and not payload.get("fullyIdle", True):
        normalized["hook_event_name"] = "PostToolUse"
        normalized["message"] = "Background work is still running"

    return normalized


def response_for_event(event_name: str) -> dict[str, Any]:
    if event_name == "PreToolUse":
        return {"decision": "allow"}
    if event_name == "PreInvocation":
        return {"injectSteps": []}
    if event_name == "PostInvocation":
        return {"injectSteps": [], "terminationBehavior": ""}
    if event_name == "Stop":
        return {"decision": ""}
    return {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--event", required=True)
    parser.add_argument("--log", required=True, type=Path)
    args = parser.parse_args(argv)

    try:
        raw = json.loads(sys.stdin.read() or "{}")
        payload = raw if isinstance(raw, dict) else {}
        normalized = normalize_payload(args.event, payload)
        provider, log_path, line = routed_hook_payload(
            "antigravity",
            args.log.expanduser(),
            json.dumps(normalized, separators=(",", ":"), ensure_ascii=False),
        )
        send_hook_event(provider, line)
        write_hook_line(log_path, line)
        write_hook_status_audit(provider, line)
    except Exception:
        pass

    sys.stdout.write(json.dumps(response_for_event(args.event), separators=(",", ":")))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
