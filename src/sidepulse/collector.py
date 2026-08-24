from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any, Callable, Iterable

from .models import (
    MODE_PRIORITY,
    AgentMode,
    AgentStatus,
    AggregateStatus,
    HookEvent,
    parse_datetime,
    provider_label,
)
from .origin import origin_label_from_payload
from .providers import detect_log_path, parse_log_line
from .settings import AgentMonitorSettings, load_settings


CODEX_TRANSCRIPT_PROVIDER = "codex-transcripts"
CLAUDE_TRANSCRIPT_PROVIDER = "claude-transcripts"
CODEX_TRANSCRIPT_MAX_FILES = 12
CODEX_TRANSCRIPT_MAX_LINES = 500
CLAUDE_TRANSCRIPT_MAX_FILES = 24
CLAUDE_TRANSCRIPT_MAX_LINES = 500
TRANSCRIPT_FILE_LIST_CACHE_SECONDS = 5.0
CLAUDE_TRANSCRIPT_MTIME_HEARTBEAT_SKEW_SECONDS = 30.0
CODEX_SESSION_INDEX_MAX_LINES = 5000
COMPLETED_VISIBLE_SECONDS = 20 * 60.0
IDLE_VISIBLE_SECONDS = 0.0
POST_TOOL_WORKING_VISIBLE_SECONDS = 2 * 60.0


@dataclass(frozen=True)
class SourceSpec:
    provider: str
    path: Path


@dataclass
class StatusMetadata:
    cwd: str | None = None
    title: str | None = None
    origin: str | None = None


@dataclass(frozen=True)
class CachedTranscriptRecords:
    mtime: float
    size: int
    records: tuple[HookEvent, ...]


@dataclass(frozen=True)
class CachedTranscriptFileList:
    expires_at: float
    paths: tuple[Path, ...]


@dataclass(frozen=True)
class CachedCodexSessionIndex:
    path: Path
    mtime: float
    size: int
    titles: dict[str, str]


_codex_session_index_cache: CachedCodexSessionIndex | None = None
_codex_session_index_lock = threading.RLock()


@dataclass(frozen=True)
class MonitorSnapshot:
    aggregate: AggregateStatus
    statuses: tuple[AgentStatus, ...]
    stale_statuses: tuple[AgentStatus, ...]
    sources: tuple[SourceSpec, ...]
    collected_at: datetime

    def to_dict(self) -> dict:
        return {
            "collected_at": self.collected_at.isoformat(),
            "sources": [
                {"provider": source.provider, "path": str(source.path)}
                for source in self.sources
            ],
            "aggregate": self.aggregate.to_dict(self.collected_at),
            "statuses": [
                status.to_dict(self.collected_at) for status in self.statuses
            ],
            "stale_statuses": [
                status.to_dict(self.collected_at) for status in self.stale_statuses
            ],
        }


