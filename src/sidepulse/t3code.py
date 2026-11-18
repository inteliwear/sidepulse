"""Read recent T3 Code provider events and report status through the SidePulse daemon."""
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
from .program_adapter import clean_text, provider_report
from .program_status import MAX_SESSIONS, ProgramState, StatusError, hook_scope

MAX_FILE_AGE = 24 * 60 * 60
MAX_FILES = 16
MAX_TAIL_BYTES = 1024 * 1024
LOG_NAME = re.compile(r"events\.(.+)\.log(?:\.\d+)?")
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
    native: bool = False
    title: str | None = None


def parse_event(line: bytes, thread_from_file: str | None = None) -> ProviderEvent | None:
    prefix, marker, entry = line.partition(b"] ")
    stream, separator, body = entry.partition(b": ")
    if not marker or not separator or stream not in {b"CANON", b"NTIVE"} or not prefix.startswith(b"["):
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
    if stream == b"NTIVE":
        return parse_native_event(event, thread_from_file, date_ms)
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
    cwd = clean_cwd(config.get("cwd")) if name in {"session.configured", "thread.configured"} else None
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
    request_id = request_key(event.get("requestId")) or request_key(payload.get("requestId"))
    if state or native_id or cwd or tool:
        return ProviderEvent(provider, thread, date_ms, name, native_id, cwd, kind, state, tool, request_id)
    return None


def clean_cwd(value: object) -> str | None:
    return value if isinstance(value, str) and value == clean_text(value, 4096) else None


def request_key(value: object) -> str | None:
    if type(value) is int and abs(value) <= 9007199254740991:
        return str(value)
    return string(value)


CODEX_REQUESTS = {
    "item/commandExecution/requestApproval": ("request.opened", "permission"),
    "item/fileChange/requestApproval": ("request.opened", "permission"),
    "item/permissions/requestApproval": ("request.opened", "permission"),
    "item/tool/requestUserInput": ("user-input.requested", "question"),
    "mcpServer/elicitation/request": ("user-input.requested", "question"),
}
CODEX_TOOL_LABELS = {
    "commandExecution": "Shell command",
    "fileChange": "File change",
    "mcpToolCall": "MCP tool",
    "dynamicToolCall": "Tool call",
    "webSearch": "Web search",
    "imageView": "Image view",
}


def parse_native_event(event: dict, thread: str | None, date: int) -> ProviderEvent | None:
    if not string(thread) or thread == "_global" or event.get("kind") != "protocol":
        return None
    frame = object_dict(event.get("event"))
    if frame.get("stage") != "decoded":
        return None
    protocol = event.get("protocol")
    payload = object_dict(frame.get("payload"))
    direction = frame.get("direction")
    if protocol == "codex.app-server" and event.get("provider") == "codex":
        return codex_native_event(payload, direction, thread, date)
    if protocol == "claude-agent-sdk.query" and event.get("provider") == "claudeAgent":
        return claude_native_event(payload, direction, thread, date)
    return None


