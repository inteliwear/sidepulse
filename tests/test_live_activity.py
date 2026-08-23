from __future__ import annotations

import json
from datetime import datetime, timezone

from sidepulse.live_activity import (
    MAX_AGENT_ROWS,
    TokenStore,
    build_content_state,
)
from sidepulse.models import AgentMode, AgentStatus


def make_status(
    agent_id: str,
    mode: AgentMode,
    name: str = "",
    tool: str | None = None,
    session_id: str | None = None,
) -> AgentStatus:
    return AgentStatus(
        provider="claude",
        agent_id=agent_id,
        display_name=name or agent_id,
        mode=mode,
        updated_at=datetime.now(timezone.utc),
        event_name="PreToolUse",
        tool_name=tool,
        session_id=session_id,
    )


def test_content_state_orders_by_mode_priority_and_caps_rows():
    statuses = [make_status(f"work-{i}", AgentMode.WORKING) for i in range(MAX_AGENT_ROWS)]
    statuses.append(make_status("blocked", AgentMode.BLOCKED_ERROR))
    state = build_content_state(statuses, aggregate_mode="blocked_error")

    # activeCount counts non-terminal sessions; rows cap at MAX_AGENT_ROWS.
    assert state["activeCount"] == MAX_AGENT_ROWS + 1
    assert len(state["agents"]) == MAX_AGENT_ROWS
    assert state["agents"][0]["id"] == "blocked"
    assert state["aggregateMode"] == "blocked_error"


def test_content_state_appends_recent_finished_without_duplicates():
    working = make_status("a", AgentMode.WORKING, name="Running")
    finished = [
        {"id": "gone", "name": "Old Task", "mode": "completed", "detail": None,
         "provider": "claude", "cwd": "repo", "finishedAt": 1000.0},
        {"id": "a", "name": "Running", "mode": "completed", "detail": None,
         "provider": "claude", "cwd": "repo", "finishedAt": 2000.0},
    ]
    state = build_content_state([working], aggregate_mode="working", recent_finished=finished)

    assert state["activeCount"] == 1
    ids = [row["id"] for row in state["agents"]]
    # The active row wins; its stale finished entry is not duplicated.
    assert ids == ["a", "gone"]
    assert state["agents"][1]["mode"] == "completed"


def test_content_state_truncates_long_fields_and_serializes():
    status = make_status("a", AgentMode.TOOL_RUNNING, name="x" * 200, tool="y" * 200)
    state = build_content_state([status], aggregate_mode="tool_running")

    row = state["agents"][0]
    assert len(row["name"]) <= 45
    assert len(row["detail"]) <= 33
    # The whole payload must stay well under the 4 KB APNs content-state cap.
    assert len(json.dumps(state)) < 2000


def test_token_store_round_trip(tmp_path):
    store = TokenStore(tmp_path / "tokens.json")
    store.register("push_to_start", "aa11", {"device": "iPhone"})
    store.register("update", "bb22", {"device": "iPhone", "activity_id": "A1"})

    reloaded = TokenStore(tmp_path / "tokens.json")
    assert reloaded.tokens("push_to_start") == ["aa11"]
    assert reloaded.tokens("update") == ["bb22"]

    reloaded.drop("update", "bb22")
    assert reloaded.tokens("update") == []
    assert TokenStore(tmp_path / "tokens.json").tokens("update") == []


def test_compute_alerts_only_on_transition_with_cooldown():
    from sidepulse.live_activity import compute_alerts

    working = make_status("a", AgentMode.WORKING, name="Session A")
    waiting = make_status("a", AgentMode.WAITING_FOR_INPUT, name="Session A")
    last_alerts: dict[tuple[str, str], float] = {}

    # First tick after restart: seeds state, never alerts.
    alerts, modes = compute_alerts({}, [waiting], now=100.0, last_alerts=last_alerts)
    assert alerts == []

    # working -> waiting transitions and alerts once.
    alerts, modes = compute_alerts({"a": "working"}, [waiting], now=200.0, last_alerts=last_alerts)
    assert len(alerts) == 1
    assert "Needs your input" in alerts[0]["title"]
    assert "Session A" in alerts[0]["title"]

    # Flapping back within the cooldown stays silent.
    alerts, modes = compute_alerts(modes, [working], now=210.0, last_alerts=last_alerts)
    assert alerts == []
    alerts, modes = compute_alerts(modes, [waiting], now=220.0, last_alerts=last_alerts)
    assert alerts == []

    # After the cooldown the same transition alerts again.
    alerts, modes = compute_alerts({"a": "working"}, [waiting], now=400.0, last_alerts=last_alerts)
    assert len(alerts) == 1


def test_compute_alerts_completed_and_blocked():
    from sidepulse.live_activity import compute_alerts

    done = make_status("b", AgentMode.COMPLETED, name="Deploy")
    blocked = make_status("c", AgentMode.BLOCKED_ERROR, name="Tests", tool="pytest")
    alerts, _ = compute_alerts(
        {"b": "working", "c": "tool_running"}, [done, blocked], now=50.0, last_alerts={}
    )
    titles = sorted(alert["title"] for alert in alerts)
    assert any("Finished" in title for title in titles)
    assert any("Blocked" in title for title in titles)
    blocked_alert = next(alert for alert in alerts if "Blocked" in alert["title"])
    assert blocked_alert["body"] == "pytest"


