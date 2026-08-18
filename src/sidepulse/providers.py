from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import sqlite3
import time
from contextlib import closing
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

HOOK_PROVIDERS = ("codex", "claude", "grok")
HERMES_EVENTS = ("SessionActivate", "SessionFinalize")
KNOWN_EVENTS = tuple(
    dict.fromkeys(CODEX_EVENTS + CLAUDE_EVENTS + GROK_EVENTS + HERMES_EVENTS)
)

_HERMES_SESSION_CACHE_TTL_SECONDS = 2.0
_HERMES_SESSION_CACHE: dict[
    tuple[str, str], tuple[float, tuple[str, str | None] | None]
] = {}
_HERMES_PROFILE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


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
    return [detect_codex_config(home), detect_claude_config(home), detect_grok_config(home)]


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
    if provider == "hermes":
        enrich_hermes_session(normalized_raw)

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
        hermes_profile=_first_string(normalized_raw, "hermes_profile"),
    )


def enrich_hermes_session(raw: dict[str, Any]) -> None:
    session_id = _first_string(raw, "session_id", "sessionId")
    if not session_id:
        return

    profile_name = _first_string(raw, "hermes_profile")
    if not profile_name and not os.environ.get("SIDEPULSE_HERMES_STATE_DB"):
        # An unscoped event cannot be attributed safely when multiple Hermes
        # profiles share the same durable session identifiers. An explicitly
        # configured database is the caller's deliberate scope.
        return
    metadata = hermes_session_metadata(session_id, profile_name=profile_name)
    if metadata is None:
        return

    root_session_id, title = metadata
    if not profile_name:
        raw["_hermes_session_metadata_scoped"] = True
    current_agent_id = _first_string(raw, "agent_id", "agentId") or "hermes"
    agent_prefix = current_agent_id.rsplit(":", 1)[0]
    root_digest = hashlib.sha256(root_session_id.encode("utf-8")).hexdigest()[:12]
    raw["agent_id"] = f"{agent_prefix}:{root_digest}"
    if title:
        raw["session_title"] = title


def hermes_session_metadata(
    session_id: str,
    *,
    profile_name: str | None = None,
) -> tuple[str, str | None] | None:
    now = time.monotonic()
    matches: list[tuple[str, str | None]] = []
    for state_db in hermes_state_db_paths(profile_name=profile_name):
        cache_key = (str(state_db), session_id)
        cached = _HERMES_SESSION_CACHE.get(cache_key)
        if cached and now - cached[0] < _HERMES_SESSION_CACHE_TTL_SECONDS:
            if cached[1] is not None:
                matches.append(cached[1])
            continue

        metadata = hermes_session_metadata_from_db(state_db, session_id)
        _HERMES_SESSION_CACHE[cache_key] = (now, metadata)
        if metadata is not None:
            matches.append(metadata)

    # A profile selector narrows discovery to one database. Profile-less
    # lookups are only reachable here for an explicitly configured database;
    # automatic multi-profile discovery is rejected by enrich_hermes_session.
    if profile_name:
        return matches[0] if matches else None
    return matches[0] if len(matches) == 1 else None


def hermes_state_db_paths(*, profile_name: str | None = None) -> tuple[Path, ...]:
    configured = os.environ.get("SIDEPULSE_HERMES_STATE_DB")
    configured_db = Path(configured).expanduser() if configured else None

    configured_home = os.environ.get("HERMES_HOME")
    active_home = (
        Path(configured_home).expanduser()
        if configured_home
        else Path.home() / ".hermes"
    )
    root_home = (
        active_home.parent.parent
        if active_home.parent.name == "profiles"
        else active_home
    )

    if profile_name:
        normalized_profile = profile_name.strip()
        normalized_casefold = normalized_profile.casefold()
        if normalized_casefold == "custom":
            selected_db = active_home / "state.db"
        elif normalized_casefold == "default":
            selected_db = root_home / "state.db"
        elif not _HERMES_PROFILE_TOKEN.fullmatch(normalized_profile):
            return ()
        elif (
            active_home.parent.name == "profiles"
            and active_home.name == normalized_profile
        ):
            selected_db = active_home / "state.db"
        else:
            selected_db = root_home / "profiles" / normalized_profile / "state.db"

        if configured_db is not None:
            if configured_db.resolve(strict=False) != selected_db.resolve(strict=False):
                return ()
            return (configured_db,)
        return (selected_db,)

    if configured_db is not None:
        return (configured_db,)

    candidates: list[Path] = [active_home / "state.db"]
    candidates.append(root_home / "state.db")
    candidates.extend(sorted((root_home / "profiles").glob("*/state.db")))
    return _dedupe_paths(candidates)


