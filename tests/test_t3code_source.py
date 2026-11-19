from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

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
    t3 = T3CodeSource(state.submit, env={"T3CODE_HOME": str(home)})
    t3.root.mkdir(parents=True)
    (home / "userdata" / "environment-id").write_text("environment\n")
    return t3, t3.root / "events.thread.log", state


def line(at, event, payload=None, *, thread="thread", provider="claudeAgent", **fields):
    date = datetime.fromtimestamp(at, timezone.utc).isoformat()
    data = {"createdAt": date, "provider": provider, "threadId": thread,
            "type": event, "payload": payload or {}, **fields}
    return f"[{date}] CANON: {json.dumps(data)}\n"


def add(path, lines):
    with path.open("a", encoding="utf-8") as file:
        file.write("".join(lines))


def test_canonical_lifecycle_without_hooks(source):
    t3, log, state = source
    now = time.time()
    add(log, [line(now - 9, "session.configured", {"config": {"cwd": "/work/project"}}),
              line(now - 8, "hook.started", raw={"payload": {"session_id": "native"}}),
              line(now - 7, "turn.started"),
              line(now - 6, "item.started", {"itemType": "command_execution", "title": "Command run",
                                             "data": {"command": "sensitive-command"}})])
    t3.poll(now=now)
    row, = state.statuses()
    assert (row.provider, row.session_id, row.cwd) == ("claude", "native", "/work/project")
    assert (row.mode, row.tool_name) == (AgentMode.TOOL_RUNNING, "Command run")
    assert row.t3_link == "t3code://threads/environment/thread"
    first_update = state.pending_events("test")[0][1]
    assert "sensitive-command" not in json.dumps(first_update)
    assert "t3_link" not in json.dumps(bridge_update(first_update))
    assert "t3_link" not in json.dumps(phone_update(first_update))

    add(log, [line(now - 5, "request.opened", requestId="first"),
              line(now - 4, "user-input.requested", requestId="second"),
              line(now - 3, "request.resolved", requestId="first"),
              line(now - 2, "item.completed", {"itemType": "command_execution"})])
    t3.poll(now=now)
    row, = state.statuses()
    assert row.mode == AgentMode.WAITING_FOR_INPUT
    number, update = state.pending_events("test")[0]
    assert update["status"]["kind"] == "question"
    t3.poll(now=now)
    assert state.pending_events("test")[0][0] == number

    completed = line(now - 1, "thread.state.changed", {"state": "idle"})
    add(log, [completed[:-1]])
    t3.poll(now=now)
    assert state.statuses()[0].mode == AgentMode.WAITING_FOR_INPUT
    add(log, ["\n"])
    t3.poll(now=now)
    assert state.statuses()[0].mode == AgentMode.COMPLETED
    t3.poll(now=now + MAX_FILE_AGE)
    assert not state.statuses()


def test_t3_exit_retains_recent_history_without_a_live_session(source):
    t3, log, state = source
    now = time.time()
    add(log, [line(now - 3, "turn.started", {"providerThreadId": "native"})])
    t3.poll(now=now)

    add(log, [line(now - 1, "session.exited")])
    t3.poll(now=now)
    assert not state.statuses()
    row, = state.statuses(include_closed=True)
    assert (row.session_id, row.mode, row.stale) == ("native", AgentMode.COMPLETED, True)
    assert row.t3_link == "t3code://threads/environment/thread"
    assert state.pending_events("phone")[-1][1]["status"]["state"] == "clear"


def test_native_hook_and_delayed_log_use_one_daemon_session(source):
    t3, log, state = source
    now = time.time()
    add(log, [line(now - 3, "thread.started", {"providerThreadId": "native"}),
              line(now - 2, "turn.completed")])
    with tempfile.TemporaryDirectory(prefix="sp-t3-", dir="/tmp" if os.name != "nt" else None) as tmp:
        socket = Path(tmp) / "t3.sock"
        server = ProgramServer(state, path=socket)
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
    t3, log, state = source
    now = time.time()
    add(log, [line(now - 3, "turn.started", thread="one"),
              line(now - 2, "turn.started", thread="two")])
    t3.poll(now=now)
    report = hook_report("claude", {"hook_event_name": "Stop", "session_id": "native",
                                  "agent_origin": "Claude in T3 Code",
                                  "t3_link": "t3code://threads/environment/one"})
    state.submit(report)
    rows = {row.t3_link.rsplit("/", 1)[-1]: row for row in state.statuses()}
    assert len(state.statuses()) == 2
    assert rows["one"].mode == AgentMode.COMPLETED
    assert rows["one"].session_id == "native"
    assert rows["two"].mode == AgentMode.WORKING

    next_hook = hook_report("claude", {"hook_event_name": "UserPromptSubmit", "session_id": "native"})
    state.submit(next_hook)
    rows = {row.t3_link.rsplit("/", 1)[-1]: row for row in state.statuses()}
    assert len(rows) == 2
    assert rows["one"].mode == AgentMode.WORKING
    assert rows["one"].origin == "Claude in T3 Code"


