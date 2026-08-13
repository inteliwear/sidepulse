from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import HookEvent, parse_datetime
from .origin import origin_label_from_payload

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 compatibility.
    tomllib = None

CODEX_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PermissionRequest",
    "PreCompact",
    "PostCompact",
    "SubagentStart",
    "SubagentStop",
    "Stop",
)

CLAUDE_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "PermissionRequest",
    "Notification",
    "PreCompact",
    "PostCompact",
    "SubagentStop",
    "Stop",
    "SessionEnd",
)

GROK_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "PermissionDenied",
    "Notification",
    "PreCompact",
    "PostCompact",
    "SubagentStart",
    "SubagentStop",
    "Stop",
    "StopFailure",
    "SessionEnd",
)

# Cursor's hook events that SidePulse subscribes to. Unlike the Codex / Claude
# / Grok hooks, the Cursor integration is status-only: the registered commands
# publish an explicit `sidepulse_status` transition and never write message
# content to a local log.
CURSOR_EVENTS = (
    "beforeSubmitPrompt",
    "stop",
)

HOOK_PROVIDERS = ("codex", "claude", "grok", "cursor")
KNOWN_EVENTS = tuple(dict.fromkeys(CODEX_EVENTS + CLAUDE_EVENTS + GROK_EVENTS))


@dataclass(frozen=True)
class ProviderConfig:
    provider: str
    config_path: Path
    exists: bool
    hooks_enabled: bool
    hook_events: tuple[str, ...]
    log_paths: tuple[Path, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "config_path": str(self.config_path),
            "exists": self.exists,
            "hooks_enabled": self.hooks_enabled,
            "hook_events": list(self.hook_events),
            "log_paths": [str(path) for path in self.log_paths],
        }


def default_state_dir(home: Path | None = None) -> Path:
    if home is None:
        xdg_state_home = os.environ.get("XDG_STATE_HOME")
        if xdg_state_home:
            return Path(xdg_state_home).expanduser() / "sidepulse" / "agent-monitor"

    base = home or Path.home()
    return base / ".local" / "state" / "sidepulse" / "agent-monitor"


def default_log_path(provider: str, home: Path | None = None) -> Path:
    suffix = "jsonl"
    return default_state_dir(home) / f"{provider}.{suffix}"


def detect_provider_configs(home: Path | None = None) -> list[ProviderConfig]:
    return [
        detect_codex_config(home),
        detect_claude_config(home),
        detect_grok_config(home),
        detect_cursor_config(home),
    ]


def default_cursor_hook_config_path(home: Path | None = None) -> Path:
    base = home or Path.home()
    return base / ".cursor" / "hooks.json"


def detect_cursor_config(home: Path | None = None) -> ProviderConfig:
    config_path = default_cursor_hook_config_path(home)
    if not config_path.exists():
        return ProviderConfig("cursor", config_path, False, False, (), ())

    try:
        data = json.loads(config_path.read_text())
    except Exception:
        return ProviderConfig("cursor", config_path, True, False, (), ())

    hooks = data.get("hooks") if isinstance(data, dict) else None
    events: list[str] = []
    if isinstance(hooks, dict):
        for event_name, entries in hooks.items():
            if event_name not in CURSOR_EVENTS or not isinstance(entries, list):
                continue
            for entry in entries:
                if isinstance(entry, dict) and "publish-status" in str(entry.get("command", "")):
                    events.append(event_name)
                    break

    return ProviderConfig(
        "cursor",
        config_path,
        True,
        bool(events),
        tuple(sorted(set(events))),
        (),
    )


def detect_codex_config(home: Path | None = None) -> ProviderConfig:
    base = home or Path.home()
    config_path = base / ".codex" / "config.toml"
    if not config_path.exists():
        return ProviderConfig("codex", config_path, False, False, (), ())

    text = config_path.read_text()
    try:
        if tomllib is None:
            raise RuntimeError("tomllib unavailable")
        data = tomllib.loads(text)
    except Exception:
        return detect_codex_config_from_text(config_path, text)

    features = data.get("features") or {}
    hooks = data.get("hooks") or {}
    hook_events: list[str] = []
    paths: list[Path] = []

    if isinstance(hooks, dict):
        for event_name, entries in hooks.items():
            if event_name not in CODEX_EVENTS or not isinstance(entries, list):
                continue
            hook_events.append(event_name)
            paths.extend(_paths_from_hook_entries(entries))

    return ProviderConfig(
        "codex",
        config_path,
        True,
        bool(features.get("hooks")),
        tuple(sorted(set(hook_events))),
        _dedupe_paths(paths),
    )


