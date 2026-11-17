"""Read recent canonical T3 Code provider events and report their local status."""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterator, Mapping

from .models import provider_label
from .origin import t3_data_dir, t3_environment_id, t3_link_for_thread
from .program_adapter import clean_text
from .program_status import MAX_SESSIONS, ProgramState, StatusError, hook_scope

MAX_FILE_AGE = 24 * 60 * 60
MAX_FILES = 16
MAX_TAIL_BYTES = 1024 * 1024
LOG_NAME = re.compile(r"events\..+\.log(?:\.\d+)?")
PROVIDER_NAME = re.compile(r"[a-zA-Z0-9_.+-]{1,32}")
TOOL_TYPES = ("command", "tool", "file_change", "web_search", "image_view")
FAILED_STATES = {"failed", "error", "errored"}
REQUESTS = {"request.opened": "permission", "user-input.requested": "question"}
RESOLUTIONS = {"request.resolved": "permission", "user-input.resolved": "question"}
TERMINAL_STATES = {"done", "idle", "error", "clear"}


def string(value: object) -> str | None:
    if isinstance(value, str) and value and len(value) <= 512:
        if all(32 <= ord(char) < 127 or ord(char) >= 160 for char in value):
            try:
                if len(value.encode()) <= 512:
                    return value
            except UnicodeError:
                pass
    return None


def object_dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


@dataclass(frozen=True)
class ProviderEvent:
    provider: str
    thread: str
    date: int
    event: str
    native_id: str | None
    cwd: str | None
    kind: str | None
    state: str | None
    tool: str | None
    request_id: str | None