def test_finished_waits_for_subagents():
    from sidepulse.live_activity import compute_alerts

    main_done = make_status(
        "claude:session:s1", AgentMode.COMPLETED, name="Big Task", session_id="s1"
    )
    subagent = make_status(
        "claude:agent:sub1", AgentMode.TOOL_RUNNING, name="Subtask", session_id="s1"
    )
    prev = {"claude:session:s1": "working", "claude:agent:sub1": "tool_running"}
    last_alerts: dict[tuple[str, str], float] = {}

    # Main session completed but a subagent still runs: no Finished alert.
    alerts, modes = compute_alerts(prev, [main_done, subagent], now=100.0, last_alerts=last_alerts)
    assert alerts == []

    # Subagent finishes too: exactly one Finished alert, named after the session.
    sub_done = make_status(
        "claude:agent:sub1", AgentMode.COMPLETED, name="Subtask", session_id="s1"
    )
    alerts, modes = compute_alerts(modes, [main_done, sub_done], now=200.0, last_alerts=last_alerts)
    assert len(alerts) == 1
    assert alerts[0]["title"] == "Finished: Big Task"


def test_ignored_cwd_sessions_are_filtered(monkeypatch):
    from sidepulse.collector import StatusMetadata, should_ignore_record
    from sidepulse.models import HookEvent

    def record(cwd):
        return HookEvent(
            provider="claude",
            logged_at=datetime.now(timezone.utc),
            event_name="PreToolUse",
            raw={},
            session_id="s1",
            cwd=cwd,
        )

    meta = StatusMetadata(cwd=None)
    assert should_ignore_record(record("/Users/x/Git/aura-server"), meta)
    assert should_ignore_record(record("/Users/x/.claude/memories"), meta)
    assert not should_ignore_record(record("/Users/x/Git/sidepulse"), meta)

    # Per-run subdirectories under an ignored dir are ignored too.
    assert should_ignore_record(
        record("/Users/x/Git/aura-server/runs/20260823-120000-routine-inbox"), meta
    )

    monkeypatch.setenv("SIDEPULSE_IGNORE_DIRS", "scratch")
    assert should_ignore_record(record("/tmp/scratch"), meta)


def test_ignored_display_name_prefix():
    from sidepulse.collector import is_ignored_display_name

    assert is_ignored_display_name("aura-server: You are an autonomous agent")
    assert is_ignored_display_name("memories: Memory Writing Agent")
    assert not is_ignored_display_name("sidepulse: Merge main")


def test_background_tasks_keep_session_visible(tmp_path):
    from sidepulse.live_activity import LiveActivityConfig, LiveActivityDaemon, TokenStore

    config = LiveActivityConfig(
        apns_key_path=tmp_path / "missing.p8",
        apns_key_id="X", apns_team_id="Y",
    )
    daemon = LiveActivityDaemon(config, token_store=TokenStore(tmp_path / "tokens.json"))
    daemon._bg_task_counter = lambda session_id: 2 if session_id == "s1" else 0

    done = make_status("claude:session:s1", AgentMode.COMPLETED, name="Deploy", session_id="s1")
    overlaid = daemon._overlay_background_tasks(done, now=100.0)
    assert overlaid.mode == AgentMode.LONG_TASK_PROGRESS
    assert "background task" in (overlaid.tool_name or "")

    quiet = make_status("claude:session:s2", AgentMode.COMPLETED, name="Idle", session_id="s2")
    assert daemon._overlay_background_tasks(quiet, now=100.0).mode == AgentMode.COMPLETED


def test_sync_background_tasks_emits_and_restores(tmp_path, monkeypatch):
    import json as _json

    from sidepulse.live_activity import LiveActivityConfig, LiveActivityDaemon, TokenStore

    # Redirect the claude log and the moonside runtime into the sandbox.
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr("sidepulse.live_activity.default_state_dir", lambda: state_dir)
    monkeypatch.setenv("MOONSIDE_RUNTIME_DIR", str(tmp_path))
    sessions = tmp_path / "moonside_sessions"
    sessions.mkdir()
    (sessions / "s1").write_text("idle\nStop\nturn\n/tmp/t.jsonl\n")

    config = LiveActivityConfig(apns_key_path=tmp_path / "k.p8", apns_key_id="X", apns_team_id="Y")
    daemon = LiveActivityDaemon(config, token_store=TokenStore(tmp_path / "tok.json"))
    counts = {"s1": 1}
    daemon._bg_task_counter = lambda sid: counts.get(sid, 0)

    done = make_status("claude:session:s1", AgentMode.COMPLETED, name="T", session_id="s1")
    daemon._sync_background_tasks([done], now=100.0)

    log = (state_dir / "claude.jsonl").read_text().splitlines()
    assert _json.loads(log[-1])["hook_event_name"] == "PreToolUse"
    assert _json.loads(log[-1])["tool_name"] == "BackgroundTask"
    assert (sessions / "s1").read_text().splitlines()[0] == "working"

    # Tasks finish: synthetic Stop + marker restored.
    counts["s1"] = 0
    daemon._bg_task_cache.clear()
    synthetic = make_status(
        "claude:session:s1", AgentMode.TOOL_RUNNING, name="T",
        session_id="s1", tool="BackgroundTask",
    )
    synthetic = type(synthetic)(**{**synthetic.__dict__, "event_name": "PreToolUse"})
    daemon._sync_background_tasks([synthetic], now=200.0)

    log = (state_dir / "claude.jsonl").read_text().splitlines()
    assert _json.loads(log[-1])["hook_event_name"] == "Stop"
    assert (sessions / "s1").read_text().splitlines()[0] == "idle"
    assert "s1" not in daemon._bg_injected