class AgentMonitor:
    def __init__(
        self,
        sources: Iterable[SourceSpec] | None = None,
        stale_after_seconds: float = 3600.0,
        tool_running_timeout_seconds: float = 0.0,
        completed_visible_seconds: float = COMPLETED_VISIBLE_SECONDS,
        idle_visible_seconds: float = IDLE_VISIBLE_SECONDS,
        post_tool_working_visible_seconds: float = POST_TOOL_WORKING_VISIBLE_SECONDS,
        max_lines_per_source: int = 5000,
    ) -> None:
        self.sources = tuple(sources) if sources is not None else default_sources()
        self.stale_after_seconds = stale_after_seconds
        self.tool_running_timeout_seconds = tool_running_timeout_seconds
        self.completed_visible_seconds = completed_visible_seconds
        self.idle_visible_seconds = idle_visible_seconds
        self.post_tool_working_visible_seconds = post_tool_working_visible_seconds
        self.max_lines_per_source = max_lines_per_source
        self._log_records_cache: dict[tuple[str, str, int], CachedTranscriptRecords] = {}
        self._transcript_records_cache: dict[tuple[str, str], CachedTranscriptRecords] = {}
        self._transcript_file_list_cache: dict[tuple[str, int], CachedTranscriptFileList] = {}
        self._latest_status_signature: tuple[Any, ...] | None = None
        self._latest_statuses_by_key: dict[str, AgentStatus] | None = None

    @classmethod
    def from_default_sources(
        cls,
        stale_after_seconds: float = 3600.0,
        tool_running_timeout_seconds: float = 0.0,
        completed_visible_seconds: float = COMPLETED_VISIBLE_SECONDS,
        idle_visible_seconds: float = IDLE_VISIBLE_SECONDS,
        post_tool_working_visible_seconds: float = POST_TOOL_WORKING_VISIBLE_SECONDS,
        max_lines_per_source: int = 5000,
    ) -> "AgentMonitor":
        return cls(
            stale_after_seconds=stale_after_seconds,
            tool_running_timeout_seconds=tool_running_timeout_seconds,
            completed_visible_seconds=completed_visible_seconds,
            idle_visible_seconds=idle_visible_seconds,
            post_tool_working_visible_seconds=post_tool_working_visible_seconds,
            max_lines_per_source=max_lines_per_source,
        )

    def snapshot(self, include_stale: bool = False) -> MonitorSnapshot:
        now = datetime.now(timezone.utc)
        statuses_by_key = self._latest_statuses()

        fresh: list[AgentStatus] = []
        stale: list[AgentStatus] = []
        for status in statuses_by_key.values():
            effective = status_for_snapshot(
                status,
                now,
                post_tool_working_visible_seconds=self.post_tool_working_visible_seconds,
            )
            is_stale = self.is_stale_status(effective, now)
            current = _replace_stale(effective, is_stale)
            if is_stale:
                stale.append(current)
            else:
                fresh.append(current)

        # Only session-level rows may demote completed sessions to stale: an
        # orphaned subagent row (its parent already done, its Stop event lost)
        # would otherwise hide the parent's COMPLETED row from the aggregate
        # and keep the overall state "working" forever.
        if any(
            status_counts_active(status) and ":agent:" not in status.agent_id
            for status in fresh
        ):
            inactive = [status for status in fresh if not status_counts_active(status)]
            fresh = [status for status in fresh if status_counts_active(status)]
            stale.extend(_replace_stale(status, True) for status in inactive)

        fresh.sort(key=lambda status: (status.priority, -status.updated_at.timestamp()))
        stale.sort(key=lambda status: (status.priority, -status.updated_at.timestamp()))

        visible = tuple(fresh)
        stale_visible = tuple(stale if include_stale else stale)
        aggregate = aggregate_status(visible, stale_visible)

        return MonitorSnapshot(
            aggregate=aggregate,
            statuses=visible,
            stale_statuses=stale_visible,
            sources=self.sources,
            collected_at=now,
        )

    def _latest_statuses(self) -> dict[str, AgentStatus]:
        signature = self._input_signature()
        if (
            self._latest_status_signature == signature
            and self._latest_statuses_by_key is not None
        ):
            return dict(self._latest_statuses_by_key)

        statuses_by_key: dict[str, AgentStatus] = {}
        metadata_by_session: dict[str, StatusMetadata] = {}
        metadata_by_status: dict[str, StatusMetadata] = {}
        pending_permissions_by_key: dict[str, set[str]] = {}

        records = sorted(
            self._iter_records(),
            key=lambda record: record.logged_at,
        )

        for record in records:
            metadata = metadata_for_record(
                record,
                metadata_by_session,
                metadata_by_status,
            )
            status = status_from_event(record, metadata)
            if status is not None:
                track_pending_permissions(record, pending_permissions_by_key)
                previous = statuses_by_key.get(status.agent_id)
                if should_ignore_status_transition(
                    previous,
                    status,
                    pending_permissions_by_key.get(status.agent_id, set()),
                ):
                    continue
                statuses_by_key[status.agent_id] = status
                complete_subagents_for_ended_session(record, statuses_by_key)

        self._latest_status_signature = signature
        self._latest_statuses_by_key = dict(statuses_by_key)
        return statuses_by_key

    def _input_signature(self) -> tuple[Any, ...]:
        parts: list[Any] = []
        for source in self.sources:
            if source.provider == CODEX_TRANSCRIPT_PROVIDER:
                parts.append(
                    (
                        source.provider,
                        str(source.path),
                        self._transcript_source_signature(
                            source.path,
                            limit=CODEX_TRANSCRIPT_MAX_FILES,
                        ),
                    )
                )
                continue
            if source.provider == CLAUDE_TRANSCRIPT_PROVIDER:
                parts.append(
                    (
                        source.provider,
                        str(source.path),
                        self._transcript_source_signature(
                            source.path,
                            limit=CLAUDE_TRANSCRIPT_MAX_FILES,
                        ),
                    )
                )
                continue

            parts.append((source.provider, str(source.path), file_signature(source.path)))
        return tuple(parts)

    def _transcript_source_signature(
        self,
        root: Path,
        *,
        limit: int,
    ) -> tuple[tuple[str, tuple[float, int] | None], ...]:
        return tuple(
            (str(path), file_signature(path))
            for path in self._recent_transcript_files(root, limit=limit)
        )

    def _iter_records(self) -> Iterable[HookEvent]:
        for source in self.sources:
            if not source.path.exists():
                continue
            if source.provider == CODEX_TRANSCRIPT_PROVIDER:
                yield from self._iter_codex_transcript_records(source.path)
                continue
            if source.provider == CLAUDE_TRANSCRIPT_PROVIDER:
                yield from self._iter_claude_transcript_records(source.path)
                continue
            yield from self._cached_log_records(source)

    def _cached_log_records(self, source: SourceSpec) -> tuple[HookEvent, ...]:
        try:
            stat = source.path.stat()
        except OSError:
            return ()

        key = (source.provider, str(source.path), self.max_lines_per_source)
        cached = self._log_records_cache.get(key)
        if cached is not None and cached.mtime == stat.st_mtime and cached.size == stat.st_size:
            return cached.records

        records: list[HookEvent] = []
        for line in read_recent_lines(source.path, self.max_lines_per_source):
            record = parse_log_line(source.provider, line)
            if record is not None:
                records.append(record)

        cached_records = tuple(records)
        self._log_records_cache[key] = CachedTranscriptRecords(
            mtime=stat.st_mtime,
            size=stat.st_size,
            records=cached_records,
        )
        return cached_records

    def _iter_codex_transcript_records(self, root: Path) -> Iterable[HookEvent]:
        for path in self._recent_transcript_files(root, limit=CODEX_TRANSCRIPT_MAX_FILES):
            yield from self._cached_transcript_records(
                CODEX_TRANSCRIPT_PROVIDER,
                path,
                iter_codex_transcript_file,
            )

    def _iter_claude_transcript_records(self, root: Path) -> Iterable[HookEvent]:
        for path in self._recent_transcript_files(root, limit=CLAUDE_TRANSCRIPT_MAX_FILES):
            yield from self._cached_transcript_records(
                CLAUDE_TRANSCRIPT_PROVIDER,
                path,
                iter_claude_transcript_file,
            )

    def _recent_transcript_files(self, root: Path, *, limit: int) -> tuple[Path, ...]:
        key = (str(root), limit)
        now = time.monotonic()
        cached = self._transcript_file_list_cache.get(key)
        if cached is not None and now < cached.expires_at:
            return cached.paths

        paths = tuple(recent_transcript_files(root, limit=limit))
        self._transcript_file_list_cache[key] = CachedTranscriptFileList(
            expires_at=now + TRANSCRIPT_FILE_LIST_CACHE_SECONDS,
            paths=paths,
        )
        return paths

    def _cached_transcript_records(
        self,
        provider: str,
        path: Path,
        parser: Callable[[Path], Iterable[HookEvent]],
    ) -> tuple[HookEvent, ...]:
        try:
            stat = path.stat()
        except OSError:
            return ()

        key = (provider, str(path))
        cached = self._transcript_records_cache.get(key)
        if cached is not None and cached.mtime == stat.st_mtime and cached.size == stat.st_size:
            return cached.records

        records = tuple(parser(path))
        self._transcript_records_cache[key] = CachedTranscriptRecords(
            mtime=stat.st_mtime,
            size=stat.st_size,
            records=records,
        )
        return records

    def is_stale_status(self, status: AgentStatus, now: datetime) -> bool:
        age = status.age_seconds(now)
        if status.mode == AgentMode.COMPLETED and self.completed_visible_seconds >= 0:
            return age > self.completed_visible_seconds
        if status.mode == AgentMode.IDLE_READY and self.idle_visible_seconds >= 0:
            return age > self.idle_visible_seconds
        return (
            age > self.stale_after_seconds
            or self.is_expired_tool_running(status, now)
        )

    def is_expired_tool_running(self, status: AgentStatus, now: datetime) -> bool:
        return (
            status.mode == AgentMode.TOOL_RUNNING
            and self.tool_running_timeout_seconds > 0
            and status.age_seconds(now) > self.tool_running_timeout_seconds
        )


