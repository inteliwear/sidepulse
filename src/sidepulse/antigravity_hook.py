"""Adapt Antigravity lifecycle hooks to SidePulse events."""

from __future__ import annotations

from typing import Any

ANTIGRAVITY_ERROR_REASONS = frozenset(
    {
        "ERROR",
        "TERMINAL_CUSTOM_HOOK",
        "MAX_INVOCATIONS",
        "MAX_FORCED_INVOCATIONS",
        "MAX_TOKEN_BUDGET_EXCEEDED",
    }
)

PRE_TOOL_STDOUT = '{"decision":"ask"}\n'
DEFAULT_STDOUT = "{}\n"


def hook_stdout(
    event_name: str | None = None,
    payload: dict[str, Any] | None = None,
) -> str:
    """Return the JSON stdout response Antigravity expects for a hook event.

    Antigravity's PreToolUse contract treats an omitted ``decision`` as a hard
    denial, whereas ``{"decision":"ask"}`` defers to Antigravity's normal
    permission policy and "Always Allow" cache without auto-approving restricted
    actions.
    """
    effective = _effective_event_name(event_name, payload or {})
    if effective == "PreToolUse":
        return PRE_TOOL_STDOUT
    return DEFAULT_STDOUT


def _effective_event_name(event_name: str | None, payload: dict[str, Any]) -> str:
    for candidate in (
        event_name,
        payload.get("hookEventName"),
        payload.get("hook_event_name"),
    ):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


def origin_from_antigravity_paths(payload: dict[str, Any]) -> tuple[str, str] | None:
    for key in ("transcriptPath", "artifactDirectoryPath", "transcript_path"):
        raw_path = payload.get(key)
        if not isinstance(raw_path, str) or not raw_path:
            continue
        lowered = raw_path.replace("\\", "/").lower()
        if "/.gemini/antigravity-cli/" in lowered:
            return ("antigravity_cli", "Antigravity CLI")
        if "/.gemini/antigravity-ide/" in lowered:
            return ("antigravity_ide", "Antigravity IDE")
        if "/.gemini/antigravity/" in lowered:
            return ("antigravity_app", "Antigravity App")
    return None


def _extract_question_message(tool_input: Any) -> str | None:
    if not isinstance(tool_input, dict):
        return None
    questions = tool_input.get("questions")
    if not isinstance(questions, list):
        return None
    for item in questions:
        if isinstance(item, dict):
            question = item.get("question")
            if isinstance(question, str) and question.strip():
                return question.strip()
    return None


def normalize_payload(
    event_name: str | None,
    payload: dict[str, Any],
    *,
    fallback_cwd: str | None = None,
) -> dict[str, Any]:
    normalized = dict(payload)
    raw_event = _effective_event_name(event_name, payload)

    conversation_id = (
        payload.get("conversationId")
        or payload.get("conversation_id")
        or payload.get("sessionId")
        or payload.get("session_id")
    )
    if isinstance(conversation_id, str) and conversation_id:
        normalized["session_id"] = conversation_id

    cwd = payload.get("cwd")
    if not (isinstance(cwd, str) and cwd):
        workspace_paths = payload.get("workspacePaths") or payload.get("workspace_paths")
        if isinstance(workspace_paths, list):
            for entry in workspace_paths:
                if isinstance(entry, str) and entry:
                    cwd = entry
                    break
    if not (isinstance(cwd, str) and cwd) and isinstance(fallback_cwd, str) and fallback_cwd:
        cwd = fallback_cwd
    if isinstance(cwd, str) and cwd:
        normalized["cwd"] = cwd

    last_user_input = payload.get("lastUserInput")
    if isinstance(last_user_input, str) and last_user_input and "prompt" not in normalized:
        normalized["prompt"] = last_user_input

    if "agent_origin" not in normalized and "agentOrigin" not in normalized:
        surface = origin_from_antigravity_paths(payload)
        if surface is not None:
            kind, label = surface
            normalized["agent_origin"] = label
            normalized["agent_origin_kind"] = kind
            normalized["agent_origin_source"] = "hook:antigravity"
            normalized["agent_origin_confidence"] = "explicit"

    tool_call = payload.get("toolCall")
    if isinstance(tool_call, dict):
        tool_name = tool_call.get("name")
        if isinstance(tool_name, str) and tool_name:
            normalized.setdefault("tool_name", tool_name)
        if "args" in tool_call and "tool_input" not in normalized:
            normalized["tool_input"] = tool_call.get("args")
        tool_id = tool_call.get("id")
        if isinstance(tool_id, str) and tool_id:
            normalized.setdefault("tool_use_id", tool_id)

    if raw_event == "PreInvocation":
        normalized["hook_event_name"] = "UserPromptSubmit"
    elif raw_event == "PreToolUse":
        normalized["hook_event_name"] = "PreToolUse"
        if normalized.get("tool_name") == "ask_question" and not normalized.get("message"):
            question = _extract_question_message(normalized.get("tool_input"))
            if question:
                normalized["message"] = question
    elif raw_event == "PostToolUse":
        error = payload.get("error")
        if isinstance(error, str) and error.strip():
            normalized["hook_event_name"] = "PostToolUseFailure"
            normalized.setdefault("message", error.strip())
        else:
            normalized["hook_event_name"] = "PostToolUse"
    elif raw_event == "Stop":
        reason = str(payload.get("terminationReason") or "").strip()
        error = payload.get("error")
        error_text = error.strip() if isinstance(error, str) else ""
        if reason == "USER_CANCELED":
            normalized["hook_event_name"] = "Interrupt"
        elif error_text or reason in ANTIGRAVITY_ERROR_REASONS:
            normalized["hook_event_name"] = "StopFailure"
            normalized.setdefault(
                "message",
                error_text or f"Antigravity stopped: {reason}",
            )
        else:
            normalized["hook_event_name"] = "Stop"
    elif raw_event:
        normalized["hook_event_name"] = raw_event

    return normalized
