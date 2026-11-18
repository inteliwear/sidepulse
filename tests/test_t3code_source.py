from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sidepulse.models import AgentMode
from sidepulse.program_adapter import hook_report
from sidepulse.program_presentation import DaemonMonitor
from sidepulse.program_status import ProgramState, bridge_update, phone_update
from sidepulse.program_transport import ProgramServer, daemon_command
from sidepulse.t3code import MAX_FILE_AGE, T3CodeSource


@pytest.fixture
def source(tmp_path):
    home = tmp_path / "t3"
    state = ProgramState(publisher="test-sidepulse")
    t3 = T3CodeSource(state, env={"T3CODE_HOME": str(home)})
    t3.root.mkdir(parents=True)
    (home / "userdata" / "environment-id").write_text("environment\n")
    return t3, t3.root / "events.thread.log"


def line(at, event, payload=None, *, thread="thread", provider="claudeAgent", **fields):
    date = datetime.fromtimestamp(at, timezone.utc).isoformat()
    data = {"createdAt": date, "provider": provider, "threadId": thread,
            "type": event, "payload": payload or {}, **fields}
    return f"[{date}] CANON: {json.dumps(data)}\n"


def add(path, lines):
    with path.open("a", encoding="utf-8") as file:
        file.write("".join(lines))


def test_canonical_lifecycle_without_hooks(source):
    t3, log = source
    now = time.time()
    add(log, [line(now - 9, "session.configured", {"config": {"cwd": "/work/project"}}),
              line(now - 8, "hook.started", raw={"payload": {"session_id": "native"}}),
              line(now - 7, "turn.started"),
              line(now - 6, "item.started", {"itemType": "command_execution", "title": "Command run",
                                             "data": {"command": "sensitive-command"}})])
    t3.poll(now=now)
    row, = t3.state.statuses()
    assert (row.provider, row.session_id, row.cwd) == ("claude", "native", "/work/project")
    assert (row.mode, row.tool_name) == (AgentMode.TOOL_RUNNING, "Command run")
    assert row.t3_link == "t3code://threads/environment/thread"
    first_update = t3.state.pending_events("test")[0][1]
    assert "sensitive-command" not in json.dumps(first_update)
    assert "t3_link" not in json.dumps(bridge_update(first_update))
    assert "t3_link" not in json.dumps(phone_update(first_update))

    add(log, [line(now - 5, "request.opened", requestId="first"),
              line(now - 4, "user-input.requested", requestId="second"),
              line(now - 3, "request.resolved", requestId="first"),
              line(now - 2, "item.completed", {"itemType": "command_execution"})])
    t3.poll(now=now)
    row, = t3.state.statuses()
    assert row.mode == AgentMode.WAITING_FOR_INPUT
    number, update = t3.state.pending_events("test")[0]
    assert update["status"]["kind"] == "question"
    t3.poll(now=now)
    assert t3.state.pending_events("test")[0][0] == number

    completed = line(now - 1, "thread.state.changed", {"state": "idle"})
    add(log, [completed[:-1]])
    t3.poll(now=now)
    assert t3.state.statuses()[0].mode == AgentMode.WAITING_FOR_INPUT
    add(log, ["\n"])
    t3.poll(now=now)
    assert t3.state.statuses()[0].mode == AgentMode.COMPLETED
    t3.poll(now=now + MAX_FILE_AGE)
    assert not t3.state.statuses()


def test_native_hook_and_delayed_log_use_one_daemon_session(source):
    t3, log = source
    now = time.time()
    add(log, [line(now - 3, "thread.started", {"providerThreadId": "native"}),
              line(now - 2, "turn.completed")])
    with tempfile.TemporaryDirectory(prefix="sp-t3-", dir="/tmp" if os.name != "nt" else None) as tmp:
        socket = Path(tmp) / "t3.sock"
        server = ProgramServer(t3.state, path=socket)
        server.start()
        try:
            report = hook_report("claude", {"hook_event_name": "UserPromptSubmit", "session_id": "native",
                                          "agent_origin": "Claude Code CLI", "cwd": "/tmp/work"})
            result = daemon_command(report, path=socket)
            assert result["ok"]
            t3.poll(now=now)
            row, = DaemonMonitor(path=socket).snapshot().statuses
            assert row.mode == AgentMode.WORKING
            assert row.cwd == "/tmp/work"
            assert row.origin == "Claude in T3 Code"
            assert row.t3_link == "t3code://threads/environment/thread"
        finally:
            server.stop()