class LiveAgentMonitor:
    def __init__(
        self,
        *,
        sources: Iterable[SourceSpec] = (),
        stale_after_seconds: float = 3600.0,
        tool_running_timeout_seconds: float = 0.0,
        completed_visible_seconds: float = COMPLETED_VISIBLE_SECONDS,
        idle_visible_seconds: float = IDLE_VISIBLE_SECONDS,
        post_tool_working_visible_seconds: float = POST_TOOL_WORKING_VISIBLE_SECONDS,
        latest_state_path: Path | None = None,
    ) -> None:
        self.sources = tuple(sources)
        self.stale_after_seconds = stale_after_seconds
        self.tool_running_timeout_seconds = tool_running_timeout_seconds
        self.completed_visible_seconds = completed_visible_seconds
        self.idle_visible_seconds = idle_visible_seconds
        self.post_tool_working_visible_seconds = post_tool_working_visible_seconds
        self.latest_state_path = latest_state_path
        self.lock = threading.RLock()
        self.statuses_by_key: dict[str, AgentStatus] = {}
        self.metadata_by_session: dict[str, StatusMetadata] = {}
        self.metadata_by_status: dict[str, StatusMetadata] = {}
        self.pending_permissions_by_key: dict[str, set[str]] = {}
        self.load_latest_state()

    def ingest_record(self, record: HookEvent) -> None:
        with self.lock:
            metadata = metadata_for_record(
                record,
                self.metadata_by_session,
                self.metadata_by_status,
            )
            status = status_from_event(record, metadata)
            if status is None:
                return

            track_pending_permissions(record, self.pending_permissions_by_key)
            previous = self.statuses_by_key.get(status.agent_id)
            if should_ignore_status_transition(
                previous,
                status,
                self.pending_permissions_by_key.get(status.agent_id, set()),
            ):
                return
            self.statuses_by_key[status.agent_id] = status
            complete_subagents_for_ended_session(record, self.statuses_by_key)
            self.write_latest_state()

    def snapshot(self, include_stale: bool = False) -> MonitorSnapshot:
        now = datetime.now(timezone.utc)
        with self.lock:
            statuses = tuple(self.statuses_by_key.values())
        return snapshot_from_statuses(
            statuses,
            sources=self.sources,
            collected_at=now,
            stale_after_seconds=self.stale_after_seconds,
            tool_running_timeout_seconds=self.tool_running_timeout_seconds,
            completed_visible_seconds=self.completed_visible_seconds,
            idle_visible_seconds=self.idle_visible_seconds,
            post_tool_working_visible_seconds=self.post_tool_working_visible_seconds,
            include_stale=include_stale,
        )

    def load_latest_state(self) -> None:
        if self.latest_state_path is None or not self.latest_state_path.exists():
            return
        try:
            data = json.loads(self.latest_state_path.read_text())
        except Exception:
            return
        statuses = data.get("statuses") if isinstance(data, dict) else None
        if not isinstance(statuses, list):
            return
        loaded: dict[str, AgentStatus] = {}
        for status_data in statuses:
            status = agent_status_from_dict(status_data)
            if status is not None:
                loaded[status.agent_id] = status
        self.statuses_by_key.update(loaded)

    def write_latest_state(self) -> None:
        if self.latest_state_path is None:
            return
        now = datetime.now(timezone.utc)
        payload = {
            "updated_at": now.isoformat(),
            "statuses": [
                status.to_dict(now) for status in self.statuses_by_key.values()
            ],
        }
        try:
            self.latest_state_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.latest_state_path.with_suffix(".json.tmp")
            temp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            temp_path.replace(self.latest_state_path)
        except OSError:
            pass


def default_sources(settings: AgentMonitorSettings | None = None) -> tuple[SourceSpec, ...]:
    from .remote_hosts import configured_remote_logs

    active_settings = load_settings() if settings is None else settings
    sources = [
        SourceSpec("codex", detect_log_path("codex")),
    ]
    if active_settings.codex_transcripts_enabled:
        sources.append(SourceSpec(CODEX_TRANSCRIPT_PROVIDER, Path.home() / ".codex" / "sessions"))
    sources.append(SourceSpec("claude", detect_log_path("claude")))
    if active_settings.claude_transcripts_enabled:
        sources.append(SourceSpec(CLAUDE_TRANSCRIPT_PROVIDER, Path.home() / ".claude" / "projects"))
    sources.append(SourceSpec("grok", detect_log_path("grok")))
    sources.extend(SourceSpec(provider, path) for provider, path in configured_remote_logs())
    return unique_sources(sources)


def unique_sources(sources: Iterable[SourceSpec]) -> tuple[SourceSpec, ...]:
    seen: set[tuple[str, str]] = set()
    result: list[SourceSpec] = []
    for source in sources:
        key = (source.provider, str(source.path.expanduser()))
        if key in seen:
            continue
        seen.add(key)
        result.append(SourceSpec(source.provider, source.path.expanduser()))
    return tuple(result)


def iter_codex_transcript_records(root: Path) -> Iterable[HookEvent]:
    for path in recent_transcript_files(root):
        yield from iter_codex_transcript_file(path)


def recent_transcript_files(
    root: Path,
    *,
    limit: int = CODEX_TRANSCRIPT_MAX_FILES,
) -> list[Path]:
    try:
        files = [path for path in root.rglob("*.jsonl") if path.is_file()]
    except OSError:
        return []

    files.sort(key=lambda path: safe_mtime(path), reverse=True)
    return files[:limit]


def safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def file_signature(path: Path) -> tuple[float, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime, stat.st_size)


def iter_codex_transcript_file(path: Path) -> Iterable[HookEvent]:
    session_id = codex_session_id_from_path(path)
    if session_id is None:
        return

    cwd = None
    turn_id = None
    for line in read_recent_lines(path, CODEX_TRANSCRIPT_MAX_LINES):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue

        timestamp = parse_transcript_timestamp(row)
        row_type = row.get("type")
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue

        if row_type == "turn_context":
            cwd = _string_or_none(payload.get("cwd")) or cwd
            turn_id = _string_or_none(payload.get("turn_id")) or turn_id
            continue

        event = codex_transcript_event(
            payload,
            session_id=session_id,
            turn_id=turn_id,
            cwd=cwd,
            timestamp=timestamp,
            path=path,
        )
        if event is not None:
            yield event


def codex_session_id_from_path(path: Path) -> str | None:
    match = re.search(
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        path.name,
        flags=re.IGNORECASE,
    )
    return match.group(1) if match else None


def parse_transcript_timestamp(row: dict[str, Any]) -> datetime:
    from .models import parse_datetime

    return parse_datetime(row.get("timestamp"))


