from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class AgentMode(str, Enum):
    IDLE_READY = "idle_ready"
    WORKING = "working"
    TOOL_RUNNING = "tool_running"
    WAITING_FOR_INPUT = "waiting_for_input"
    LONG_TASK_PROGRESS = "long_task_progress"
    BLOCKED_ERROR = "blocked_error"
    COMPLETED = "completed"
    UNKNOWN = "unknown"


MODE_PRIORITY: dict[AgentMode, int] = {
    AgentMode.BLOCKED_ERROR: 1,
    AgentMode.WAITING_FOR_INPUT: 2,
    AgentMode.TOOL_RUNNING: 3,
    AgentMode.LONG_TASK_PROGRESS: 4,
    AgentMode.WORKING: 5,
    AgentMode.COMPLETED: 6,
    AgentMode.IDLE_READY: 7,
    AgentMode.UNKNOWN: 99,
}


MODE_LABELS: dict[AgentMode, str] = {
    AgentMode.IDLE_READY: "Idle / Ready",
    AgentMode.WORKING: "Working",
    AgentMode.TOOL_RUNNING: "Tool Running",
    AgentMode.WAITING_FOR_INPUT: "Waiting for Input",
    AgentMode.LONG_TASK_PROGRESS: "Long Task Progress",
    AgentMode.BLOCKED_ERROR: "Blocked / Error",
    AgentMode.COMPLETED: "Completed",
    AgentMode.UNKNOWN: "Unknown",
}


@dataclass(frozen=True)
class HookEvent:
    provider: str
    logged_at: datetime
    event_name: str
    raw: dict[str, Any]
    session_id: str | None = None
    turn_id: str | None = None
    agent_id: str | None = None
    cwd: str | None = None
    tool_name: str | None = None
    message: str | None = None
    origin: str | None = None

    @property
    def status_key(self) -> str:
        if self.agent_id:
            return f"{self.provider}:agent:{self.agent_id}"
        if self.session_id:
            return f"{self.provider}:session:{self.session_id}"
        return f"{self.provider}:unknown"


@dataclass(frozen=True)
class AgentStatus:
    provider: str
    agent_id: str
    display_name: str
    mode: AgentMode
    updated_at: datetime
    event_name: str
    session_id: str | None = None
    cwd: str | None = None
    tool_name: str | None = None
    message: str | None = None
    origin: str | None = None
    stale: bool = False

    @property
    def priority(self) -> int:
        return MODE_PRIORITY.get(self.mode, MODE_PRIORITY[AgentMode.UNKNOWN])

    @property
    def mode_label(self) -> str:
        return MODE_LABELS.get(self.mode, self.mode.value)

    def age_seconds(self, now: datetime | None = None) -> float:
        current = now or datetime.now(timezone.utc)
        return max(0.0, (current - self.updated_at).total_seconds())

    def to_dict(self, now: datetime | None = None) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "agent_id": self.agent_id,
            "display_name": self.display_name,
            "mode": self.mode.value,
            "mode_label": self.mode_label,
            "priority": self.priority,
            "updated_at": self.updated_at.isoformat(),
            "age_seconds": round(self.age_seconds(now), 3),
            "event_name": self.event_name,
            "session_id": self.session_id,
            "cwd": self.cwd,
            "tool_name": self.tool_name,
            "message": self.message,
            "origin": self.origin,
            "stale": self.stale,
        }


@dataclass(frozen=True)
class AggregateStatus:
    mode: AgentMode
    active_count: int
    stale_count: int
    representative: AgentStatus | None = None

    @property
    def mode_label(self) -> str:
        return MODE_LABELS.get(self.mode, self.mode.value)

    def to_dict(self, now: datetime | None = None) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "mode_label": self.mode_label,
            "active_count": self.active_count,
            "stale_count": self.stale_count,
            "representative": (
                self.representative.to_dict(now) if self.representative else None
            ),
        }


def parse_datetime(value: Any, fallback: datetime | None = None) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        text = value.strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            parsed = fallback or datetime.now(timezone.utc)
    else:
        parsed = fallback or datetime.now(timezone.utc)

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def provider_label(provider: str) -> str:
    return {
        "codex": "Codex",
        "claude": "Claude",
        "grok": "Grok",
        # Lowercase is the product's own spelling, and provider.title() would break it.
        "opencode": "opencode",
    }.get(provider, provider.title())