def test_log_rotation_and_old_events(source):
    t3, log, state = source
    now = time.time()
    add(log, [line(now - 10, "turn.started", provider="codex"),
              line(now - MAX_FILE_AGE - 1, "turn.started", thread="stale"),
              "broken log record\n"])
    t3.poll(now=now)
    row, = state.statuses()
    log.rename(log.with_suffix(".log.1"))
    add(log, [line(now - 5, "turn.completed", {"state": "failed"}, provider="codex")])
    os.utime(log, (now, now))
    t3.poll(now=now)
    failed, = state.statuses()
    assert failed.agent_id == row.agent_id
    assert failed.mode == AgentMode.BLOCKED_ERROR


def native_line(at, provider, direction, payload):
    date = datetime.fromtimestamp(at, timezone.utc).isoformat()
    protocol = {"codex": "codex.app-server", "claudeAgent": "claude-agent-sdk.query"}[provider]
    packet = {"provider": provider, "protocol": protocol, "kind": "protocol",
              "providerSessionId": "t3-internal-session",
              "event": {"stage": "decoded", "direction": direction, "payload": payload}}
    return f"[{date}] NTIVE: {json.dumps(packet)}\n"


@pytest.mark.parametrize(("hook_event", "tool_name", "kind"), [
    ("PreToolUse", "request_user_input_async", "question"),
    ("PermissionRequest", "Bash", "permission"),
])
def test_t3_background_activity_preserves_hook_wait(source, hook_event, tool_name, kind):
    t3, log, state = source
    now = time.time()
    session = "codex-native"
    waiting = hook_report("codex", {
        "hook_event_name": hook_event, "session_id": session, "tool_name": tool_name,
    })
    state.submit(waiting, observed_at=int((now - 7) * 1000))
    forwarded = state.pending_events("test")
    assert forwarded[0][1]["status"]["kind"] == kind

    add(log, [
        native_line(now - 9, "codex", "incoming", {
            "method": "turn/started", "params": {"threadId": session},
        }),
        native_line(now - 6, "codex", "incoming", {
            "method": "item/started", "params": {
                "threadId": session, "item": {"type": "commandExecution"},
            },
        }),
    ])
    t3.poll(now=now)
    assert state.statuses()[0].mode == AgentMode.WAITING_FOR_INPUT
    assert state.pending_events("test") == forwarded

    add(log, [native_line(now - 5, "codex", "incoming", {
        "method": "item/completed", "params": {
            "threadId": session, "item": {"type": "commandExecution", "status": "failed"},
        },
    })])
    t3.poll(now=now)
    assert state.statuses()[0].mode == AgentMode.WAITING_FOR_INPUT
    assert state.pending_events("test") == forwarded

    # A later turn start can release the wait even when the same poll sees a tool start.
    add(log, [
        native_line(now - 3, "codex", "incoming", {
            "method": "turn/started", "params": {"threadId": session},
        }),
        native_line(now - 2, "codex", "incoming", {
            "method": "item/started", "params": {
                "threadId": session, "item": {"type": "commandExecution"},
            },
        }),
    ])
    t3.poll(now=now)
    row, = state.statuses()
    assert row.mode == AgentMode.TOOL_RUNNING
    assert state.pending_events("test")[0][1]["status"]["state"] == "working"