def detect_codex_config_from_text(config_path: Path, text: str) -> ProviderConfig:
    hook_events = tuple(
        sorted(
            {
                match.group(1)
                for match in re.finditer(r"^\s*\[\[hooks\.([A-Za-z0-9_]+)\]\]\s*$", text, re.MULTILINE)
                if match.group(1) in CODEX_EVENTS
            }
        )
    )

    paths: list[Path] = []
    for match in re.finditer(r"command\s*=\s*'''(.*?)'''", text, re.DOTALL):
        paths.extend(extract_log_paths_from_command(match.group(1)))
    for match in re.finditer(r'command\s*=\s*"(.*?)"', text):
        paths.extend(extract_log_paths_from_command(match.group(1)))

    return ProviderConfig(
        "codex",
        config_path,
        True,
        codex_hooks_feature_enabled(text),
        hook_events,
        _dedupe_paths(paths),
    )


def codex_hooks_feature_enabled(text: str) -> bool:
    match = re.search(r"^\s*\[features\]\s*$(.*?)(?=^\s*\[|\Z)", text, re.MULTILINE | re.DOTALL)
    if not match:
        return False
    return bool(re.search(r"^\s*hooks\s*=\s*true\s*$", match.group(1), re.MULTILINE))


def detect_claude_config(home: Path | None = None) -> ProviderConfig:
    base = home or Path.home()
    config_path = base / ".claude" / "settings.json"
    if not config_path.exists():
        return ProviderConfig("claude", config_path, False, False, (), ())

    try:
        data = json.loads(config_path.read_text())
    except Exception:
        return ProviderConfig("claude", config_path, True, False, (), ())

    hooks = data.get("hooks") or {}
    hook_events: list[str] = []
    paths: list[Path] = []

    if isinstance(hooks, dict):
        for event_name, entries in hooks.items():
            if event_name not in CLAUDE_EVENTS or not isinstance(entries, list):
                continue
            hook_events.append(event_name)
            paths.extend(_paths_from_hook_entries(entries))

    return ProviderConfig(
        "claude",
        config_path,
        True,
        bool(hook_events),
        tuple(sorted(set(hook_events))),
        _dedupe_paths(paths),
    )


def default_grok_hook_config_path(home: Path | None = None) -> Path:
    base = home or Path.home()
    return base / ".grok" / "hooks" / "sidepulse.json"


def detect_grok_config(home: Path | None = None) -> ProviderConfig:
    config_path = default_grok_hook_config_path(home)
    if not config_path.exists():
        return ProviderConfig("grok", config_path, False, False, (), ())

    try:
        data = json.loads(config_path.read_text())
    except Exception:
        return ProviderConfig("grok", config_path, True, False, (), ())

    hooks = data.get("hooks") or {}
    hook_events: list[str] = []
    paths: list[Path] = []

    if isinstance(hooks, dict):
        for event_name, entries in hooks.items():
            canonical = canonical_event_name(event_name)
            if canonical not in GROK_EVENTS or not isinstance(entries, list):
                continue
            hook_events.append(canonical)
            paths.extend(_paths_from_hook_entries(entries))

    return ProviderConfig(
        "grok",
        config_path,
        True,
        bool(hook_events),
        tuple(sorted(set(hook_events))),
        _dedupe_paths(paths),
    )


def detect_log_path(provider: str, home: Path | None = None) -> Path:
    if provider == "codex":
        config = detect_codex_config(home)
    elif provider == "claude":
        config = detect_claude_config(home)
    elif provider == "grok":
        config = detect_grok_config(home)
    else:
        config = ProviderConfig(provider, default_log_path(provider, home), False, False, (), ())
    if config.log_paths:
        return config.log_paths[0]
    return default_log_path(provider, home)


def parse_log_line(provider: str, line: str) -> HookEvent | None:
    line = line.strip()
    if not line:
        return None

    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None

    if not isinstance(obj, dict):
        return None

    if provider == "codex" and isinstance(obj.get("event"), dict):
        raw = obj["event"]
        logged_at = obj.get("logged_at") or raw.get("logged_at")
    else:
        raw = obj
        logged_at = raw.get("logged_at") or raw.get("timestamp")
    provider = infer_provider_from_payload(provider, raw)

    event_name = canonical_event_name(
        raw.get("hook_event_name")
        or raw.get("hookEventName")
        or raw.get("event_name")
        or raw.get("eventName")
    )
    if not event_name:
        return None

    normalized_raw = normalize_event_payload(raw, event_name, logged_at)

    return HookEvent(
        provider=provider,
        logged_at=parse_datetime(logged_at),
        event_name=event_name,
        raw=normalized_raw,
        session_id=_first_string(normalized_raw, "session_id", "sessionId"),
        turn_id=_first_string(normalized_raw, "turn_id", "turnId"),
        agent_id=_first_string(normalized_raw, "agent_id", "agentId"),
        cwd=_first_string(normalized_raw, "cwd", "workspaceRoot"),
        tool_name=_first_string(normalized_raw, "tool_name", "toolName"),
        message=_first_string(normalized_raw, "message", "last_assistant_message", "lastAssistantMessage"),
        origin=origin_label_from_payload(provider, normalized_raw),
    )


