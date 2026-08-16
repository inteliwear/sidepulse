"""Hermes lifecycle integration for SidePulse."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .bridge import (
    PluginSettings,
    emit_hook,
    recover_session_mode,
    recover_session_surface,
)
from .diagnostics import handle_cli, setup_cli

HOOK_NAMES = (
    "on_session_start",
    "on_session_activate",
    "on_session_reset",
    "pre_llm_call",
    "pre_tool_call",
    "post_tool_call",
    "pre_approval_request",
    "post_approval_response",
    "api_request_error",
    "post_llm_call",
    "on_session_end",
    "on_session_finalize",
)


def _optional_path(value: Any) -> Path | None:
    text = str(value or "").strip()
    return Path(text).expanduser() if text else None


def _boolean_setting(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def _settings_from_context(ctx) -> PluginSettings:
    profile_name = str(getattr(ctx, "profile_name", "") or "").strip()
    agent_id = str(ctx.get_config("agent_id", default="") or "").strip() or profile_name
    timeout_value = ctx.get_config("socket_timeout", default=0.2)
    try:
        socket_timeout = max(0.01, min(float(timeout_value), 5.0))
    except (TypeError, ValueError):
        socket_timeout = 0.2
    return PluginSettings(
        agent_id=agent_id,
        profile_name=profile_name,
        state_dir=_optional_path(ctx.get_config("state_dir", default="")),
        socket_path=_optional_path(ctx.get_config("socket_path", default="")),
        socket_timeout=socket_timeout,
        log_events=_boolean_setting(
            ctx.get_config("log_events", default=True),
            default=True,
        ),
    )


def _observer(
    hook_name: str,
    settings: PluginSettings,
    surfaces_by_session: dict[str, str],
    sessions_by_turn: dict[str, str],
):
    def callback(**kwargs: Any) -> None:
        session_id = str(kwargs.get("session_id") or "").strip()
        turn_id = str(kwargs.get("turn_id") or "").strip()
        if session_id and turn_id:
            sessions_by_turn[turn_id] = session_id
        elif turn_id:
            session_id = sessions_by_turn.get(turn_id, "")

        # SidePulse identities are session-scoped. A shared observer can see
        # tool hooks without durable session context; emitting those would
        # create an independent active agent that no completion can clear.
        if not session_id:
            return
        if not str(settings.profile_name or "").strip():
            return
        # Hermes background memory/skill reviews reuse the foreground session id
        # and platform. They are hidden maintenance work, not user activity, so
        # never let them create or refresh SidePulse status.
        if bool(kwargs.get("background_review")):
            return

        platform = str(kwargs.get("platform") or "").strip()
        hook_surface = str(kwargs.get("surface") or "").strip()
        is_approval_hook = hook_name in {
            "pre_approval_request",
            "post_approval_response",
        }
        client_surface = platform or ("" if is_approval_hook else hook_surface)
        if session_id and client_surface:
            surfaces_by_session[session_id] = client_surface

        updates: dict[str, str] = {}
        if hook_name == "on_session_activate":
            if bool(kwargs.get("running")):
                previous_mode = recover_session_mode(settings, session_id)
                updates["activation_mode"] = (
                    "waiting_for_input"
                    if previous_mode == "waiting_for_input"
                    else "working"
                )
            else:
                updates["activation_mode"] = "idle_ready"
        if is_approval_hook and "surface" in kwargs:
            kwargs = {key: value for key, value in kwargs.items() if key != "surface"}
        if session_id and not kwargs.get("session_id"):
            updates["session_id"] = session_id
        cached_surface = surfaces_by_session.get(session_id, "")
        if session_id and not cached_surface and not client_surface:
            cached_surface = recover_session_surface(settings, session_id)
            if cached_surface:
                surfaces_by_session[session_id] = cached_surface
        if cached_surface and (is_approval_hook or not client_surface):
            updates["platform"] = cached_surface
        elif is_approval_hook and not platform:
            updates["platform"] = "unknown"
        if updates:
            kwargs = {**kwargs, **updates}
        try:
            emit_hook(hook_name, kwargs, settings)
        except Exception:  # noqa: BLE001, S110 - observer must never break Hermes
            pass
        finally:
            if hook_name in {"on_session_end", "on_session_finalize"} and session_id:
                surfaces_by_session.pop(session_id, None)
                for known_turn, known_session in list(sessions_by_turn.items()):
                    if known_session == session_id:
                        sessions_by_turn.pop(known_turn, None)

    return callback


def register(ctx) -> None:
    """Register SidePulse as an observer-only Hermes lifecycle plugin."""

    settings = _settings_from_context(ctx)
    surfaces_by_session: dict[str, str] = {}
    sessions_by_turn: dict[str, str] = {}
    for hook_name in HOOK_NAMES:
        ctx.register_hook(
            hook_name,
            _observer(
                hook_name,
                settings,
                surfaces_by_session,
                sessions_by_turn,
            ),
        )
    ctx.register_cli_command(
        name="sidepulse",
        help="Inspect the Hermes SidePulse integration",
        setup_fn=setup_cli,
        handler_fn=lambda args: handle_cli(args, settings),
    )