def codex_transcript_event(
    payload: dict[str, Any],
    *,
    session_id: str,
    turn_id: str | None,
    cwd: str | None,
    timestamp: datetime,
    path: Path,
) -> HookEvent | None:
    payload_type = payload.get("type")

    if payload_type == "message":
        role = payload.get("role")
        if role == "user":
            prompt = message_text_from_content(payload.get("content"))
            return HookEvent(
                provider="codex",
                logged_at=timestamp,
                event_name="UserPromptSubmit",
                raw={
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "cwd": cwd,
                    "prompt": prompt,
                    "transcript_path": str(path),
                    "source": CODEX_TRANSCRIPT_PROVIDER,
                },
                session_id=session_id,
                turn_id=turn_id,
                cwd=cwd,
                message=prompt,
            )
        if role == "assistant":
            message = message_text_from_content(payload.get("content"))
            return HookEvent(
                provider="codex",
                logged_at=timestamp,
                event_name="Stop",
                raw={
                    "hook_event_name": "Stop",
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "cwd": cwd,
                    "last_assistant_message": message,
                    "transcript_path": str(path),
                    "source": CODEX_TRANSCRIPT_PROVIDER,
                },
                session_id=session_id,
                turn_id=turn_id,
                cwd=cwd,
                message=message,
            )

    if payload_type == "function_call":
        tool_name = _string_or_none(payload.get("name"))
        return HookEvent(
            provider="codex",
            logged_at=timestamp,
            event_name="PreToolUse",
            raw={
                "hook_event_name": "PreToolUse",
                "session_id": session_id,
                "turn_id": turn_id,
                "cwd": cwd,
                "tool_name": tool_name,
                "tool_input": payload.get("arguments"),
                "tool_use_id": payload.get("call_id"),
                "transcript_path": str(path),
                "source": CODEX_TRANSCRIPT_PROVIDER,
            },
            session_id=session_id,
            turn_id=turn_id,
            cwd=cwd,
            tool_name=tool_name,
        )

    if payload_type == "function_call_output":
        return HookEvent(
            provider="codex",
            logged_at=timestamp,
            event_name="PostToolUse",
            raw={
                "hook_event_name": "PostToolUse",
                "session_id": session_id,
                "turn_id": turn_id,
                "cwd": cwd,
                "tool_response": payload.get("output"),
                "tool_use_id": payload.get("call_id"),
                "transcript_path": str(path),
                "source": CODEX_TRANSCRIPT_PROVIDER,
            },
            session_id=session_id,
            turn_id=turn_id,
            cwd=cwd,
        )

    if payload_type == "task_complete":
        message = _string_or_none(payload.get("last_agent_message")) or ""
        return HookEvent(
            provider="codex",
            logged_at=timestamp,
            event_name="Stop",
            raw={
                "hook_event_name": "Stop",
                "session_id": session_id,
                "turn_id": turn_id,
                "cwd": cwd,
                "last_assistant_message": message,
                "transcript_path": str(path),
                "source": CODEX_TRANSCRIPT_PROVIDER,
            },
            session_id=session_id,
            turn_id=turn_id,
            cwd=cwd,
            message=message,
        )

    return None


def iter_claude_transcript_records(root: Path) -> Iterable[HookEvent]:
    for path in recent_transcript_files(root, limit=CLAUDE_TRANSCRIPT_MAX_FILES):
        yield from iter_claude_transcript_file(path)


def iter_claude_transcript_file(path: Path) -> Iterable[HookEvent]:
    session_id = claude_session_id_from_path(path)
    if session_id is None:
        return

    last_event_at = None
    last_event_name = None
    last_cwd = None
    emitted_event = False
    for line in read_recent_lines(path, CLAUDE_TRANSCRIPT_MAX_LINES):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue

        last_cwd = _string_or_none(row.get("cwd")) or last_cwd
        event = claude_transcript_event(
            row,
            session_id=session_id,
            timestamp=parse_transcript_timestamp(row),
            path=path,
        )
        if event is not None:
            emitted_event = True
            last_event_at = event.logged_at
            last_event_name = event.event_name
            last_cwd = event.cwd or last_cwd
            yield event

    if not emitted_event or last_event_at is None:
        return
    if not claude_mtime_can_extend_event(last_event_name):
        return

    mtime = datetime.fromtimestamp(safe_mtime(path), timezone.utc)
    if (mtime - last_event_at).total_seconds() <= CLAUDE_TRANSCRIPT_MTIME_HEARTBEAT_SKEW_SECONDS:
        return

    yield HookEvent(
        provider="claude",
        logged_at=mtime,
        event_name="Notification",
        raw={
            "hook_event_name": "Notification",
            "session_id": session_id,
            "cwd": last_cwd,
            "notification_type": "transcript_mtime",
            "message": "Claude transcript file changed after the last embedded event.",
            "transcript_path": str(path),
            "source": CLAUDE_TRANSCRIPT_PROVIDER,
        },
        session_id=session_id,
        cwd=last_cwd,
        message="Claude transcript file changed after the last embedded event.",
    )


def claude_mtime_can_extend_event(event_name: str | None) -> bool:
    return event_name in {
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "PreCompact",
        "PostCompact",
        "SubagentStart",
    }


def claude_session_id_from_path(path: Path) -> str | None:
    match = re.search(
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        path.name,
        flags=re.IGNORECASE,
    )
    return match.group(1) if match else None


def claude_transcript_event(
    row: dict[str, Any],
    *,
    session_id: str,
    timestamp: datetime,
    path: Path,
) -> HookEvent | None:
    row_type = row.get("type")
    cwd = _string_or_none(row.get("cwd"))

    if row_type == "user":
        if row.get("isMeta") is True:
            return None

        message = row.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if claude_content_has_tool_result(content) or row.get("toolUseResult") is not None:
            failed = claude_tool_result_failed(content, row.get("toolUseResult"))
            event_name = "PostToolUseFailure" if failed else "PostToolUse"
            return HookEvent(
                provider="claude",
                logged_at=timestamp,
                event_name=event_name,
                raw={
                    "hook_event_name": event_name,
                    "session_id": session_id,
                    "cwd": cwd,
                    "tool_response": row.get("toolUseResult") or content,
                    "tool_use_id": row.get("sourceToolAssistantUUID"),
                    "transcript_path": str(path),
                    "source": CLAUDE_TRANSCRIPT_PROVIDER,
                },
                session_id=session_id,
                cwd=cwd,
            )

        prompt = message_text_from_content(content)
        if not prompt:
            return None
        if prompt.strip().startswith("<task-notification>"):
            return None
        return HookEvent(
            provider="claude",
            logged_at=timestamp,
            event_name="UserPromptSubmit",
            raw={
                "hook_event_name": "UserPromptSubmit",
                "session_id": session_id,
                "cwd": cwd,
                "prompt": prompt,
                "transcript_path": str(path),
                "source": CLAUDE_TRANSCRIPT_PROVIDER,
            },
            session_id=session_id,
            cwd=cwd,
            message=prompt,
        )

    if row_type == "assistant":
        message = row.get("message")
        if not isinstance(message, dict):
            return None

        content = message.get("content")
        tool_use = first_claude_tool_use(content)
        if tool_use is not None:
            tool_name = _string_or_none(tool_use.get("name"))
            return HookEvent(
                provider="claude",
                logged_at=timestamp,
                event_name="PreToolUse",
                raw={
                    "hook_event_name": "PreToolUse",
                    "session_id": session_id,
                    "cwd": cwd,
                    "tool_name": tool_name,
                    "tool_input": tool_use.get("input"),
                    "tool_use_id": tool_use.get("id"),
                    "transcript_path": str(path),
                    "source": CLAUDE_TRANSCRIPT_PROVIDER,
                },
                session_id=session_id,
                cwd=cwd,
                tool_name=tool_name,
            )

        if message.get("stop_reason") == "end_turn":
            text = message_text_from_content(content)
            return HookEvent(
                provider="claude",
                logged_at=timestamp,
                event_name="Stop",
                raw={
                    "hook_event_name": "Stop",
                    "session_id": session_id,
                    "cwd": cwd,
                    "last_assistant_message": text,
                    "transcript_path": str(path),
                    "source": CLAUDE_TRANSCRIPT_PROVIDER,
                },
                session_id=session_id,
                cwd=cwd,
                message=text,
            )

    return None


