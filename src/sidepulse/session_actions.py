from __future__ import annotations

import shlex
from pathlib import Path
from urllib.parse import quote, urlencode

from .models import AgentStatus

SESSION_OPEN_APP = "app"
SESSION_OPEN_TERMINAL = "terminal"
SESSION_OPEN_VSCODE = "vscode"
SESSION_OPEN_CHOICES = (SESSION_OPEN_APP, SESSION_OPEN_TERMINAL, SESSION_OPEN_VSCODE)
SESSION_OPEN_APP_SURFACES = ("app", "ui", "transcript")
SESSION_OPEN_TERMINAL_SURFACES = ("cli", "terminal", "command line")
SESSION_OPEN_VSCODE_SURFACES = ("vscode", "vs code", "visual studio code")


def session_deep_link(status: AgentStatus) -> str | None:
    provider = status.provider.lower()
    session_id = status.session_id

    if provider == "codex" and session_id:
        return f"codex://threads/{quote(session_id, safe='')}"
    if provider == "claude":
        return "claude://"
    return None


def session_vscode_link(status: AgentStatus) -> str | None:
    if status.provider.lower() != "claude" or not status.session_id:
        return None
    return "vscode://anthropic.claude-code/open?" + urlencode(
        {"session": status.session_id},
        quote_via=quote,
    )


def session_resume_command(status: AgentStatus) -> str | None:
    if not status.session_id:
        return None

    provider = status.provider.lower()
    cwd = shlex.quote(status.cwd or str(Path.home()))
    session_id = shlex.quote(status.session_id)

    if provider == "codex":
        return f"cd {cwd} && codex resume {session_id}"
    if provider == "claude":
        return f"cd {cwd} && claude --resume {session_id}"
    if provider == "grok":
        return f"cd {cwd} && grok --resume {session_id}"
    if provider == "junie":
        return f"cd {cwd} && junie --session-id={session_id} --resume"
    return None


def default_session_open_action(status: AgentStatus) -> str:
    for action in preferred_session_open_actions(status):
        if session_open_target(status, action):
            return action
    return SESSION_OPEN_TERMINAL


def preferred_session_open_actions(status: AgentStatus) -> tuple[str, ...]:
    origin = normalized_origin(status.origin)
    if origin:
        if any(surface in origin for surface in SESSION_OPEN_VSCODE_SURFACES):
            return (SESSION_OPEN_VSCODE, SESSION_OPEN_APP, SESSION_OPEN_TERMINAL)
        if any(surface in origin for surface in SESSION_OPEN_TERMINAL_SURFACES):
            return (SESSION_OPEN_TERMINAL, SESSION_OPEN_APP, SESSION_OPEN_VSCODE)
        if any(surface in origin for surface in SESSION_OPEN_APP_SURFACES):
            return (SESSION_OPEN_APP, SESSION_OPEN_VSCODE, SESSION_OPEN_TERMINAL)
        if "cursor" in origin or "windsurf" in origin:
            return (SESSION_OPEN_APP, SESSION_OPEN_TERMINAL, SESSION_OPEN_VSCODE)

    if status.provider.lower() == "claude":
        return (SESSION_OPEN_VSCODE, SESSION_OPEN_APP, SESSION_OPEN_TERMINAL)
    return (SESSION_OPEN_APP, SESSION_OPEN_TERMINAL, SESSION_OPEN_VSCODE)


def normalized_origin(origin: str | None) -> str:
    return " ".join(str(origin or "").strip().lower().replace("-", " ").split())


def session_open_target(status: AgentStatus, action: str) -> tuple[str, str] | None:
    if action == SESSION_OPEN_APP:
        url = session_deep_link(status)
        return ("url", url) if url else None
    if action == SESSION_OPEN_VSCODE:
        url = session_vscode_link(status)
        return ("url", url) if url else None
    if action == SESSION_OPEN_TERMINAL:
        command = session_resume_command(status)
        return ("terminal", command) if command else None
    return None


def available_session_open_actions(status: AgentStatus) -> tuple[str, ...]:
    return tuple(action for action in SESSION_OPEN_CHOICES if session_open_target(status, action))


def session_open_action_label(status: AgentStatus, action: str) -> str:
    provider = status.provider.lower()
    if action == SESSION_OPEN_APP:
        if provider == "codex":
            return "Open in Codex"
        if provider == "claude":
            return "Open Claude App"
        return "Open App"
    if action == SESSION_OPEN_VSCODE:
        return "Open in VS Code"
    if action == SESSION_OPEN_TERMINAL:
        return "Resume in Terminal"
    return action
