from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path

from .text_utils import read_recent_lines, truncate_text

JUNIE_SESSION_INDEX_MAX_LINES = 5000


@dataclass(frozen=True)
class JunieSessionInfo:
    title: str | None
    project_dir: str | None


@dataclass(frozen=True)
class CachedJunieSessionIndex:
    path: Path
    mtime: int
    size: int
    sessions: dict[str, JunieSessionInfo]


_junie_session_index_cache: CachedJunieSessionIndex | None = None
_junie_session_index_lock = threading.RLock()


def junie_session_index(path: Path | None = None) -> dict[str, JunieSessionInfo]:
    index_path = path or junie_session_index_path()
    try:
        stat = index_path.stat()
    except OSError:
        return {}

    with _junie_session_index_lock:
        global _junie_session_index_cache
        cached = _junie_session_index_cache
        if (
            cached is not None
            and cached.path == index_path
            and cached.mtime == stat.st_mtime_ns
            and cached.size == stat.st_size
        ):
            return cached.sessions

        sessions: dict[str, JunieSessionInfo] = {}
        for line in read_recent_lines(index_path, JUNIE_SESSION_INDEX_MAX_LINES):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue

            session_id = _string_or_none(row.get("sessionId") or row.get("session_id"))
            if not session_id:
                continue
            title = _string_or_none(row.get("taskName") or row.get("task_name"))
            project_dir = _string_or_none(
                row.get("projectDir")
                or row.get("project_dir")
                or row.get("projectPath")
            )
            sessions[session_id] = JunieSessionInfo(
                title=truncate_text(title.strip(), 72)
                if title and title.strip()
                else None,
                project_dir=project_dir,
            )

        _junie_session_index_cache = CachedJunieSessionIndex(
            path=index_path,
            mtime=stat.st_mtime_ns,
            size=stat.st_size,
            sessions=sessions,
        )
        return sessions


def junie_session_title(session_id: str | None) -> str | None:
    if not session_id:
        return None
    session = junie_session_index().get(session_id)
    return session.title if session else None


def junie_session_project_dir(session_id: str | None) -> str | None:
    if not session_id:
        return None
    session = junie_session_index().get(session_id)
    return session.project_dir if session else None


def junie_session_index_path() -> Path:
    return Path.home() / ".junie" / "sessions" / "index.jsonl"


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
