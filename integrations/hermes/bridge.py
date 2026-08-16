"""Translate Hermes lifecycle hooks into SidePulse status events."""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SURFACE_RECOVERY_WINDOW_BYTES = 1024 * 1024
_SURFACE_TOKEN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_APPROVAL_EVENT_NAMES = {"PermissionRequest", "PermissionDenied"}


@dataclass(frozen=True)
class PluginSettings:
    """Privacy-safe identity and transport settings for SidePulse events."""

    agent_id: str = "Hermes"
    profile_name: str = ""
    provider: str = "hermes"
    state_dir: Path | None = None
    socket_path: Path | None = None
    socket_timeout: float = 0.2
    log_events: bool = True

    def resolved_state_dir(self) -> Path:
        if self.state_dir is not None:
            return Path(self.state_dir).expanduser()
        xdg_state_home = os.environ.get("XDG_STATE_HOME")
        base = (
            Path(xdg_state_home).expanduser()
            if xdg_state_home
            else Path.home() / ".local" / "state"
        )
        return base / "sidepulse" / "agent-monitor"

    def resolved_socket_path(self) -> Path:
        if self.socket_path is not None:
            return Path(self.socket_path).expanduser()
        return self.resolved_state_dir() / "events.sock"


@dataclass(frozen=True)
class DeliveryResult:
    event: dict[str, Any] | None
    logged: bool
    delivered: bool


def _status_agent_id(
    label: str,
    session_id: Any,
    profile_name: Any = "",
) -> str:
    durable_session_id = str(session_id or "").strip()
    if not durable_session_id:
        return label
    profile = str(profile_name or "").strip()
    identity = f"{profile}\0{durable_session_id}" if profile else durable_session_id
    suffix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    return f"{label}:{suffix}"


def _surface_metadata(platform: Any) -> tuple[str, str, str]:
    surface = str(platform or "unknown").strip().lower() or "unknown"
    if surface == "unknown":
        return surface, "Hermes", "hermes"
    if surface == "desktop":
        return surface, "Hermes Desktop", "hermes_desktop"
    if surface in {"cli", "terminal"}:
        return surface, "Hermes CLI", "hermes_cli"
    return surface, f"Hermes {surface.title()}", f"hermes_{surface.replace('-', '_')}"