def _hermes_session_is_explicit_fork(session: dict[str, Any]) -> bool:
    if session.get("source") == "tool":
        return True
    raw_config = session.get("model_config")
    if not raw_config:
        return False
    try:
        config = json.loads(raw_config) if isinstance(raw_config, str) else raw_config
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(config, dict):
        return False
    parent_id = session.get("parent_session_id")
    return bool(
        parent_id
        and (
            config.get("_branched_from") == parent_id
            or config.get("_delegate_from") == parent_id
            or config.get("_reset_from") == parent_id
        )
    )


def hermes_session_metadata_from_db(
    state_db: Path,
    session_id: str,
) -> tuple[str, str | None] | None:
    if not state_db.is_file():
        return None

    try:
        database_uri = f"file:{state_db.resolve()}?mode=ro"
        with closing(sqlite3.connect(database_uri, uri=True, timeout=0.25)) as connection:
            connection.row_factory = sqlite3.Row
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(sessions)").fetchall()
            }
            required = {"id", "parent_session_id", "title", "started_at"}
            if not required.issubset(columns):
                return None

            selected_columns = [
                "id",
                "parent_session_id",
                "title",
                "started_at",
            ]
            selected_columns.extend(
                column
                for column in ("end_reason", "source", "model_config")
                if column in columns
            )
            projection = ", ".join(selected_columns)

            def load_session(candidate_id: str) -> dict[str, Any] | None:
                row = connection.execute(
                    f"SELECT {projection} FROM sessions WHERE id = ?",
                    (candidate_id,),
                ).fetchone()
                return dict(row) if row is not None else None

            requested = load_session(session_id)
            if requested is None:
                return None

            # Compression cannot be proven against legacy/incomplete schemas.
            # Keep the requested session independent rather than merging it with
            # branches, delegates, or tool children based on parent_id alone.
            if "end_reason" not in columns:
                title = str(requested.get("title") or "").strip() or None
                return str(requested["id"]), title

            root = requested
            anchored_child_by_parent: dict[str, dict[str, Any]] = {}
            seen = {str(root["id"])}
            for _ in range(128):
                parent_id = str(root.get("parent_session_id") or "")
                if (
                    not parent_id
                    or parent_id in seen
                    or _hermes_session_is_explicit_fork(root)
                ):
                    break
                parent = load_session(parent_id)
                if parent is None or parent.get("end_reason") != "compression":
                    break
                anchored_child_by_parent[parent_id] = root
                root = parent
                seen.add(parent_id)

            lineage = [root]
            current = root
            seen = {str(root["id"])}
            for _ in range(128):
                if current.get("end_reason") != "compression":
                    break
                current_id = str(current["id"])
                next_child = anchored_child_by_parent.get(current_id)
                if next_child is None:
                    rows = connection.execute(
                        f"SELECT {projection} FROM sessions "
                        "WHERE parent_session_id = ? "
                        "ORDER BY started_at DESC, id DESC",
                        (current_id,),
                    ).fetchall()
                    for row in rows:
                        candidate = dict(row)
                        if not _hermes_session_is_explicit_fork(candidate):
                            next_child = candidate
                            break
                if next_child is None or str(next_child["id"]) in seen:
                    break
                lineage.append(next_child)
                current = next_child
                seen.add(str(current["id"]))

            titled = [
                row
                for row in lineage
                if isinstance(row.get("title"), str) and row["title"].strip()
            ]
            latest_title = max(
                titled,
                key=lambda row: (str(row.get("started_at") or ""), str(row["id"])),
                default=None,
            )
    except (OSError, sqlite3.Error):
        return None

    title = str(latest_title["title"]).strip() if latest_title is not None else None
    return str(root["id"]), title or None


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