def codex_native_event(packet: dict, direction: object, thread: str, date: int) -> ProviderEvent | None:
    method = string(packet.get("method"))
    params = object_dict(packet.get("params"))
    native_thread = object_dict(params.get("thread"))
    native_id = string(params.get("threadId")) or string(native_thread.get("id"))
    cwd = clean_cwd(native_thread.get("cwd")) or clean_cwd(params.get("cwd"))
    title = string(native_thread.get("name"))
    if method == "thread/name/updated":
        title = string(params.get("threadName"))
    name, state, kind, tool, wait = "thread.configured", None, None, None, None
    if direction == "outgoing":
        if method == "turn/start":
            name, state = "turn.started", "working"
        elif method is None and ("result" in packet or "error" in packet):
            name, state, wait = "request.resolved", "working", request_key(packet.get("id"))
            if wait is None:
                return None
        elif method not in {"thread/start", "thread/resume"}:
            return None
    elif direction == "incoming":
        if method in CODEX_REQUESTS:
            name, kind = CODEX_REQUESTS[method]
            state, wait = "blocked", request_key(packet.get("id"))
        elif method == "serverRequest/resolved":
            name, state, wait = "request.resolved", "working", request_key(params.get("requestId"))
        elif method == "turn/started":
            name, state = "turn.started", "working"
        elif method == "turn/completed":
            name = "turn.completed"
            state = event_state(name, {"state": object_dict(params.get("turn")).get("status")})
        elif method == "thread/status/changed":
            name = "thread.state.changed"
            state = event_state(name, {"state": object_dict(params.get("status")).get("type")})
        elif method in {"item/started", "item/completed"}:
            item = object_dict(params.get("item"))
            label = CODEX_TOOL_LABELS.get(string(item.get("type")))
            if label is None:
                return None
            name = "item.started" if method == "item/started" else "item.completed"
            tool = label if name == "item.started" else None
            state = event_state(name, item)
        elif method in {"error", "thread/realtime/error"} and not params.get("willRetry"):
            name, state = "runtime.error", "error"
        elif method != "thread/started" and native_id is None:
            return None
    if state or native_id or cwd or title:
        return ProviderEvent("codex", thread, date, name, native_id, cwd, kind, state, tool, wait,
                             native=True, title=title)
    return None


def claude_native_event(packet: dict, direction: object, thread: str, date: int) -> ProviderEvent | None:
    message_type = packet.get("type")
    native_id = string(packet.get("session_id"))
    cwd = None
    title = None
    name, state, tool, kind, request_id = "thread.configured", None, None, None, None
    if direction == "outgoing" and message_type == "query.open":
        cwd = clean_cwd(object_dict(packet.get("options")).get("cwd"))
    elif direction == "outgoing" and message_type == "prompt.offer":
        name, state = "turn.started", "working"
    elif direction == "incoming" and message_type == "system":
        subtype = packet.get("subtype")
        if subtype == "init":
            cwd = clean_cwd(packet.get("cwd"))
        elif subtype == "session_title_changed":
            title = string(packet.get("title"))
    elif direction == "incoming" and message_type == "assistant":
        content = object_dict(packet.get("message")).get("content")
        for block in content if isinstance(content, list) else ():
            block = object_dict(block)
            if block.get("type") == "tool_use":
                tool = string(block.get("name")) or "Tool"
                name, state = "item.started", "working"
                if tool == "AskUserQuestion":
                    name, state, kind = "user-input.requested", "blocked", "question"
                    request_id = string(block.get("id"))
                break
    elif direction == "incoming" and message_type == "user" and "tool_use_result" in packet:
        content = object_dict(packet.get("message")).get("content")
        for block in content if isinstance(content, list) else ():
            block = object_dict(block)
            if block.get("type") == "tool_result":
                request_id = string(block.get("tool_use_id"))
                break
        name, state = "item.completed", "working"
    elif direction == "incoming" and message_type == "result":
        name = "turn.completed"
        state = "error" if packet.get("is_error") is True else "done"
    if state or native_id or cwd or title:
        return ProviderEvent("claude", thread, date, name, native_id, cwd, kind, state, tool, request_id,
                             native=True, title=title)
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
    title: str | None = None
    pending: dict[str, str] = field(default_factory=dict)

    def apply(self, event: ProviderEvent) -> None:
        self.session_id = event.native_id or self.session_id
        self.cwd = event.cwd or self.cwd
        self.title = event.title or self.title
        if event.state is None:
            return
        if event.state in TERMINAL_STATES or event.event == "turn.started":
            self.pending.clear()
        if event.event in REQUESTS:
            self.pending[event.request_id or event.kind] = event.kind
        elif event.event in RESOLUTIONS:
            resolved = self.pending.pop(event.request_id or event.kind, None)
            if event.native and resolved is None:
                return
        elif event.provider == "claude" and event.native and event.event == "item.completed":
            self.pending.pop(event.request_id, None)
        self.event = event

    def report(self, environment_id: str | None) -> dict:
        event = self.event
        state = "blocked" if self.pending else event.state
        kind = next(iter(self.pending.values()), None)
        scope = hook_scope(event.provider, self.session_id or event.thread)
        origin = f"{provider_label(event.provider)} in T3 Code"
        mode = "tool_running" if event.tool else None
        context = {"session_id": self.session_id, "cwd": self.cwd,
                   "origin": origin, "display_name": origin, "event_name": event.event,
                   "tool_name": event.tool if state == "working" else None,
                   "mode": mode, "t3_link": t3_link_for_thread(environment_id, event.thread),
                   "title": self.title, "title_source": "session" if self.title else None}
        status = {"state": state}
        if kind:
            status["kind"] = kind
        return provider_report(event.provider, scope, status, context)