def _recent_session_events(
    settings: PluginSettings,
    session_id: Any,
) -> tuple[Mapping[str, Any], ...]:
    """Return bounded, newest-first metadata events for one durable session."""

    durable_session_id = str(session_id or "").strip()
    expected_profile = str(settings.profile_name or "").strip()
    if not durable_session_id or not settings.log_events or not expected_profile:
        return ()
    expected_agent_id = _status_agent_id(
        settings.agent_id,
        durable_session_id,
        expected_profile,
    )
    try:
        target = settings.resolved_state_dir() / f"{settings.provider}.jsonl"
        with open(target, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            end = handle.tell()
            start = max(0, end - _SURFACE_RECOVERY_WINDOW_BYTES)
            handle.seek(start)
            data = handle.read(end - start)
        if start:
            _, _, data = data.partition(b"\n")
        events: list[Mapping[str, Any]] = []
        for raw_line in reversed(data.splitlines()):
            try:
                event = json.loads(raw_line)
            except (RecursionError, TypeError, ValueError):
                continue
            if not isinstance(event, Mapping):
                continue
            if event.get("session_id") != durable_session_id:
                continue
            if event.get("integration") != "hermes-plugin":
                continue
            if event.get("hermes_profile") != expected_profile:
                continue
            if event.get("agent_id") != expected_agent_id:
                continue
            events.append(event)
        return tuple(events)
    except (OSError, TypeError, ValueError):
        return ()


def recover_session_surface(
    settings: PluginSettings,
    session_id: Any,
) -> str:
    """Recover safe client provenance after Hermes reloads the plugin module."""

    for event in _recent_session_events(settings, session_id):
        try:
            if event.get("hook_event_name") in _APPROVAL_EVENT_NAMES:
                continue
            surface = str(event.get("surface") or "").strip().lower()
            if (
                surface == "unknown"
                or surface in {"gateway", "smart"}
                or surface.startswith("transport")
                or not _SURFACE_TOKEN.fullmatch(surface)
            ):
                continue
            return surface
        except (RecursionError, TypeError, ValueError):
            continue
    return ""


def recover_session_mode(settings: PluginSettings, session_id: Any) -> str:
    """Recover the most recent normalized status for a durable session."""

    allowed = {
        "idle_ready",
        "working",
        "tool_running",
        "waiting_for_input",
        "completed",
        "blocked_error",
    }
    for event in _recent_session_events(settings, session_id):
        mode = str(event.get("sidepulse_mode") or "").strip().lower()
        if mode in allowed:
            return mode
    return ""


def _final_mode(response: Any) -> str:
    text = response if isinstance(response, str) else ""
    markers = re.findall(
        r"<!--\s*sidepulse\s*:\s*([a-z0-9_ -]+)\s*-->",
        text,
        flags=re.IGNORECASE,
    )
    if markers:
        normalized = re.sub(r"[^a-z0-9]+", "_", markers[-1].lower()).strip("_")
        explicit = {
            "ask": "waiting_for_input",
            "question": "waiting_for_input",
            "waiting": "waiting_for_input",
            "waiting_for_input": "waiting_for_input",
            "blocked": "blocked_error",
            "error": "blocked_error",
            "blocked_error": "blocked_error",
            "working": "working",
            "tool_running": "tool_running",
            "done": "completed",
            "complete": "completed",
            "completed": "completed",
            "idle": "idle_ready",
            "ready": "idle_ready",
            "idle_ready": "idle_ready",
        }.get(normalized)
        if explicit:
            return explicit

    visible = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    visible = re.sub(r"`[^`\n]*`", " ", visible)
    visible = re.sub(r"<!--.*?-->", " ", visible, flags=re.DOTALL)
    visible = " ".join(visible.split()).strip()
    if not visible:
        return "completed"
    casual_questions = (
        "anything else?",
        "what else can i help with?",
        "need anything else?",
        "what do you want to work on?",
    )
    if any(visible.lower().endswith(question) for question in casual_questions):
        return "completed"
    return "waiting_for_input" if visible.endswith("?") else "completed"


def _event_mapping(
    hook_name: str, payload: Mapping[str, Any]
) -> tuple[str, str] | None:
    if hook_name == "on_session_start" or hook_name == "on_session_reset":
        return "SessionStart", "idle_ready"
    if hook_name == "on_session_activate":
        mode = str(payload.get("activation_mode") or "idle_ready").strip().lower()
        if mode not in {"idle_ready", "working", "waiting_for_input"}:
            mode = "idle_ready"
        return "SessionActivate", mode
    if hook_name == "pre_llm_call":
        return "UserPromptSubmit", "working"
    if hook_name == "pre_tool_call":
        if str(payload.get("tool_name") or "").strip() == "clarify":
            return "PermissionRequest", "waiting_for_input"
        return "PreToolUse", "tool_running"
    if hook_name == "post_tool_call":
        if str(payload.get("status") or "ok").strip().lower() in {"error", "blocked"}:
            return "PostToolUseFailure", "blocked_error"
        return "PostToolUse", "working"
    if hook_name == "pre_approval_request":
        return "PermissionRequest", "waiting_for_input"
    if hook_name == "post_approval_response":
        choice = str(payload.get("choice") or "").strip().lower()
        if choice not in {"once", "session", "always", "smart_approve"}:
            return "PermissionDenied", "blocked_error"
        return "UserPromptSubmit", "working"
    if hook_name == "api_request_error":
        return "StopFailure", "blocked_error"
    if hook_name == "post_llm_call":
        return "Stop", _final_mode(payload.get("assistant_response"))
    if hook_name == "on_session_end":
        if bool(payload.get("completed")) and not bool(payload.get("interrupted")):
            return None
        if bool(payload.get("interrupted")):
            return "SessionStart", "idle_ready"
        return "StopFailure", "blocked_error"
    return None


def translate_hook(
    hook_name: str,
    payload: Mapping[str, Any],
    settings: PluginSettings,
    *,
    now: str,
) -> dict[str, Any] | None:
    """Return a SidePulse event containing status metadata only."""

    mapping = _event_mapping(hook_name, payload)
    if mapping is None:
        return None
    if not str(settings.profile_name or "").strip():
        return None
    target_event, mode = mapping

    surface, origin, origin_kind = _surface_metadata(
        payload.get("platform") or payload.get("surface")
    )
    session_id = payload.get("session_id")
    event: dict[str, Any] = {
        "logged_at": now,
        "hook_event_name": target_event,
        "integration": "hermes-plugin",
        "agent_id": _status_agent_id(
            settings.agent_id,
            session_id,
            settings.profile_name,
        ),
        "agent_origin": origin,
        "agent_origin_kind": origin_kind,
        "sidepulse_mode": mode,
    }
    if isinstance(session_id, str) and session_id.strip():
        event["session_id"] = session_id.strip()
    profile_name = str(settings.profile_name or "").strip()
    if profile_name:
        event["hermes_profile"] = profile_name
    turn_id = payload.get("turn_id")
    if isinstance(turn_id, str) and turn_id.strip():
        event["turn_id"] = turn_id.strip()
    tool_name = payload.get("tool_name")
    if isinstance(tool_name, str) and tool_name.strip():
        event["tool_name"] = tool_name.strip()
    event["surface"] = surface
    return event


def _append_event(event: Mapping[str, Any], settings: PluginSettings) -> bool:
    if not settings.log_events:
        return False
    try:
        target = settings.resolved_state_dir() / f"{settings.provider}.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, separators=(",", ":"), ensure_ascii=False) + "\n"
        descriptor = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
                descriptor = -1
                handle.write(line)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _send_event(event: Mapping[str, Any], settings: PluginSettings) -> bool:
    try:
        message = json.dumps(
            {"provider": settings.provider, "line": event},
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        if len(message) > 1024 * 1024:
            return False
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(settings.socket_timeout)
        try:
            client.connect(str(settings.resolved_socket_path()))
            client.sendall(message)
            return True
        finally:
            client.close()
    except (OSError, TypeError, ValueError):
        return False


def emit_hook(
    hook_name: str,
    payload: Mapping[str, Any],
    settings: PluginSettings,
    *,
    now: str | None = None,
) -> DeliveryResult:
    """Translate, log, and best-effort deliver one Hermes hook event."""

    timestamp = now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    event = translate_hook(hook_name, payload, settings, now=timestamp)
    if event is None:
        return DeliveryResult(event=None, logged=False, delivered=False)
    logged = _append_event(event, settings)
    delivered = _send_event(event, settings)
    return DeliveryResult(event=event, logged=logged, delivered=delivered)