def test_thread_link_matches_hook_before_native_id_is_logged(source):
    t3, log = source
    now = time.time()
    add(log, [line(now - 3, "turn.started", thread="one"),
              line(now - 2, "turn.started", thread="two")])
    t3.poll(now=now)
    report = hook_report("claude", {"hook_event_name": "Stop", "session_id": "native",
                                  "agent_origin": "Claude in T3 Code",
                                  "t3_link": "t3code://threads/environment/one"})
    t3.state.submit(report)
    rows = {row.t3_link.rsplit("/", 1)[-1]: row for row in t3.state.statuses()}
    assert len(t3.state.statuses()) == 2
    assert rows["one"].mode == AgentMode.COMPLETED
    assert rows["one"].session_id == "native"
    assert rows["two"].mode == AgentMode.WORKING

    next_hook = hook_report("claude", {"hook_event_name": "UserPromptSubmit", "session_id": "native"})
    t3.state.submit(next_hook)
    rows = {row.t3_link.rsplit("/", 1)[-1]: row for row in t3.state.statuses()}
    assert len(rows) == 2
    assert rows["one"].mode == AgentMode.WORKING
    assert rows["one"].origin == "Claude in T3 Code"


def test_log_rotation_and_old_events(source):
    t3, log = source
    now = time.time()
    add(log, [line(now - 10, "turn.started", provider="codex"),
              line(now - MAX_FILE_AGE - 1, "turn.started", thread="stale"),
              "broken log record\n"])
    t3.poll(now=now)
    row, = t3.state.statuses()
    log.rename(log.with_suffix(".log.1"))
    add(log, [line(now - 5, "turn.completed", {"state": "failed"}, provider="codex")])
    os.utime(log, (now, now))
    t3.poll(now=now)
    failed, = t3.state.statuses()
    assert failed.agent_id == row.agent_id
    assert failed.mode == AgentMode.BLOCKED_ERROR


def native_line(at, provider, direction, payload):
    date = datetime.fromtimestamp(at, timezone.utc).isoformat()
    protocol = {"codex": "codex.app-server", "claudeAgent": "claude-agent-sdk.query"}[provider]
    packet = {"provider": provider, "protocol": protocol, "kind": "protocol",
              "providerSessionId": "t3-internal-session",
              "event": {"stage": "decoded", "direction": direction, "payload": payload}}
    return f"[{date}] NTIVE: {json.dumps(packet)}\n"


def test_modern_codex_log_tracks_native_id_and_approval(source):
    t3, log = source
    now = time.time()
    add(log, [
        native_line(now - 8, "codex", "incoming", {
            "method": "thread/started", "params": {"thread": {"id": "codex-native", "cwd": "/work/project"}},
        }),
        native_line(now - 7, "codex", "incoming", {
            "method": "turn/started", "params": {"threadId": "codex-native"},
        }),
        native_line(now - 6, "codex", "incoming", {
            "method": "item/started", "params": {
                "threadId": "codex-native", "item": {"type": "commandExecution", "command": "private-command"},
            },
        }),
    ])
    t3.poll(now=now)
    row, = t3.state.statuses()
    assert (row.session_id, row.mode, row.tool_name) == ("codex-native", AgentMode.TOOL_RUNNING, "Shell command")
    assert row.t3_link == "t3code://threads/environment/thread"
    assert "private-command" not in json.dumps(t3.state.pending_events("test"))

    add(log, [native_line(now - 5, "codex", "incoming", {
        "method": "item/commandExecution/requestApproval", "id": 12, "params": {"threadId": "codex-native"},
    })])
    t3.poll(now=now)
    assert t3.state.statuses()[0].mode == AgentMode.WAITING_FOR_INPUT

    add(log, [native_line(now - 4, "codex", "outgoing", {"id": 98, "result": {}})])
    t3.poll(now=now)
    assert t3.state.statuses()[0].mode == AgentMode.WAITING_FOR_INPUT

    add(log, [native_line(now - 3, "codex", "outgoing", {"id": 12, "result": {"decision": "accept"}})])
    t3.poll(now=now)
    assert t3.state.statuses()[0].mode == AgentMode.WORKING

    add(log, [native_line(now - 2, "codex", "incoming", {
        "method": "turn/completed", "params": {"threadId": "codex-native", "turn": {"status": "completed"}},
    }), native_line(now - 1, "codex", "incoming", {
        "method": "serverRequest/resolved", "params": {"threadId": "codex-native", "requestId": 12},
    })])
    t3.poll(now=now)
    final, = t3.state.statuses()
    assert final.agent_id == row.agent_id
    assert final.mode == AgentMode.COMPLETED
    assert final.cwd == "/work/project"