def test_modern_codex_log_tracks_native_id_and_approval(source):
    t3, log, state = source
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
    row, = state.statuses()
    assert (row.session_id, row.mode, row.tool_name) == ("codex-native", AgentMode.TOOL_RUNNING, "Shell command")
    assert row.t3_link == "t3code://threads/environment/thread"
    assert "private-command" not in json.dumps(state.pending_events("test"))

    add(log, [native_line(now - 5, "codex", "incoming", {
        "method": "item/commandExecution/requestApproval", "id": 12, "params": {"threadId": "codex-native"},
    })])
    t3.poll(now=now)
    assert state.statuses()[0].mode == AgentMode.WAITING_FOR_INPUT

    add(log, [native_line(now - 4, "codex", "outgoing", {"id": 98, "result": {}})])
    t3.poll(now=now)
    assert state.statuses()[0].mode == AgentMode.WAITING_FOR_INPUT

    add(log, [native_line(now - 3.5, "codex", "incoming", {
        "method": "item/completed", "params": {
            "threadId": "codex-native", "item": {"type": "commandExecution", "status": "failed"},
        },
    })])
    t3.poll(now=now)
    assert state.statuses()[0].mode == AgentMode.WAITING_FOR_INPUT

    add(log, [
        native_line(now - 3, "codex", "outgoing", {"id": 12, "result": {"decision": "accept"}}),
        native_line(now - 2.5, "codex", "incoming", {
            "method": "item/completed", "params": {
                "threadId": "codex-native", "item": {"type": "commandExecution", "status": "completed"},
            },
        }),
    ])
    t3.poll(now=now)
    assert state.statuses()[0].mode == AgentMode.WORKING

    add(log, [native_line(now - 2, "codex", "incoming", {
        "method": "turn/completed", "params": {"threadId": "codex-native", "turn": {"status": "completed"}},
    }), native_line(now - 1, "codex", "incoming", {
        "method": "serverRequest/resolved", "params": {"threadId": "codex-native", "requestId": 12},
    })])
    t3.poll(now=now)
    final, = state.statuses()
    assert final.agent_id == row.agent_id
    assert final.mode == AgentMode.COMPLETED
    assert final.cwd == "/work/project"


def test_modern_claude_log_counts_one_live_hook_session(source):
    t3, log, state = source
    now = time.time()
    hook = hook_report("claude", {"hook_event_name": "UserPromptSubmit", "session_id": "claude-native"})
    state.submit(hook)
    add(log, [
        native_line(now - 8, "claudeAgent", "outgoing", {"type": "query.open", "options": {"cwd": "/project"}}),
        native_line(now - 7, "claudeAgent", "outgoing", {"type": "prompt.offer", "message": "private-prompt"}),
        native_line(now - 6, "claudeAgent", "incoming", {"type": "system", "subtype": "init",
                                                       "session_id": "claude-native", "cwd": "/project"}),
    ])
    t3.poll(now=now)
    row, = state.statuses()
    assert (row.session_id, row.t3_link) == ("claude-native", "t3code://threads/environment/thread")
    assert row.mode == AgentMode.WORKING
    assert "private-prompt" not in json.dumps(state.pending_events("test"))

    later = time.time() + 1
    add(log, [native_line(later, "claudeAgent", "incoming", {
        "type": "result", "subtype": "success", "session_id": "claude-native", "is_error": False,
    })])
    t3.poll(now=later + 1)
    final, = state.statuses()
    assert final.agent_id == row.agent_id
    assert final.mode == AgentMode.COMPLETED


def test_t3_codex_and_hook_updates_use_the_same_phone_and_bridge_states(source):
    t3, log, state = source
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
        _, t3_update = state.pending_events("test")[-1]
        _, hook_update = hooks.pending_events("test")[-1]
        assert t3_update["scope"] == hook_update["scope"]
        assert state.statuses()[0].mode == hooks.statuses()[0].mode
        for encode in (bridge_update, phone_update):
            output = encode(t3_update)
            expected = encode(hook_update)
            # Hook permission requests may include a tool invocation ID.
            assert {k: v for k, v in output["status"].items() if k != "id"} == {
                k: v for k, v in expected["status"].items() if k != "id"
            }
            assert output.get("context") == expected.get("context")


