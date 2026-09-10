"""Adapt Cursor lifecycle hooks to SidePulse events."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .hook import routed_hook_payload, write_hook_line, write_hook_status_audit
from .ipc import send_hook_event


EVENT_ALIASES = {
    "sessionStart": "SessionStart",
    "sessionEnd": "SessionEnd",
    "beforeSubmitPrompt": "UserPromptSubmit",
    "preToolUse": "PreToolUse",
    "beforeShellExecution": "PreToolUse",
    "afterShellExecution": "PostToolUse",
    "afterFileEdit": "PostToolUse",
    "postToolUse": "PostToolUse",
    "postToolUseFailure": "StopFailure",
    "stop": "Stop",
}


def normalize_payload(event_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    normalized["hook_event_name"] = EVENT_ALIASES.get(event_name, event_name)
    normalized["session_id"] = str(
        payload.get("conversation_id") or payload.get("conversationId") or ""
    )
    normalized["agent_origin"] = "Cursor"
    normalized["agent_origin_kind"] = "cursor_app"
    normalized["agent_origin_source"] = "hook:cursor"
    normalized["agent_origin_confidence"] = "explicit"

    workspace_roots = payload.get("workspace_roots") or payload.get("workspaceRoots")
    if isinstance(workspace_roots, list) and workspace_roots:
        normalized["cwd"] = str(workspace_roots[0])
    elif payload.get("workspace_root"):
        normalized["cwd"] = str(payload["workspace_root"])

    prompt = payload.get("prompt")
    if isinstance(prompt, str):
        normalized["prompt"] = prompt

    if event_name in {"beforeShellExecution", "preToolUse"}:
        normalized["tool_name"] = str(payload.get("tool_name") or "shell")
        normalized["tool_input"] = payload.get("tool_input") or {
            "command": payload.get("command")
        }
    elif event_name in {
        "afterShellExecution",
        "afterFileEdit",
        "postToolUse",
        "postToolUseFailure",
    }:
        normalized["tool_name"] = str(payload.get("tool_name") or event_name)
        normalized["tool_response"] = {
            "status": payload.get("status"),
            "exit_code": payload.get("exit_code"),
            "error": payload.get("error_message"),
        }

    if event_name == "postToolUseFailure" and payload.get("error_message"):
        normalized["message"] = str(payload["error_message"])

    status = payload.get("status")
    if event_name == "stop" and isinstance(status, str):
        normalized["message"] = f"Cursor stopped: {status}"

    return normalized


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
            "cursor",
            args.log.expanduser(),
            json.dumps(normalized, separators=(",", ":"), ensure_ascii=False),
        )
        try:
            write_hook_line(log_path, line)
        except Exception:
            pass
        try:
            write_hook_status_audit(provider, line)
        except Exception:
            pass
        try:
            send_hook_event(provider, line)
        except Exception:
            pass
    except Exception:
        pass

    sys.stdout.write("{}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