def parse_event(line: bytes) -> ProviderEvent | None:
    prefix, marker, body = line.partition(b"] CANON: ")
    if not marker or not prefix.startswith(b"["):
        return None
    try:
        event = json.loads(body)
        if not isinstance(event, dict):
            return None
        date = event.get("createdAt") or prefix[1:].decode()
        timestamp = datetime.fromisoformat(date.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            return None
        date_ms = int(timestamp.timestamp() * 1000)
    except (ValueError, AttributeError, OSError, OverflowError, RecursionError):
        return None
    provider, thread, name = map(string, (event.get("provider"), event.get("threadId"), event.get("type")))
    if not provider or not PROVIDER_NAME.fullmatch(provider) or not thread or not name:
        return None
    provider = {"claudeAgent": "claude", "openCode": "opencode"}.get(provider, provider.lower())
    payload = object_dict(event.get("payload"))
    raw = object_dict(object_dict(event.get("raw")).get("payload"))
    native_id = next((value for data in (payload, raw, object_dict(payload.get("detail")))
                      for key in ("providerThreadId", "session_id", "sessionId", "conversation_id",
                                  "conversationId", "threadId")
                      if (value := string(data.get(key))) and value != thread), None)
    config = object_dict(payload.get("config"))
    cwd = config.get("cwd") if name in {"session.configured", "thread.configured"} else None
    cwd = cwd if isinstance(cwd, str) and cwd == clean_text(cwd, 4096) else None
    tool_type = (string(payload.get("itemType")) or "").lower()
    tool = None
    if name in {"item.started", "task.started"} and (
        name == "task.started" or any(word in tool_type for word in TOOL_TYPES)
    ):
        data = object_dict(payload.get("data"))
        # Only use labels. Command text and output may contain private data.
        tool = string(data.get("toolName")) or string(data.get("tool_name")) or string(payload.get("title"))
        tool = tool or tool_type or "Tool"
    state = event_state(name, payload)
    kind = REQUESTS.get(name) or RESOLUTIONS.get(name)
    if kind and payload.get("kind") == "user-input":
        kind = "question"
    request_id = string(event.get("requestId")) or string(payload.get("requestId"))
    if state or native_id or cwd or tool:
        return ProviderEvent(provider, thread, date_ms, name, native_id, cwd, kind, state, tool, request_id)
    return None


def event_state(name: str, payload: dict) -> str | None:
    state = (string(payload.get("state")) or "").lower()
    if name in {"turn.completed", "turn.aborted"}:
        return "error" if state in FAILED_STATES else "done"
    if name == "session.exited":
        return "clear"
    if name in {"runtime.error", "thread.realtime.error"}:
        return "error"
    if name in REQUESTS:
        return "blocked"
    if name in RESOLUTIONS or name in {"turn.started", "task.started", "item.started"}:
        return "working"
    if name in {"item.completed", "task.completed"}:
        status = (string(payload.get("status")) or "").lower()
        return "error" if status in FAILED_STATES else "working"
    if name in {"thread.state.changed", "session.state.changed"}:
        if state in {"idle", "ready"}:
            return "done" if state == "idle" else "idle"
        if state in FAILED_STATES:
            return "error"
        if state in {"busy", "running", "active"}:
            return "working"
        if state in {"closed", "exited"}:
            return "clear"
    return None


@dataclass
class ThreadStatus:
    event: ProviderEvent | None = None
    session_id: str | None = None
    cwd: str | None = None
    pending: dict[str, str] = field(default_factory=dict)

    def apply(self, event: ProviderEvent) -> None:
        self.session_id = event.native_id or self.session_id
        self.cwd = event.cwd or self.cwd
        if event.state is None:
            return
        if event.state in TERMINAL_STATES or event.event == "turn.started":
            self.pending.clear()
        if event.event in REQUESTS:
            self.pending[event.request_id or event.kind] = event.kind
        elif event.event in RESOLUTIONS:
            self.pending.pop(event.request_id or event.kind, None)
        self.event = event

    def report(self, environment_id: str | None) -> dict:
        event = self.event
        state = "blocked" if self.pending else event.state
        kind = next(iter(self.pending.values()), None)
        scope = hook_scope(event.provider, self.session_id or event.thread)
        origin = f"{provider_label(event.provider)} in T3 Code"
        mode = "tool_running" if event.tool else None
        context = {"provider": event.provider, "session_id": self.session_id, "cwd": self.cwd,
                   "origin": origin, "display_name": origin, "event_name": event.event,
                   "tool_name": event.tool if state == "working" else None,
                   "mode": mode, "t3_link": t3_link_for_thread(environment_id, event.thread)}
        app = "claude-code" if event.provider == "claude" else event.provider
        return {"v": 1, "type": "program_status", "scope": scope,
                "status": {"state": state, "app": app, **({"kind": kind} if kind else {})},
                "context": {key: value for key, value in context.items() if value is not None}}


def read_events(path: Path) -> Iterator[ProviderEvent]:
    with path.open("rb") as handle:
        end = handle.seek(0, os.SEEK_END)
        start = max(0, end - MAX_TAIL_BYTES)
        handle.seek(start)
        data = handle.read(end - start)
    lines = data.split(b"\n")
    # The first line may be truncated, and a writer may still be appending the last.
    for line in lines[bool(start):-1]:
        event = parse_event(line)
        if event:
            yield event


class T3CodeSource:
    def __init__(self, state: ProgramState, *, env: Mapping[str, str] | None = None):
        self.state = state
        base = t3_data_dir(os.environ if env is None else env)
        self.root = base / "userdata" / "logs" / "provider"
        self.home = base
        self.files: dict[Path, tuple[tuple[int, int, int], list[ProviderEvent]]] = {}
        self.reported: dict[tuple[str, str], tuple[int, dict]] = {}
        self.environment_id: str | None = None

    def poll(self, *, now: float | None = None) -> None:
        now = time.time() if now is None else now
        cutoff_ms = int((now - MAX_FILE_AGE) * 1000)
        files = self.recent_files(now)
        unchanged = set(files) == set(self.files)
        for path, signature in files.items():
            if path in self.files and signature == self.files[path][0]:
                continue
            try:
                self.files[path] = signature, list(read_events(path))
                unchanged = False
            except OSError:
                continue
        self.files = {path: cached for path, cached in self.files.items() if path in files}

        for key, (date, report) in list(self.reported.items()):
            if date < cutoff_ms:
                self.state.submit({**report, "status": {"state": "clear"}}, observed_at=date)
                del self.reported[key]
        if not self.files:
            return
        environment_id = t3_environment_id({"T3CODE_HOME": str(self.home)})
        if unchanged and self.environment_id == environment_id:
            return
        self.environment_id = environment_id
        threads: dict[tuple[str, str], ThreadStatus] = {}
        events = sorted((event for _, records in self.files.values() for event in records),
                        key=lambda event: event.date)
        for event in events:
            if cutoff_ms <= event.date <= int(now * 1000):
                threads.setdefault((event.provider, event.thread), ThreadStatus()).apply(event)

        recent = sorted(((key, status) for key, status in threads.items() if status.event),
                        key=lambda row: row[1].event.date)[-MAX_SESSIONS:]
        for key, status in recent:
            date = status.event.date
            previous = self.reported.get(key)
            if previous and not status.session_id:
                status.session_id = previous[1]["context"].get("session_id")
            if previous and not status.cwd:
                status.cwd = previous[1]["context"].get("cwd")
            report = status.report(self.environment_id)
            if previous and (date < previous[0] or previous == (date, report)):
                continue
            try:
                self.state.submit(report, observed_at=date)
            except StatusError:
                continue
            self.reported.pop(key, None)
            self.reported[key] = date, report
        while len(self.reported) > MAX_SESSIONS:
            del self.reported[next(iter(self.reported))]

    def recent_files(self, now: float) -> dict[Path, tuple[int, int, int]]:
        files = {}
        for path in self.root.glob("events.*.log*"):
            if not LOG_NAME.fullmatch(path.name) or not path.is_file():
                continue
            try:
                info = path.stat()
            except OSError:
                continue
            if info.st_mtime >= now - MAX_FILE_AGE:
                files[path] = info.st_ino, info.st_mtime_ns, info.st_size
        return dict(sorted(files.items(), key=lambda row: row[1][1])[-MAX_FILES:])