def infer_provider_from_payload(provider: str, raw: dict[str, Any]) -> str:
    if provider == "claude" and grok_payload_looks_compatible(raw):
        return "grok"
    return provider


def grok_payload_looks_compatible(raw: dict[str, Any]) -> bool:
    transcript_path = str(raw.get("transcriptPath") or raw.get("transcript_path") or "")
    if "/.grok/" in transcript_path or "\\.grok\\" in transcript_path:
        return True

    camel_grok_keys = {"hookEventName", "sessionId", "workspaceRoot"}
    return "hookEventName" in raw and bool(camel_grok_keys.intersection(raw))


def canonical_event_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text in KNOWN_EVENTS:
        return text

    normalized = re.sub(r"[^a-zA-Z0-9]+", "_", text)
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", normalized)
    normalized = normalized.strip("_").lower()
    aliases = {_event_alias_key(event): event for event in KNOWN_EVENTS}
    aliases.update(
        {
            "pre_tool_use": "PreToolUse",
            "post_tool_use": "PostToolUse",
            "post_tool_use_failure": "PostToolUseFailure",
            "permission_request": "PermissionRequest",
            "permission_denied": "PermissionDenied",
            "user_prompt_submit": "UserPromptSubmit",
            "session_start": "SessionStart",
            "session_end": "SessionEnd",
            "subagent_start": "SubagentStart",
            "subagent_stop": "SubagentStop",
            "subagent_end": "SubagentStop",
            "pre_compact": "PreCompact",
            "post_compact": "PostCompact",
            "stop_failure": "StopFailure",
        }
    )
    return aliases.get(normalized)


def normalize_event_payload(raw: dict[str, Any], event_name: str, logged_at: Any) -> dict[str, Any]:
    normalized = dict(raw)
    normalized.setdefault("hook_event_name", event_name)
    if logged_at is not None:
        normalized.setdefault("logged_at", logged_at)

    _copy_alias(normalized, "sessionId", "session_id")
    _copy_alias(normalized, "turnId", "turn_id")
    _copy_alias(normalized, "agentId", "agent_id")
    _copy_alias(normalized, "workspaceRoot", "cwd")
    _copy_alias(normalized, "toolName", "tool_name")
    _copy_alias(normalized, "toolInput", "tool_input")
    _copy_alias(normalized, "toolResponse", "tool_response")
    _copy_alias(normalized, "lastAssistantMessage", "last_assistant_message")
    _copy_alias(normalized, "notificationType", "notification_type")
    _copy_alias(normalized, "agentOrigin", "agent_origin")
    _copy_alias(normalized, "agentOriginKind", "agent_origin_kind")
    _copy_alias(normalized, "sidepulseOrigin", "sidepulse_origin")
    return normalized


def _event_alias_key(event_name: str) -> str:
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", event_name).lower()


def _copy_alias(data: dict[str, Any], source: str, target: str) -> None:
    if target not in data and source in data:
        data[target] = data[source]


def _paths_from_hook_entries(entries: list[Any]) -> list[Path]:
    paths: list[Path] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for hook in entry.get("hooks") or []:
            if not isinstance(hook, dict):
                continue
            command = hook.get("command")
            if isinstance(command, str):
                paths.extend(extract_log_paths_from_command(command))
    return paths


def extract_log_paths_from_command(command: str) -> list[Path]:
    paths: list[Path] = []

    for match in re.finditer(r">>\s+(['\"]?)([^'\"\s]+)\1", command):
        paths.append(Path(match.group(2)).expanduser())

    try:
        parts = shlex.split(command)
    except ValueError:
        parts = []

    for index, part in enumerate(parts):
        if part == "--log" and index + 1 < len(parts):
            paths.append(Path(parts[index + 1]).expanduser())
        elif part.startswith("--log="):
            paths.append(Path(part.split("=", 1)[1]).expanduser())

    return _dedupe_paths(paths)


def _dedupe_paths(paths: list[Path]) -> tuple[Path, ...]:
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return tuple(result)


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _first_string(data: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = _string_or_none(data.get(key))
        if value:
            return value
    return None