def read_events(path: Path) -> Iterator[ProviderEvent]:
    match = LOG_NAME.fullmatch(path.name)
    thread = match[1] if match else None
    with path.open("rb") as handle:
        end = handle.seek(0, os.SEEK_END)
        start = max(0, end - MAX_TAIL_BYTES)
        handle.seek(start)
        data = handle.read(end - start)
    lines = data.split(b"\n")
    # The first line may be truncated, and a writer may still be appending the last.
    for line in lines[bool(start):-1]:
        event = parse_event(line, thread)
        if event:
            yield event


def log_directory(base: Path) -> Path:
    return base / "userdata" / "logs" / "provider"


def monitoring_status(base: Path | None = None, *, files_read: int | None = None) -> dict:
    base = t3_data_dir(os.environ) if base is None else base
    root = log_directory(base)
    try:
        exists = root.is_dir()
    except OSError:
        exists = False
    return {
        "log_directory": str(root),
        "log_directory_exists": exists,
        "files_read": files_read,
        "thread_links_available": bool(t3_environment_id({"T3CODE_HOME": str(base)})),
    }


def monitoring_status_label(status: dict) -> str:
    files_read = status.get("files_read")
    if files_read is None:
        return "Automatic · Service status unavailable"
    if not status.get("log_directory_exists"):
        return "Automatic · Waiting for provider logs"
    if not files_read:
        return "Automatic · Waiting for recent events"
    return "Automatic · Reading provider logs"


class T3CodeSource:
    def __init__(self, state: ProgramState, *, env: Mapping[str, str] | None = None):
        self.state = state
        base = t3_data_dir(os.environ if env is None else env)
        self.root = log_directory(base)
        self.home = base
        self.files: dict[Path, tuple[tuple[int, int, int], list[ProviderEvent]]] = {}
        self.reported: dict[tuple[str, str], tuple[int, dict]] = {}
        self.environment_id: str | None = None

    def diagnostics(self) -> dict:
        files = self.files.copy()
        return monitoring_status(
            self.home,
            files_read=sum(any(event.state for event in events) for _, events in files.values()),
        )

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
        streams: dict[tuple[str, str, bool], ThreadStatus] = {}
        events = sorted((event for _, records in self.files.values() for event in records),
                        key=lambda event: event.date)
        for event in events:
            if cutoff_ms <= event.date <= int(now * 1000):
                streams.setdefault((event.provider, event.thread, event.native), ThreadStatus()).apply(event)
        threads: dict[tuple[str, str], ThreadStatus] = {}
        for (provider, thread, native), status in streams.items():
            if status.event is None or (native and status.session_id is None):
                continue
            key = provider, thread
            current = threads.get(key)
            if current is None or (status.event.date, not native) > (current.event.date, not current.event.native):
                threads[key] = status

        recent = sorted(threads.items(),
                        key=lambda row: row[1].event.date)[-MAX_SESSIONS:]
        for key, status in recent:
            date = status.event.date
            previous = self.reported.get(key)
            if previous and not status.session_id:
                status.session_id = previous[1]["context"].get("session_id")
            if previous and not status.cwd:
                status.cwd = previous[1]["context"].get("cwd")
            if previous and not status.title:
                status.title = previous[1]["context"].get("title")
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
