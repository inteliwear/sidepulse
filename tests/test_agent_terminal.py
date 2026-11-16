import json
from datetime import datetime, timedelta
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from sidepulse.agent_terminal import (
    AgentTerminal, claude_session_terminal, junie_session_terminal, live_agent_terminal,
)
from sidepulse.origin import ProcessInfo
from sidepulse.settings import TERMINAL_APP_TERMINAL


@pytest.mark.parametrize(
    "later_event, should_focus",
    [
        ({"session_id": "other-session", "sidepulse_junie_process_id": 456}, True),
        ({"session_id": "other-session", "sidepulse_junie_process_id": 123}, False),
        ({"session_id": "my-session", "hook_event_name": "SessionEnd"}, False),
    ],
)
def test_junie_terminal_lookup_uses_the_sessions_current_process(tmp_path, later_event, should_focus):
    log = tmp_path / "junie.jsonl"
    event = {
        "session_id": "my-session",
        "hook_event_name": "PreToolUse",
        "sidepulse_junie_process_id": 123,
        "logged_at": "2026-11-16T20:20:52Z",
    }
    log.write_text(json.dumps(event) + "\n" + json.dumps(later_event) + "\n")
    terminal = AgentTerminal("/dev/ttys028", TERMINAL_APP_TERMINAL)
    with (
        patch("sidepulse.agent_terminal.detect_log_path", return_value=log),
        patch("sidepulse.agent_terminal.live_agent_terminal", return_value=terminal) as lookup,
    ):
        result = junie_session_terminal("my-session")

    if should_focus:
        assert result == terminal
        assert lookup.call_args.args[1] == 123
    else:
        assert result is None
        lookup.assert_not_called()


def test_live_junie_terminal_checks_process_start_and_terminal_app():
    started_at = datetime(2026, 11, 16, 12, 20, 39).astimezone()
    executable = "/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal"
    processes = (
        ProcessInfo(123, 456, "/usr/local/bin/junie", "/usr/local/bin/junie"),
        ProcessInfo(456, 1, "/System/Applicat", executable),
    )
    ps = CompletedProcess(
        args=[], returncode=0, stdout="501 ttys028 Mon Nov 16 12:20:39 2026\n"
    )
    with (
        patch("sidepulse.agent_terminal.process_ancestry", return_value=processes),
        patch("sidepulse.agent_terminal.subprocess.run", return_value=ps),
        patch("sidepulse.agent_terminal.os.getuid", return_value=501, create=True),
    ):
        assert live_agent_terminal("junie", 123, started_at) == AgentTerminal(
            "/dev/ttys028", TERMINAL_APP_TERMINAL
        )
        assert live_agent_terminal("junie", 123, started_at - timedelta(seconds=1)) is None


def test_junie_terminal_uses_original_live_process_after_a_duplicate_exits(tmp_path):
    log = tmp_path / "junie.jsonl"
    events = [
        {"session_id": "my-session", "sidepulse_junie_process_id": 123},
        {"session_id": "my-session", "sidepulse_junie_process_id": 456},
    ]
    log.write_text("\n".join(map(json.dumps, events)) + "\n")
    terminal = AgentTerminal("/dev/ttys028", TERMINAL_APP_TERMINAL)
    with (
        patch("sidepulse.agent_terminal.detect_log_path", return_value=log),
        patch("sidepulse.agent_terminal.live_agent_terminal", side_effect=[None, terminal]) as lookup,
    ):
        assert junie_session_terminal("my-session") == terminal

    assert [call.args[1] for call in lookup.call_args_list] == [456, 123]


def test_claude_terminal_finds_the_live_pid_for_this_session(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "123.json").write_text(json.dumps({
        "sessionId": "claude-session",
        "pid": 123,
        "startedAt": 1794862410215,
        "cwd": "/work/project",
        "name": "Review the project",
    }))
    (sessions / "456.json").write_text(json.dumps({
        "sessionId": "other-session",
        "pid": 456,
        "startedAt": 1794862410215,
    }))
    (sessions / "789.json").write_text(json.dumps({
        "sessionId": "claude-session",
        "pid": 789,
        "startedAt": 1794862420215,
    }))
    with patch(
        "sidepulse.agent_terminal.live_agent_terminal",
        return_value=AgentTerminal("/dev/ttys029", "ghostty"),
    ) as lookup:
        assert claude_session_terminal("claude-session") == AgentTerminal(
            "/dev/ttys029", "ghostty", "/work/project", "Review the project"
        )

    lookup.assert_called_once()
    assert lookup.call_args.args[:2] == ("claude", 123)