def claude_content_has_tool_result(content: object) -> bool:
    if not isinstance(content, list):
        return False
    return any(
        isinstance(item, dict) and item.get("type") == "tool_result"
        for item in content
    )


def claude_tool_result_failed(content: object, tool_use_result: object) -> bool:
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "tool_result":
                if item.get("is_error") is True:
                    return True
                if _tool_response_looks_failed(item.get("content")):
                    return True
    return _tool_response_looks_failed(tool_use_result)


def first_claude_tool_use(content: object) -> dict[str, Any] | None:
    if not isinstance(content, list):
        return None
    for item in content:
        if isinstance(item, dict) and item.get("type") == "tool_use":
            return item
    return None


def message_text_from_content(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def metadata_for_record(
    record: HookEvent,
    metadata_by_session: dict[str, StatusMetadata],
    metadata_by_status: dict[str, StatusMetadata],
) -> StatusMetadata:
    session_metadata = None
    if record.session_id:
        session_metadata = metadata_by_session.setdefault(
            f"{record.provider}:session:{record.session_id}",
            StatusMetadata(),
        )
        update_metadata(session_metadata, record)

    status_metadata = metadata_by_status.setdefault(record.status_key, StatusMetadata())
    update_metadata(status_metadata, record)

    if session_metadata is None:
        return status_metadata
    return StatusMetadata(
        cwd=status_metadata.cwd or session_metadata.cwd,
        title=status_metadata.title or session_metadata.title,
        origin=status_metadata.origin or session_metadata.origin,
    )


def update_metadata(metadata: StatusMetadata, record: HookEvent) -> None:
    if record.cwd:
        metadata.cwd = record.cwd

    title = title_from_event(record)
    if title and (metadata.title is None or is_provider_session_title(record, title)):
        metadata.title = title

    origin = record.origin or origin_label_from_payload(record.provider, record.raw)
    if origin:
        metadata.origin = origin


def is_provider_session_title(record: HookEvent, title: str) -> bool:
    if record.provider == "codex":
        return title == codex_session_title(record.session_id)
    return False


def status_from_event(record: HookEvent, metadata: StatusMetadata | None = None) -> AgentStatus | None:
    mode = mode_for_event(record)
    if mode is None:
        return None

    metadata = metadata or StatusMetadata(cwd=record.cwd)
    if should_ignore_record(record, metadata):
        return None

    if record.agent_id:
        short_id = record.agent_id[:8]
        fallback = f"{provider_label(record.provider)} agent {short_id}"
        display_name = display_name_for_record(record, metadata, f"agent {short_id}", fallback)
    elif record.session_id:
        short_id = record.session_id[:8]
        fallback = f"{provider_label(record.provider)} session {short_id}"
        display_name = display_name_for_record(record, metadata, short_id, fallback)
    else:
        display_name = provider_label(record.provider)

    if is_ignored_display_name(display_name):
        return None

    return AgentStatus(
        provider=record.provider,
        agent_id=record.status_key,
        display_name=display_name,
        mode=mode,
        updated_at=record.logged_at,
        event_name=record.event_name,
        session_id=record.session_id,
        cwd=record.cwd,
        tool_name=record.tool_name,
        message=record.message,
        origin=record.origin or metadata.origin or origin_label_from_payload(record.provider, record.raw),
    )


def mode_for_event(record: HookEvent) -> AgentMode | None:
    event = record.event_name
    raw = record.raw
    explicit_mode = explicit_mode_for_record(record)
    if explicit_mode is not None:
        return explicit_mode

    if event in {"PostToolUseFailure", "PermissionDenied", "StopFailure"}:
        return AgentMode.BLOCKED_ERROR
    if event in {"PermissionRequest"}:
        return AgentMode.WAITING_FOR_INPUT
    if event == "Notification":
        notification_type = str(raw.get("notification_type", "")).strip().lower()
        message = str(raw.get("message", "")).strip().lower()
        text = " ".join(
            str(raw.get(key, ""))
            for key in ("notification_type", "message")
        ).lower()
        if notification_text_indicates_completion(notification_type, message):
            return AgentMode.COMPLETED
        if notification_text_indicates_input_needed(text):
            return AgentMode.WAITING_FOR_INPUT
        return AgentMode.WORKING
    if event in {"PreToolUse"}:
        return AgentMode.TOOL_RUNNING
    if event in {"PostToolUse"}:
        if _tool_response_looks_failed(raw.get("tool_response")):
            return AgentMode.BLOCKED_ERROR
        return AgentMode.WORKING
    if event in {"UserPromptSubmit", "PreCompact", "PostCompact", "SubagentStart"}:
        return AgentMode.WORKING
    if event in {"Stop", "SubagentStop"}:
        if _assistant_message_asks_question(raw.get("last_assistant_message")):
            return AgentMode.WAITING_FOR_INPUT
        # The turn ended, but the harness reports background tasks still
        # running (run_in_background shells, monitors) — the session is not
        # done until they close, at which point the harness re-invokes the
        # session and fresh events flow.
        background_tasks = raw.get("background_tasks")
        if isinstance(background_tasks, list) and any(
            isinstance(task, dict) and task.get("status") == "running"
            for task in background_tasks
        ):
            return AgentMode.LONG_TASK_PROGRESS
        return AgentMode.COMPLETED
    if event in {"SessionEnd"}:
        return AgentMode.COMPLETED
    if event == "SessionStart":
        return AgentMode.IDLE_READY
    return None


def explicit_mode_for_record(record: HookEvent) -> AgentMode | None:
    raw = record.raw
    for key in ("sidepulse_status", "sidepulse_mode", "sidepulse_status", "sidepulse_mode"):
        mode = explicit_mode_from_value(raw.get(key))
        if mode is not None:
            return mode

    return explicit_mode_from_message(
        raw.get("last_assistant_message") or raw.get("message")
    )


def notification_text_indicates_completion(notification_type: str, message: str) -> bool:
    text = f"{notification_type} {message}".strip()
    completion_phrases = (
        "turn complete",
        "turn completed",
        "task complete",
        "task completed",
        "completed successfully",
        "work complete",
        "work completed",
    )
    if any(phrase in text for phrase in completion_phrases):
        return True
    return notification_type == "idle_prompt" and message in {"done", "complete", "completed"}


def notification_text_indicates_input_needed(text: str) -> bool:
    return any(
        phrase in text
        for phrase in (
            "waiting for your input",
            "waiting for input",
            "needs your input",
            "needs input",
            "permission",
            "approval",
            "confirm",
        )
    )


def explicit_mode_from_value(value: object) -> AgentMode | None:
    if not isinstance(value, str):
        return None

    normalized = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    return {
        "ask": AgentMode.WAITING_FOR_INPUT,
        "question": AgentMode.WAITING_FOR_INPUT,
        "waiting": AgentMode.WAITING_FOR_INPUT,
        "waiting_for_input": AgentMode.WAITING_FOR_INPUT,
        "input": AgentMode.WAITING_FOR_INPUT,
        "blocked": AgentMode.BLOCKED_ERROR,
        "error": AgentMode.BLOCKED_ERROR,
        "blocked_error": AgentMode.BLOCKED_ERROR,
        "working": AgentMode.WORKING,
        "tool_running": AgentMode.TOOL_RUNNING,
        "progress": AgentMode.LONG_TASK_PROGRESS,
        "long_task_progress": AgentMode.LONG_TASK_PROGRESS,
        "done": AgentMode.COMPLETED,
        "complete": AgentMode.COMPLETED,
        "completed": AgentMode.COMPLETED,
        "idle": AgentMode.IDLE_READY,
        "ready": AgentMode.IDLE_READY,
        "idle_ready": AgentMode.IDLE_READY,
    }.get(normalized)


def explicit_mode_from_message(message: object) -> AgentMode | None:
    if not isinstance(message, str):
        return None

    text = strip_markdown_code_blocks(message)
    patterns = (
        r"(?im)^\s*<!--\s*(?:sidepulse|agent[-_ ]monitor)\s*:\s*([a-z0-9_ -]+)\s*-->\s*$",
        r"(?im)^\s*<!--\s*(?:sidepulse|agent[-_ ]monitor)\s+(?:status|mode)\s*:\s*([a-z0-9_ -]+)\s*-->\s*$",
        r"(?im)^\s*\[(?:sidepulse|agent[-_ ]monitor)\s+(?:status|mode)\s*:\s*([a-z0-9_ -]+)\]\s*$",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            mode = explicit_mode_from_value(match.group(1))
            if mode is not None:
                return mode

    return None


def strip_markdown_code_blocks(text: str) -> str:
    return re.sub(r"```.*?```", "", text, flags=re.DOTALL)


def strip_markdown_inline_code(text: str) -> str:
    return re.sub(r"`[^`\n]*`", "", text)


def aggregate_status(
    statuses: tuple[AgentStatus, ...],
    stale_statuses: tuple[AgentStatus, ...] = (),
) -> AggregateStatus:
    # A subagent's activity is subsumed by its parent session, and an
    # orphaned subagent (parent already done) must not keep the overall
    # state "working"; drive the aggregate from session-level rows.
    effective = tuple(s for s in statuses if ":agent:" not in s.agent_id)
    if not effective:
        return AggregateStatus(
            mode=AgentMode.IDLE_READY,
            active_count=0,
            stale_count=len(stale_statuses),
            representative=None,
        )

    representative = min(
        effective,
        key=lambda status: (
            MODE_PRIORITY.get(status.mode, MODE_PRIORITY[AgentMode.UNKNOWN]),
            -status.updated_at.timestamp(),
        ),
    )

    return AggregateStatus(
        mode=representative.mode,
        active_count=sum(1 for status in effective if status_counts_active(status)),
        stale_count=len(stale_statuses),
        representative=representative,
    )


def snapshot_from_statuses(
    statuses: tuple[AgentStatus, ...],
    *,
    sources: tuple[SourceSpec, ...],
    collected_at: datetime,
    stale_after_seconds: float,
    tool_running_timeout_seconds: float,
    completed_visible_seconds: float,
    idle_visible_seconds: float,
    post_tool_working_visible_seconds: float = POST_TOOL_WORKING_VISIBLE_SECONDS,
    include_stale: bool = False,
) -> MonitorSnapshot:
    fresh: list[AgentStatus] = []
    stale: list[AgentStatus] = []
    for status in statuses:
        status = status_for_snapshot(
            status,
            collected_at,
            post_tool_working_visible_seconds=post_tool_working_visible_seconds,
        )
        is_stale = status_is_stale(
            status,
            collected_at,
            stale_after_seconds=stale_after_seconds,
            tool_running_timeout_seconds=tool_running_timeout_seconds,
            completed_visible_seconds=completed_visible_seconds,
            idle_visible_seconds=idle_visible_seconds,
        )
        current = _replace_stale(status, is_stale)
        if is_stale:
            stale.append(current)
        else:
            fresh.append(current)

    # Only session-level rows may demote completed sessions to stale (see the
    # matching check in AgentMonitor.snapshot): an orphaned subagent row must
    # not hide the parent's COMPLETED row from the aggregate.
    if any(
        status_counts_active(status) and ":agent:" not in status.agent_id
        for status in fresh
    ):
        inactive = [status for status in fresh if not status_counts_active(status)]
        fresh = [status for status in fresh if status_counts_active(status)]
        stale.extend(_replace_stale(status, True) for status in inactive)

    fresh.sort(key=lambda status: (status.priority, -status.updated_at.timestamp()))
    stale.sort(key=lambda status: (status.priority, -status.updated_at.timestamp()))

    visible = tuple(fresh)
    stale_visible = tuple(stale if include_stale else stale)
    return MonitorSnapshot(
        aggregate=aggregate_status(visible, stale_visible),
        statuses=visible,
        stale_statuses=stale_visible,
        sources=sources,
        collected_at=collected_at,
    )


def status_is_stale(
    status: AgentStatus,
    now: datetime,
    *,
    stale_after_seconds: float,
    tool_running_timeout_seconds: float,
    completed_visible_seconds: float,
    idle_visible_seconds: float,
) -> bool:
    age = status.age_seconds(now)
    if status.mode == AgentMode.COMPLETED and completed_visible_seconds >= 0:
        return age > completed_visible_seconds
    if status.mode == AgentMode.IDLE_READY and idle_visible_seconds >= 0:
        return age > idle_visible_seconds
    return (
        age > stale_after_seconds
        or (
            status.mode == AgentMode.TOOL_RUNNING
            and tool_running_timeout_seconds > 0
            and age > tool_running_timeout_seconds
        )
    )


def status_for_snapshot(
    status: AgentStatus,
    now: datetime,
    *,
    post_tool_working_visible_seconds: float,
) -> AgentStatus:
    if (
        status.mode == AgentMode.WORKING
        and status.event_name == "PostToolUse"
        and post_tool_working_visible_seconds >= 0
        and status.age_seconds(now) > post_tool_working_visible_seconds
    ):
        return _replace_mode(status, AgentMode.COMPLETED)
    return status


def agent_status_from_dict(data: object) -> AgentStatus | None:
    if not isinstance(data, dict):
        return None
    try:
        provider = str(data["provider"])
        agent_id = str(data["agent_id"])
        session_id = _string_or_none(data.get("session_id"))
        cwd = _string_or_none(data.get("cwd"))
        display_name = str(data["display_name"])
        if provider == "codex" and session_id:
            title = codex_session_title(session_id)
            if title:
                display_name = display_name_from_parts(
                    project_name(cwd),
                    title,
                    session_id[:8],
                    display_name,
                )

        mode = AgentMode(str(data["mode"]))
        updated_at = parse_datetime(data["updated_at"])
        return AgentStatus(
            provider=provider,
            agent_id=agent_id,
            display_name=display_name,
            mode=mode,
            updated_at=updated_at,
            event_name=str(data["event_name"]),
            session_id=session_id,
            cwd=cwd,
            tool_name=_string_or_none(data.get("tool_name")),
            message=_string_or_none(data.get("message")),
            origin=_string_or_none(data.get("origin")),
            stale=bool(data.get("stale", False)),
        )
    except Exception:
        return None


def status_counts_active(status: AgentStatus) -> bool:
    return status.mode not in {AgentMode.COMPLETED, AgentMode.IDLE_READY}


def complete_subagents_for_ended_session(
    record: HookEvent,
    statuses_by_key: dict[str, AgentStatus],
) -> None:
    """Mark a session's subagent rows completed when the session itself ends.

    SubagentStop rows are keyed by agent id, so a later SessionEnd for the parent
    session would otherwise leave them visible until the generic stale timeout.
    """
    if record.event_name != "SessionEnd" or record.agent_id or not record.session_id:
        return
    for key, status in statuses_by_key.items():
        if (
            key != record.status_key
            and status.provider == record.provider
            and status.session_id == record.session_id
            and status_counts_active(status)
        ):
            statuses_by_key[key] = _replace_mode(status, AgentMode.COMPLETED)


def track_pending_permissions(
    record: HookEvent,
    pending_permissions_by_key: dict[str, set[str]],
) -> None:
    signature = permission_signature(record)
    if record.event_name == "PermissionRequest" and signature:
        pending_permissions_by_key.setdefault(record.status_key, set()).add(signature)
        return

    if record.event_name == "PostToolUse" and signature:
        pending = pending_permissions_by_key.get(record.status_key)
        if pending is not None:
            pending.discard(signature)
            if not pending:
                pending_permissions_by_key.pop(record.status_key, None)
        return

    if record.event_name in {"Stop", "SessionEnd", "UserPromptSubmit"}:
        pending_permissions_by_key.pop(record.status_key, None)


def permission_signature(record: HookEvent) -> str | None:
    raw = record.raw
    tool_name = _string_or_none(raw.get("tool_name")) or record.tool_name
    tool_input = raw.get("tool_input")
    if not isinstance(tool_input, dict):
        return None

    command = _string_or_none(tool_input.get("command"))
    if command:
        return f"{tool_name or ''}\0{command}"

    return None


def should_ignore_status_transition(
    previous: AgentStatus | None,
    current: AgentStatus,
    pending_permission_signatures: set[str],
) -> bool:
    if (
        previous is not None
        and previous.mode == AgentMode.COMPLETED
        and current.event_name == "Notification"
    ):
        return True

    return (
        previous is not None
        and previous.mode == AgentMode.WAITING_FOR_INPUT
        and previous.event_name == "PermissionRequest"
        and current.event_name != "PermissionRequest"
        and bool(pending_permission_signatures)
    )


# Sessions running in these directories are background automation (memory
# writers, the aura-server agent loop) and never worth surfacing on LEDs,
# in the status bar, or on the phone. Extend with SIDEPULSE_IGNORE_DIRS
# (comma-separated directory names).
DEFAULT_IGNORED_CWD_NAMES = ("memories", "aura-server", "aura")


def ignored_cwd_names() -> frozenset[str]:
    extra = os.environ.get("SIDEPULSE_IGNORE_DIRS", "")
    names = set(DEFAULT_IGNORED_CWD_NAMES)
    names.update(part.strip() for part in extra.split(",") if part.strip())
    return frozenset(names)


def is_ignored_path(cwd: str | None) -> bool:
    if not cwd:
        return False
    ignored = ignored_cwd_names()
    return any(part in ignored for part in Path(cwd).parts)


def is_ignored_display_name(display_name: str) -> bool:
    return any(display_name.startswith(f"{name}:") for name in ignored_cwd_names())


def should_ignore_record(record: HookEvent, metadata: StatusMetadata) -> bool:
    # Match any path component: automation runs often use per-task
    # subdirectories (aura-server/runs/2026...-routine-inbox), so the leaf
    # name alone is not enough.
    if is_ignored_path(metadata.cwd) or is_ignored_path(record.cwd):
        return True

    if record.provider != "codex":
        return False

    raw = record.raw
    text = " ".join(
        part
        for part in (
            metadata.title,
            _string_or_none(raw.get("prompt")),
            _string_or_none(raw.get("message")),
            _string_or_none(raw.get("last_assistant_message")),
        )
        if part
    ).lower()
    if not text:
        return False

    internal_prompts = (
        "generate 0 to 3 hyperpersonalized suggestions",
        "you are an expert at upholding safety and compliance standards",
    )
    return any(prompt in text for prompt in internal_prompts)


def read_recent_lines(path: Path, max_lines: int) -> list[str]:
    if max_lines <= 0:
        return []

    chunk_size = 8192
    chunks: list[bytes] = []
    newline_count = 0

    with path.open("rb") as handle:
        handle.seek(0, 2)
        position = handle.tell()
        while position > 0 and newline_count <= max_lines:
            read_size = min(chunk_size, position)
            position -= read_size
            handle.seek(position)
            chunk = handle.read(read_size)
            chunks.append(chunk)
            newline_count += chunk.count(b"\n")

    data = b"".join(reversed(chunks))
    lines = data.decode("utf-8", errors="replace").splitlines()
    return lines[-max_lines:]


def _replace_stale(status: AgentStatus, stale: bool) -> AgentStatus:
    if status.stale == stale:
        return status
    return AgentStatus(
        provider=status.provider,
        agent_id=status.agent_id,
        display_name=status.display_name,
        mode=status.mode,
        updated_at=status.updated_at,
        event_name=status.event_name,
        session_id=status.session_id,
        cwd=status.cwd,
        tool_name=status.tool_name,
        message=status.message,
        origin=status.origin,
        stale=stale,
    )


def _replace_mode(status: AgentStatus, mode: AgentMode) -> AgentStatus:
    if status.mode == mode:
        return status
    return AgentStatus(
        provider=status.provider,
        agent_id=status.agent_id,
        display_name=status.display_name,
        mode=mode,
        updated_at=status.updated_at,
        event_name=status.event_name,
        session_id=status.session_id,
        cwd=status.cwd,
        tool_name=status.tool_name,
        message=status.message,
        origin=status.origin,
        stale=status.stale,
    )


def title_from_event(record: HookEvent) -> str | None:
    if record.provider == "codex":
        title = codex_session_title(record.session_id)
        if title:
            return title

    if record.event_name != "UserPromptSubmit":
        return None
    return summarize_prompt(record.raw.get("prompt"))


def codex_session_title(session_id: str | None) -> str | None:
    if not session_id:
        return None
    return codex_session_titles().get(session_id)


def codex_session_titles(path: Path | None = None) -> dict[str, str]:
    index_path = path or codex_session_index_path()
    try:
        stat = index_path.stat()
    except OSError:
        return {}

    with _codex_session_index_lock:
        global _codex_session_index_cache
        cached = _codex_session_index_cache
        if (
            cached is not None
            and cached.path == index_path
            and cached.mtime == stat.st_mtime
            and cached.size == stat.st_size
        ):
            return cached.titles

        titles: dict[str, str] = {}
        for line in read_recent_lines(index_path, CODEX_SESSION_INDEX_MAX_LINES):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue

            session_id = _string_or_none(row.get("id"))
            title = _string_or_none(row.get("thread_name"))
            if session_id and title:
                titles[session_id] = truncate_text(title.strip(), 72)

        _codex_session_index_cache = CachedCodexSessionIndex(
            path=index_path,
            mtime=stat.st_mtime,
            size=stat.st_size,
            titles=titles,
        )
        return titles


def codex_session_index_path() -> Path:
    return Path.home() / ".codex" / "session_index.jsonl"


def summarize_prompt(value: object, max_len: int = 72) -> str | None:
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text or text.startswith("<task-notification>"):
        return None

    marker = re.search(
        r"##\s+My request for [^:\n]+:\s*(.*)",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if marker:
        text = marker.group(1)

    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(
        r"(['\"])(?:~|/Users|/var|/private|/tmp)[^'\"]+\1",
        r"\1...\1",
        text,
    )
    text = re.sub(r"(?:~|/Users|/var|/private|/tmp)/[^\s,;)'\"`]+", "...", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"#+\s*", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" -:\n\t")

    if not text:
        return None
    return truncate_text(text, max_len)


def display_name_for_record(
    record: HookEvent,
    metadata: StatusMetadata,
    short_id: str,
    fallback: str,
) -> str:
    project = project_name(metadata.cwd or record.cwd)
    title = metadata.title

    return display_name_from_parts(project, title, short_id, fallback)


def display_name_from_parts(
    project: str | None,
    title: str | None,
    short_id: str,
    fallback: str,
) -> str:
    if project and title:
        if normalized_name_part(project) == normalized_name_part(title):
            return truncate_text(f"{title} ({short_id})", 96)
        return truncate_text(f"{project}: {title} ({short_id})", 96)
    if title:
        return truncate_text(f"{title} ({short_id})", 96)
    if project:
        return truncate_text(f"{project} ({short_id})", 96)
    return fallback


def normalized_name_part(text: str) -> str:
    return " ".join(text.replace("_", " ").replace("-", " ").split()).casefold()


def project_name(cwd: str | None) -> str | None:
    if not cwd:
        return None
    path = Path(cwd)
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return candidate.name or str(candidate)
    return path.name or cwd


def truncate_text(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    trimmed = text[: max_len - 1].rstrip()
    boundary = max(trimmed.rfind(" "), trimmed.rfind(","), trimmed.rfind(";"))
    if boundary >= max_len // 2:
        trimmed = trimmed[:boundary].rstrip()
    return f"{trimmed}..."


def _tool_response_looks_failed(response: object) -> bool:
    if isinstance(response, dict):
        if response.get("interrupted") is True:
            return True
        if response.get("success") is False:
            return True
        if response.get("exit_code") not in (None, 0):
            return True
        return False

    if isinstance(response, str):
        text = response.lower()
        return "exit code: 1" in text or "traceback" in text

    return False


def _assistant_message_asks_question(message: object) -> bool:
    if not isinstance(message, str):
        return False

    text = strip_markdown_inline_code(strip_markdown_code_blocks(message))
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return False

    for line in reversed(lines[-8:]):
        if _assistant_status_line(line):
            continue
        if _assistant_line_asks_question(line):
            return True

    return False


def _assistant_status_line(line: str) -> bool:
    text = line.strip().lower()
    return text.startswith(
        (
            "* cogitated ",
            "* recap:",
            "※ recap:",
            "recap:",
        )
    )


def _assistant_line_asks_question(line: str) -> bool:
    text = line.strip()
    if not text:
        return False

    lowered = text.lower()
    if text.endswith(":"):
        return False
    if _assistant_line_is_casual_closing_question(lowered):
        return False
    if re.search(
        r"(?:^|[.!?]\s+)(?:want me to|need me to|should i|should we|do you want me to)\b",
        lowered,
    ):
        return True

    required_question_prefixes = (
        "which ",
        "what ",
        "where ",
        "when ",
        "who ",
        "why ",
        "how ",
        "can you ",
        "could you ",
        "please confirm",
        "please choose",
        "choose ",
        "need me to ",
        "want me to ",
        "should i ",
        "should we ",
        "do you want me to ",
    )
    if text.endswith("?"):
        return lowered.startswith(required_question_prefixes)

    return lowered.startswith(
        (
            "please confirm",
            "please choose",
            "choose ",
            "need me to ",
            "want me to ",
            "should i ",
            "should we ",
            "do you want me to ",
        )
    )


def _assistant_line_is_casual_closing_question(lowered: str) -> bool:
    return lowered.startswith(
        (
            "anything else",
            "any other",
            "all good",
            "need anything else",
            "want anything else",
            "anything you want",
            "anything you'd like",
            "anything else you want",
            "anything else you'd like",
        )
    )
