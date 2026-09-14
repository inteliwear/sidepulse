from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .audit import append_status_audit_record
from .collector import StatusMetadata, read_recent_lines, status_from_event, title_from_event
from .ipc import send_hook_event
from .origin import annotate_payload_with_origin
from .providers import detect_log_path, infer_provider_from_payload, parse_log_line


def format_hook_payload(
    provider: str,
    payload_text: str,
    *,
    logged_at: str | None = None,
    include_origin: bool = True,
) -> dict[str, Any]:
    timestamp = logged_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        payload: Any = json.loads(payload_text or "{}")
    except json.JSONDecodeError as exc:
        payload = {
            "hook_event_name": "ParseError",
            "raw": payload_text,
            "parse_error": str(exc),
        }

    if include_origin and isinstance(payload, dict):
        payload = annotate_payload_with_origin(provider, payload)
    if provider == "codex":
        return {"logged_at": timestamp, "event": payload}
    if isinstance(payload, dict):
        line = dict(payload)
        line["logged_at"] = line.get("logged_at") or timestamp
        return line
    return {"logged_at": timestamp, "event": payload}


def write_hook_line(log_path: Path, line: dict[str, Any]) -> None:
    log_path = log_path.expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, separators=(",", ":"), ensure_ascii=False) + "\n")


def write_hook_payload(provider: str, log_path: Path, payload_text: str) -> None:
    line = format_hook_payload(provider, payload_text)
    write_hook_line(log_path, line)


def routed_hook_payload(
    provider: str,
    log_path: Path,
    payload_text: str,
) -> tuple[str, Path, dict[str, Any]]:
    line = format_hook_payload(provider, payload_text, include_origin=False)
    actual_provider = infer_provider_from_hook_line(provider, line)
    line = annotate_hook_line(actual_provider, line)
    actual_log_path = log_path
    if actual_provider != provider:
        actual_log_path = detect_log_path(actual_provider)
    return actual_provider, actual_log_path, line


def annotate_hook_line(provider: str, line: dict[str, Any]) -> dict[str, Any]:
    if isinstance(line.get("event"), dict):
        annotated = dict(line)
        annotated["event"] = annotate_payload_with_origin(provider, line["event"])
        return annotated
    return annotate_payload_with_origin(provider, line)


def infer_provider_from_hook_line(provider: str, line: dict[str, Any]) -> str:
    raw = line.get("event") if provider == "codex" else line
    if isinstance(raw, dict):
        return infer_provider_from_payload(provider, raw)
    return provider


def write_hook_status_audit(provider: str, line: dict[str, Any]) -> None:
    try:
        record = parse_log_line(
            provider,
            json.dumps(line, separators=(",", ":"), ensure_ascii=False),
        )
        if record is None:
            return
        metadata = StatusMetadata(cwd=record.cwd, title=title_from_event(record))
        append_status_audit_record(record, status_from_event(record, metadata))
    except Exception:
        pass


def hook_event_socket_disabled() -> bool:
    return os.environ.get("SIDEPULSE_DISABLE_EVENT_SOCKET", "").lower() in {
        "1",
        "true",
        "yes",
    }


def normalize_junie_payload(
    payload: dict[str, Any],
    log_path: Path,
    *,
    process_id: int | None = None,
) -> dict[str, Any]:
    """Restore session context omitted by some Junie hook payloads.

    Junie's Stop, StopFailure, and SessionEnd wire payloads do not include the
    session fields sent with SessionStart/UserPromptSubmit. The Junie process id
    lets separate CLI instances correlate independently; the most recent Junie
    context is a fallback for launchers where ancestry cannot be identified.
    """
    normalized = dict(payload)
    if process_id is None:
        active_process_id, origin = junie_process_context()
        if origin is not None and "agent_origin" not in normalized:
            normalized.update(origin)
    else:
        active_process_id = process_id
    if active_process_id is not None:
        normalized.setdefault("sidepulse_junie_process_id", active_process_id)

    if normalized.get("session_id") and normalized.get("cwd"):
        return normalized

    context = latest_junie_hook_context(log_path, active_process_id)
    if context is None:
        return normalized
    for key in ("session_id", "cwd", "project_path"):
        if context.get(key) is not None:
            normalized.setdefault(key, context[key])
    return normalized


def junie_process_id() -> int | None:
    return junie_process_context()[0]


def junie_process_context() -> tuple[int | None, dict[str, str] | None]:
    try:
        from .origin import origin_from_processes, process_ancestry, process_basename

        ancestry = process_ancestry(os.getppid())
        origin = origin_from_processes("junie", ancestry)
        for info in ancestry:
            command = info.command.lower()
            if process_basename(info) == "junie" or any(
                marker in command
                for marker in (
                    "/junie.app/",
                    "junie-release-",
                    "matterhorn.ej.app.cli.standalone",
                )
            ):
                return info.pid, origin.to_payload() if origin is not None else None
    except Exception:
        pass
    return None, None


def latest_junie_hook_context(
    log_path: Path,
    process_id: int | None,
) -> dict[str, Any] | None:
    try:
        lines = read_recent_lines(log_path.expanduser(), 200)
    except OSError:
        return None

    fallback = None
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(row, dict) or not row.get("session_id"):
            continue
        if fallback is None:
            fallback = row
        if process_id is not None and row.get("sidepulse_junie_process_id") == process_id:
            return row
    return fallback


def hook_log_main(provider: str, log_path: Path, event: str | None = None) -> int:
    response_text = None
    try:
        payload_text = sys.stdin.read()
        if provider in {"cursor", "antigravity"} and event:
            if provider == "cursor":
                from .cursor_hook import normalize_payload
            else:
                from .antigravity_hook import normalize_payload, response_for_event
                response_text = json.dumps(response_for_event(event), separators=(",", ":"))

            try:
                raw = json.loads(payload_text or "{}")
            except json.JSONDecodeError:
                raw = {}
            payload = raw if isinstance(raw, dict) else {}
            normalized = normalize_payload(event, payload)
            payload_text = json.dumps(normalized, separators=(",", ":"), ensure_ascii=False)
        elif provider == "junie":
            try:
                raw = json.loads(payload_text or "{}")
            except json.JSONDecodeError:
                raw = {}
            payload = raw if isinstance(raw, dict) else {}
            normalized = normalize_junie_payload(payload, log_path)
            payload_text = json.dumps(normalized, separators=(",", ":"), ensure_ascii=False)

        actual_provider, actual_log_path, line = routed_hook_payload(
            provider,
            log_path,
            payload_text,
        )
        try:
            write_hook_line(actual_log_path, line)
        except Exception:
            pass
        try:
            write_hook_status_audit(actual_provider, line)
        except Exception:
            pass
        try:
            if not hook_event_socket_disabled():
                send_hook_event(actual_provider, line)
                from .relay import candidate_relay_event_socket_paths

                for relay_socket in candidate_relay_event_socket_paths():
                    if send_hook_event(
                        actual_provider,
                        line,
                        socket_path=relay_socket,
                    ):
                        break
        except Exception:
            pass
    except Exception:
        if provider == "antigravity" and event and response_text is None:
            from .antigravity_hook import response_for_event

            response_text = json.dumps(response_for_event(event), separators=(",", ":"))
        if response_text is not None:
            sys.stdout.write(response_text + "\n")
        return 0
    if response_text is not None:
        sys.stdout.write(response_text + "\n")
    return 0
