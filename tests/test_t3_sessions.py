from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sidepulse import collector as collector_module
from sidepulse.hook import format_hook_payload
from sidepulse.models import AgentMode, AgentStatus
from sidepulse.origin import detect_agent_origin, t3_thread_link
from sidepulse.program_adapter import hook_report
from sidepulse.program_status import ProgramState
from sidepulse.providers import parse_log_line
from sidepulse.session_actions import (
    SESSION_OPEN_APP,
    SESSION_OPEN_T3,
    SESSION_OPEN_TERMINAL,
    available_session_open_actions,
    default_session_open_action,
    session_open_action_label,
    session_open_target,
)
from sidepulse.settings import AgentMonitorSettings

ENVIRONMENT_ID = "160d0258-04fe-40b1-97e4-4983535f4064"
THREAD_ID = "57c826bf-81ff-47cf-84a2-109a9a711dbe"
LINK = f"t3code://threads/{ENVIRONMENT_ID}/{THREAD_ID}"


def t3_home(tmp_path: Path) -> Path:
    userdata = tmp_path / "t3" / "userdata"
    userdata.mkdir(parents=True)
    (userdata / "environment-id").write_text(ENVIRONMENT_ID + "\n", encoding="utf-8")
    return tmp_path / "t3"


def t3_env(tmp_path: Path) -> dict[str, str]:
    return {
        "T3CODE_THREAD_ID": THREAD_ID,
        "T3CODE_HOME": str(t3_home(tmp_path)),
        "__CFBundleIdentifier": "com.t3tools.t3code",
    }


def status(**overrides) -> AgentStatus:
    values = dict(
        provider="claude",
        agent_id="claude:session:abc",
        display_name="Claude abc",
        mode=AgentMode.WAITING_FOR_INPUT,
        updated_at=datetime.now(timezone.utc),
        event_name="Notification",
        session_id="1ca4348e-2aec-4147-9e81-d7d56364d257",
        cwd="/tmp/project",
        origin="Claude in T3 Code",
        t3_link=LINK,
    )
    values.update(overrides)
    return AgentStatus(**values)


def test_t3_environment_is_detected_as_origin(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("sidepulse.origin.process_ancestry", lambda *_a, **_k: ())
    origin = detect_agent_origin("claude", env=t3_env(tmp_path))
    assert origin.label == "Claude in T3 Code"
    assert origin.kind == "claude_t3code"

    bundle_only = detect_agent_origin("codex", env={"__CFBundleIdentifier": "com.t3tools.t3code"})
    assert bundle_only.label == "Codex in T3 Code"


def test_t3_thread_link_needs_thread_and_environment(tmp_path) -> None:
    assert t3_thread_link(t3_env(tmp_path)) == LINK
    assert t3_thread_link({"T3CODE_HOME": str(t3_home(tmp_path / "other"))}) is None
    assert t3_thread_link({"T3CODE_THREAD_ID": THREAD_ID}, home=tmp_path / "missing") is None
    # Default data directory is ~/.t3 when T3CODE_HOME is not exported.
    home = tmp_path / "home"
    (home / ".t3" / "userdata").mkdir(parents=True)
    (home / ".t3" / "userdata" / "environment-id").write_text(ENVIRONMENT_ID, encoding="utf-8")
    assert t3_thread_link({"T3CODE_THREAD_ID": THREAD_ID}, home=home) == LINK


def test_hook_payload_carries_t3_link_into_status(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("sidepulse.origin.process_ancestry", lambda *_a, **_k: ())
    for key, value in t3_env(tmp_path).items():
        monkeypatch.setenv(key, value)
    line = format_hook_payload(
        "claude",
        '{"hook_event_name":"UserPromptSubmit","session_id":"claude-session","prompt":"hi"}',
    )
    assert line["agent_origin"] == "Claude in T3 Code"
    assert line["t3_link"] == LINK

    record = parse_log_line("claude", json.dumps(line))
    assert record is not None and record.t3_link == LINK
    built = collector_module.status_from_event(record)
    assert built is not None and built.t3_link == LINK
    assert collector_module.agent_status_from_dict(built.to_dict()).t3_link == LINK


def test_payload_with_existing_origin_still_gets_t3_link(tmp_path) -> None:
    from sidepulse.origin import annotate_payload_with_origin

    payload = annotate_payload_with_origin(
        "claude", {"agent_origin": "Custom"}, env=t3_env(tmp_path)
    )
    assert payload["agent_origin"] == "Custom"
    assert payload["t3_link"] == LINK


def test_parse_ignores_non_t3_links() -> None:
    line = {"hook_event_name": "Stop", "session_id": "s", "t3_link": "https://example.com"}
    record = parse_log_line("claude", json.dumps(line))
    assert record is not None and record.t3_link is None


def test_daemon_keeps_t3_link_for_local_sessions(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("sidepulse.origin.process_ancestry", lambda *_a, **_k: ())
    for key, value in t3_env(tmp_path).items():
        monkeypatch.setenv(key, value)
    line = format_hook_payload(
        "claude",
        '{"hook_event_name":"UserPromptSubmit","session_id":"claude-session","prompt":"hi"}',
    )
    report = hook_report("claude", line)
    assert report is not None and report["context"]["t3_link"] == LINK

    state = ProgramState()
    state.submit(report)
    # A later event without the link keeps the session's link.
    follow_up = {**report, "request_id": str(uuid.uuid4())}
    follow_up["context"] = {k: v for k, v in report["context"].items() if k != "t3_link"}
    state.submit(follow_up)
    statuses = state.statuses()
    assert [s.t3_link for s in statuses] == [LINK]


def test_t3_sessions_open_in_t3_by_default() -> None:
    t3 = status()
    assert default_session_open_action(t3) == SESSION_OPEN_T3
    assert session_open_target(t3, SESSION_OPEN_T3) == ("url", LINK)
    assert session_open_action_label(t3, SESSION_OPEN_T3) == "Open in T3 Code"
    assert available_session_open_actions(t3)[0] == SESSION_OPEN_T3

    without_link = status(t3_link=None)
    assert SESSION_OPEN_T3 not in available_session_open_actions(without_link)
    assert default_session_open_action(without_link) == SESSION_OPEN_TERMINAL


def test_provider_wide_preference_does_not_override_t3() -> None:
    settings = AgentMonitorSettings(session_open_preferences={"claude": SESSION_OPEN_APP})
    assert settings.session_open_action("claude", "Claude in T3 Code") is None
    assert settings.session_open_action("claude", "Claude Code CLI") == SESSION_OPEN_APP

    chosen = settings.with_session_open_action("claude", SESSION_OPEN_TERMINAL, "Claude in T3 Code")
    assert chosen.session_open_action("claude", "Claude in T3 Code") == SESSION_OPEN_TERMINAL