def test_modern_claude_log_counts_one_live_hook_session(source):
    t3, log = source
    now = time.time()
    hook = hook_report("claude", {"hook_event_name": "UserPromptSubmit", "session_id": "claude-native"})
    t3.state.submit(hook)
    add(log, [
        native_line(now - 8, "claudeAgent", "outgoing", {"type": "query.open", "options": {"cwd": "/project"}}),
        native_line(now - 7, "claudeAgent", "outgoing", {"type": "prompt.offer", "message": "private-prompt"}),
        native_line(now - 6, "claudeAgent", "incoming", {"type": "system", "subtype": "init",
                                                       "session_id": "claude-native", "cwd": "/project"}),
    ])
    t3.poll(now=now)
    row, = t3.state.statuses()
    assert (row.session_id, row.t3_link) == ("claude-native", "t3code://threads/environment/thread")
    assert row.mode == AgentMode.WORKING
    assert "private-prompt" not in json.dumps(t3.state.pending_events("test"))

    later = time.time() + 1
    add(log, [native_line(later, "claudeAgent", "incoming", {
        "type": "result", "subtype": "success", "session_id": "claude-native", "is_error": False,
    })])
    t3.poll(now=later + 1)
    final, = t3.state.statuses()
    assert final.agent_id == row.agent_id
    assert final.mode == AgentMode.COMPLETED


def test_t3_codex_and_hook_updates_use_the_same_phone_and_bridge_states(source):
    t3, log = source
    hooks = ProgramState(publisher="hook-sidepulse")
    now = time.time()
    add(log, [line(now - 8, "thread.started", {"providerThreadId": "native"}, provider="codex")])

    transitions = (
        ("turn.started", "UserPromptSubmit"),
        ("request.opened", "PermissionRequest"),
        ("turn.completed", "Stop"),
    )
    for offset, (event, hook_event) in enumerate(transitions):
        add(log, [line(now - 7 + offset, event, provider="codex")])
        t3.poll(now=now)
        hooks.submit(hook_report("codex", {"hook_event_name": hook_event, "session_id": "native"}))
        _, t3_update = t3.state.pending_events("test")[-1]
        _, hook_update = hooks.pending_events("test")[-1]
        assert t3_update["scope"] == hook_update["scope"]
        assert t3.state.statuses()[0].mode == hooks.statuses()[0].mode
        for encode in (bridge_update, phone_update):
            output = encode(t3_update)
            expected = encode(hook_update)
            # Hook permission requests may include a tool invocation ID.
            assert {k: v for k, v in output["status"].items() if k != "id"} == {
                k: v for k, v in expected["status"].items() if k != "id"
            }
            assert output.get("context") == expected.get("context")


def test_claude_question_and_title_are_reported_from_native_logs(source):
    t3, log = source
    now = time.time()
    add(log, [
        native_line(now - 5, "claudeAgent", "incoming", {
            "type": "system", "subtype": "session_title_changed", "title": "T3 thread",
            "session_id": "claude-native",
        }),
        native_line(now - 4, "claudeAgent", "incoming", {
            "type": "assistant", "session_id": "claude-native",
            "message": {"content": [{"type": "tool_use", "id": "choice", "name": "AskUserQuestion",
                                     "input": {"questions": ["private-question"]}}]},
        }),
    ])
    t3.poll(now=now)
    row, = t3.state.statuses()
    assert row.mode == AgentMode.WAITING_FOR_INPUT
    notification = phone_update(t3.state.pending_events("test")[-1][1])
    assert notification["status"] == {"app": "claude-code", "state": "blocked",
                                      "kind": "question", "title": "T3 thread"}
    assert "private-question" not in json.dumps(notification)

    add(log, [native_line(now - 2, "claudeAgent", "incoming", {
        "type": "user", "session_id": "claude-native", "tool_use_result": "private-answer",
        "message": {"content": [{"type": "tool_result", "tool_use_id": "choice"}]},
    })])
    t3.poll(now=now)
    assert t3.state.statuses()[0].mode == AgentMode.WORKING


def test_doctor_shows_t3_service_readiness_and_uses_local_fallback(source, monkeypatch, capsys):
    from argparse import Namespace
    from sidepulse.cli import cmd_doctor
    from sidepulse.program_status import StatusError

    t3, log = source
    now = time.time()
    assert t3.diagnostics()["files_read"] == 0
    add(log, [line(now - 1, "turn.started")])
    t3.poll(now=now)
    monkeypatch.setattr("sidepulse.cli.detect_provider_configs", lambda: [])
    monkeypatch.setattr("sidepulse.program_transport.daemon_command",
                        lambda command: {"runtime": {"t3code": t3.diagnostics()}})
    cmd_doctor(Namespace(json=True))
    live = json.loads(capsys.readouterr().out)["t3code"]
    assert live == {"log_directory": str(t3.root), "log_directory_exists": True,
                    "files_read": 1, "thread_links_available": True}

    def unavailable(command):
        raise StatusError("service offline")

    monkeypatch.setattr("sidepulse.program_transport.daemon_command", unavailable)
    monkeypatch.setenv("T3CODE_HOME", str(t3.home))
    cmd_doctor(Namespace(json=True))
    offline = json.loads(capsys.readouterr().out)["t3code"]
    assert offline == {**live, "files_read": None}
