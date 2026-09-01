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
T3_CODE_ORIGINS = ("t3 code", "t3code")


def session_deep_link(status: AgentStatus) -> str | None:
    t3_url = t3code_thread_url(status)
    if t3_url:
        return t3_url

    provider = status.provider.lower()
    session_id = hosted_session_id(status)

    if provider == "codex" and session_id:
        return f"codex://threads/{quote(session_id, safe='')}"
    if provider == "claude":
        return "claude://"
    return None


def session_vscode_link(status: AgentStatus) -> str | None:
    session_id = hosted_session_id(status)
    if status.provider.lower() != "claude" or not session_id:
        return None
    return "vscode://anthropic.claude-code/open?" + urlencode(
        {"session": session_id},
        quote_via=quote,
    )


def session_resume_command(status: AgentStatus) -> str | None:
    provider = status.provider.lower()
    cwd = shlex.quote(status.cwd or str(Path.home()))
    native_id = hosted_session_id(status)
    session_id = shlex.quote(native_id) if native_id else None

    if provider == "codex" and session_id:
        return f"cd {cwd} && codex resume {session_id}"
    if provider == "claude" and session_id:
        return f"cd {cwd} && claude --resume {session_id}"
    if provider == "grok" and session_id:
        return f"cd {cwd} && grok --resume {session_id}"
    if provider == "opencode" and session_id:
        return f"cd {cwd} && opencode --session {session_id}"
    return None


def default_session_open_action(status: AgentStatus) -> str:
    for action in preferred_session_open_actions(status):
        if session_open_target(status, action):
            return action
    return SESSION_OPEN_TERMINAL


def hosted_session_id(status: AgentStatus) -> str | None:
    """Native provider session id when T3 is hosting, else the row's session id."""
    origin = normalized_origin(status.origin)
    if any(token in origin for token in T3_CODE_ORIGINS) and status.provider_session_id:
        return status.provider_session_id
    return status.session_id


def preferred_session_open_actions(status: AgentStatus) -> tuple[str, ...]:
    origin = normalized_origin(status.origin)
    if origin:
        if any(token in origin for token in T3_CODE_ORIGINS):
            return (SESSION_OPEN_APP, SESSION_OPEN_TERMINAL, SESSION_OPEN_VSCODE)
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


def t3code_local_environment_id(home: Path | None = None) -> str | None:
    """Read T3's local environment id from ``~/.t3/userdata/environment-id``."""
    root = home or Path.home()
    try:
        value = (root / ".t3" / "userdata" / "environment-id").read_text().strip()
    except OSError:
        return None
    return value or None


def t3code_thread_url(status: AgentStatus, *, home: Path | None = None) -> str | None:
    """T3 thread URL: ``t3code://threads/{environmentId}/{threadId}``.

    Matches T3 Code's mobile/widget contract and ``buildAgentAwarenessDeepLink``
    (``/threads/{environmentId}/{threadId}``). Desktop currently focuses the
    app; pingdotgg/t3code#6008 teaches it to navigate this same URL. Do not use
    ``t3code://app/threads/...``: that host is the renderer bundle, and the
    desktop deep-link parser explicitly rejects it.
    """

    origin = normalized_origin(status.origin)
    if not any(token in origin for token in T3_CODE_ORIGINS):
        return None
    thread_id = status.session_id
    if not thread_id:
        return None
    environment_id = t3code_local_environment_id(home)
    if not environment_id:
        return None
    return (
        "t3code://threads/"
        f"{quote(environment_id, safe='')}/{quote(thread_id, safe='')}"
    )


def session_open_target(status: AgentStatus, action: str) -> tuple[str, str] | None:
    if action == SESSION_OPEN_APP:
        t3_url = t3code_thread_url(status)
        if t3_url:
            return ("url", t3_url)
        if any(origin in normalized_origin(status.origin) for origin in T3_CODE_ORIGINS):
            return ("application", "t3code")
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
        if any(origin in normalized_origin(status.origin) for origin in T3_CODE_ORIGINS):
            return "Open T3 Code"
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