def test_claude_question_and_title_are_reported_from_native_logs(source):
    t3, log, state = source
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
    row, = state.statuses()
    assert row.mode == AgentMode.WAITING_FOR_INPUT
    notification = phone_update(state.pending_events("test")[-1][1])
    assert notification["status"] == {"app": "claude-code", "state": "blocked",
                                      "kind": "question", "title": "T3 thread"}
    assert "private-question" not in json.dumps(notification)

    add(log, [native_line(now - 2, "claudeAgent", "incoming", {
        "type": "user", "session_id": "claude-native", "tool_use_result": "private-answer",
        "message": {"content": [{"type": "tool_result", "tool_use_id": "choice"}]},
    })])
    t3.poll(now=now)
    assert state.statuses()[0].mode == AgentMode.WORKING


def test_doctor_shows_t3_service_readiness_and_uses_local_fallback(source, monkeypatch, capsys):
    from argparse import Namespace
    from sidepulse.cli import cmd_doctor
    from sidepulse.program_status import StatusError

    t3, log, state = source
    now = time.time()
    assert t3.diagnostics()["files_read"] == 0
    add(log, [line(now - 1, "turn.started")])
    t3.poll(now=now)
    monkeypatch.setattr("sidepulse.cli.detect_provider_configs", lambda: [])
    monkeypatch.setattr("sidepulse.program_transport.daemon_command",
                        lambda command: {"runtime": {"t3code": t3.diagnostics()}})
    cmd_doctor(Namespace(json=True))
    live = json.loads(capsys.readouterr().out)["t3code"]
    assert live == {"enabled": True, "log_directory": str(t3.root), "log_directory_exists": True,
                    "files_read": 1, "thread_links_available": True}

    def unavailable(command):
        raise StatusError("service offline")

    monkeypatch.setattr("sidepulse.program_transport.daemon_command", unavailable)
    monkeypatch.setenv("T3CODE_HOME", str(t3.home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(t3.home))
    cmd_doctor(Namespace(json=True))
    offline = json.loads(capsys.readouterr().out)["t3code"]
    assert offline == {"enabled": False, "log_directory": str(t3.root), "files_read": None,
                       "log_directory_exists": False, "thread_links_available": False}


def test_setup_and_uninstall_toggle_t3_monitoring(tmp_path, monkeypatch, capsys):
    from sidepulse.cli import build_sidepulse_parser
    from sidepulse.settings import load_settings

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr("sidepulse.service_launch.install_service",
                        lambda **kwargs: SimpleNamespace(path=tmp_path, started=True))
    parser = build_sidepulse_parser()

    def setup(*options):
        args = parser.parse_args(["setup", "t3code", "--no-status-bar", "--no-sd-eject-guard", *options])
        assert args.func(args) == 0
        return capsys.readouterr().out

    assert not load_settings().t3code_enabled
    assert "t3code: would enable" in setup("--dry-run")
    assert not load_settings().t3code_enabled
    assert "t3code: enabled" in setup()
    assert load_settings().t3code_enabled
    assert "t3code: would disable" in setup("--uninstall", "--dry-run")
    assert load_settings().t3code_enabled
    assert "t3code: disabled" in setup("--uninstall")
    assert not load_settings().t3code_enabled


def test_separate_scanner_reports_to_daemon_and_stops_when_disabled(tmp_path, monkeypatch):
    from sidepulse import program_transport
    from sidepulse.service import SidePulseService

    home = tmp_path / "t3code"
    log_dir = home / "userdata" / "logs" / "provider"
    log_dir.mkdir(parents=True)
    monkeypatch.setenv("T3CODE_HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    now = time.time()
    add(log_dir / "events.thread.log", [
        line(now - 3, "thread.started", {"providerThreadId": "scanner-only"}),
        line(now - 2, "turn.started"),
        line(now - 1, "turn.started", {"providerThreadId": "with-hook"}, thread="combined"),
    ])

    with tempfile.TemporaryDirectory(prefix="sp-t3-", dir="/tmp" if os.name != "nt" else None) as directory:
        socket = Path(directory) / "status.sock"
        monkeypatch.setattr(program_transport, "status_socket_path", lambda: socket)
        service = SidePulseService()
        service.program_server.start()
        try:
            hook = hook_report("claude", {"hook_event_name": "UserPromptSubmit", "session_id": "with-hook"})
            service.program_state.submit(hook, observed_at=int((now - 4) * 1000))

            service.t3code.sync(False)
            assert service.t3code.process is None
            service.t3code.sync(True)
            process = service.t3code.process
            assert process is not None and process.pid != os.getpid()

            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                view = daemon_command({"type": "get_state"}, path=socket)
                if view["runtime"]["t3code"]["files_read"] == 1:
                    break
                time.sleep(0.05)
            else:
                pytest.fail("Separate T3 Code scanner did not report its diagnostics")
            assert {row["session_id"] for row in view["snapshot"]["statuses"]} == {"scanner-only", "with-hook"}

            service.t3code.sync(False)
            assert process.poll() == 0
            view = daemon_command({"type": "get_state"}, path=socket)
            assert not view["runtime"]["t3code"]["enabled"]
            assert [row["session_id"] for row in view["snapshot"]["statuses"]] == ["with-hook"]
            updates = [update["status"]["state"] for _, update in service.program_state.pending_events("phone")]
            assert updates.count("clear") == 1
        finally:
            service.t3code.close()
            service.program_server.stop()
            service.runtime.close()


@pytest.mark.parametrize("resolution", ["answer", "dismiss"])
def test_t3_message_question_remains_open_after_turn_completion(source, resolution):
    import sqlite3
    from contextlib import closing

    t3, log, state = source
    now = time.time()
    request = "codex-async:thread:question"
    add(log, [line(now - 8, "turn.started", provider="codex"),
              line(now - 7, "user-input.requested", {"responseMode": "message"},
                   provider="codex", requestId=request)])
    t3.poll(now=now)
    assert state.statuses()[0].mode == AgentMode.WAITING_FOR_INPUT

    # T3 finishes generating after posting the question and accepts the answer later.
    add(log, [line(now - 6, "item.completed", {"itemType": "assistant_message"}, provider="codex"),
              line(now - 5, "thread.state.changed", {"state": "idle"}, provider="codex"),
              line(now - 5, "turn.completed", {"state": "completed"}, provider="codex")])
    t3.poll(now=now)
    row, = state.statuses()
    assert (row.mode, row.event_name) == (AgentMode.WAITING_FOR_INPUT, "user-input.requested")
    assert phone_update(state.pending_events("test")[-1][1])["status"]["kind"] == "question"

    with closing(sqlite3.connect(t3.home / "userdata" / "state.sqlite")) as db:
        db.execute("PRAGMA journal_mode=WAL")
        with db:
            db.execute("""CREATE TABLE projection_thread_activities (
                activity_id TEXT PRIMARY KEY, thread_id TEXT, kind TEXT, created_at TEXT
            )""")
            db.execute("INSERT INTO projection_thread_activities VALUES (?, ?, ?, ?)", (
                f"async-{resolution}:other-request", "thread", "user-input.resolved",
                datetime.fromtimestamp(now - 3, timezone.utc).isoformat(),
            ))
        t3.poll(now=now)
        assert state.statuses()[0].mode == AgentMode.WAITING_FOR_INPUT

        # T3 records a reply or dismissal in its activity database, even when the
        # provider log is unchanged. A reply will also start a fresh provider turn.
        with db:
            db.execute("INSERT INTO projection_thread_activities VALUES (?, ?, ?, ?)", (
                f"async-{resolution}:{request}", "thread", "user-input.resolved",
                datetime.fromtimestamp(now - 2, timezone.utc).isoformat(),
            ))
        t3.poll(now=now)
        row, = state.statuses()
        expected = AgentMode.WORKING if resolution == "answer" else AgentMode.COMPLETED
        assert (row.mode, row.event_name) == (expected, "user-input.resolved")
        if resolution == "answer":
            add(log, [line(now - 1, "turn.started", provider="codex")])
            t3.poll(now=now)
            assert state.statuses()[0].mode == AgentMode.WORKING


@pytest.mark.parametrize(("filename", "table"), [
    ("state.sqlite", "projection_threads"),
    ("statev2.sqlite", "orchestration_v2_projection_threads"),
])
def test_t3_thread_title_updates_live_session_without_new_provider_event(source, filename, table):
    import sqlite3
    from contextlib import closing

    t3, log, state = source
    now = time.time()
    provider = "codex"
    native = "codex-native"
    add(log, [line(now - 4, "thread.started", {"providerThreadId": native}, provider=provider),
              line(now - 3, "turn.started", provider=provider)])
    hook = hook_report(provider, {
        "hook_event_name": "PreToolUse", "tool_name": "request_user_input_async", "session_id": native,
    })
    state.submit(hook)
    initial, = state.statuses()

    with closing(sqlite3.connect(t3.userdata / filename)) as db:
        db.execute("PRAGMA journal_mode=WAL")
        with db:
            db.execute(f"CREATE TABLE {table} (thread_id TEXT PRIMARY KEY, title TEXT)")
            db.execute(f"INSERT INTO {table} VALUES (?, ?)", ("thread", "Original name"))
        t3.poll(now=now)
        row, = state.statuses()
        assert row.display_name.startswith("Original name")
        assert row.updated_at == initial.updated_at
        assert row.mode == AgentMode.WAITING_FOR_INPUT

        with db:
            db.execute(f"UPDATE {table} SET title = ? WHERE thread_id = ?", ("Renamed thread", "thread"))
        t3.poll(now=now)
        row, = state.statuses()
        assert row.display_name.startswith("Renamed thread")
        assert row.updated_at == initial.updated_at
        assert row.mode == AgentMode.WAITING_FOR_INPUT

    follow_up = hook_report(provider, {"hook_event_name": "UserPromptSubmit", "session_id": native})
    follow_up["context"].update(title="Name from provider", title_source="session")
    follow_up["status"]["title"] = "Name from provider"
    state.submit(follow_up)
    row, = state.statuses()
    assert row.display_name.startswith("Renamed thread")
    _, update = state.pending_events("test")[-1]
    assert bridge_update(update)["status"]["title"] == "Renamed thread"
    assert phone_update(update)["status"]["title"] == "Renamed thread"


def test_t3_pending_question_text_survives_completion_and_clears_after_answer(source):
    t3, log, state = source
    now = time.time()
    text = "What would you most like to improve in Cardflow?"
    first = "codex-async:thread:first"
    second = "codex-async:thread:second"
    add(log, [
        line(now - 8, "turn.started", provider="codex"),
        line(now - 7, "user-input.requested", {
            "responseMode": "message", "questions": [{"header": "Question", "question": text,
                "options": [{"label": "private-option"}]}],
        }, requestId=first, provider="codex"),
        line(now - 6, "user-input.requested", {
            "responseMode": "message", "questions": [{"question": "Which view should open?"}],
        }, requestId=second, provider="codex"),
        line(now - 5, "turn.completed", {"state": "completed"}, provider="codex"),
    ])
    t3.poll(now=now)
    row, = state.statuses()
    assert (row.mode, row.message) == (AgentMode.WAITING_FOR_INPUT, text)
    notification = phone_update(state.pending_events("test")[-1][1])
    assert notification["status"]["msg"] == text
    assert "private-option" not in json.dumps(notification)

    add(log, [line(now - 4, "user-input.resolved", requestId=first, provider="codex")])
    t3.poll(now=now)
    assert state.statuses()[0].message == "Which view should open?"
    add(log, [line(now - 3, "user-input.resolved", requestId=second, provider="codex")])
    t3.poll(now=now)
    assert state.statuses()[0].message is None
    assert "msg" not in phone_update(state.pending_events("test")[-1][1])["status"]


@pytest.mark.parametrize("provider", ["claudeAgent", "codex"])
def test_native_t3_question_text_uses_the_hook_report_message(source, provider):
    t3, log, state = source
    now = time.time()
    question = "Which branch should we use?"
    questions = [{"question": question}]
    if provider == "codex":
        packet = {"method": "item/tool/requestUserInput", "id": 12,
                  "params": {"threadId": "native", "questions": questions}}
        hook = hook_report("codex", {"hook_event_name": "PreToolUse", "session_id": "native",
            "tool_name": "request_user_input", "tool_input": {"questions": questions}})
    else:
        packet = {"type": "assistant", "session_id": "native",
                  "message": {"content": [{"type": "tool_use", "id": "q", "name": "AskUserQuestion",
                                          "input": {"questions": questions}}]}}
        hook = hook_report("claude", {"hook_event_name": "PermissionRequest", "session_id": "native",
            "tool_name": "AskUserQuestion", "tool_input": {"questions": questions}})
    add(log, [native_line(now - 1, provider, "incoming", packet)])
    t3.poll(now=now)
    row, = state.statuses()
    assert row.mode == AgentMode.WAITING_FOR_INPUT
    assert row.message == hook["status"]["msg"] == question
