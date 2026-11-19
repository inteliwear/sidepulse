import io
import json
import sys
import subprocess

import pytest

from sidepulse.antigravity_hook import normalize_payload
from sidepulse.hook import hook_log_main
from sidepulse.install import configure_antigravity_hooks
from sidepulse.program_status import ProgramState
from sidepulse.program_transport import ProgramServer
from sidepulse.session_actions import session_resume_command
from sidepulse.providers import antigravity_config_path, detect_antigravity_config, detect_log_path


def test_refresh_and_uninstall_preserve_custom_hooks(tmp_path):
    config = antigravity_config_path(tmp_path)
    log = tmp_path / "log path" / "events"
    assert configure_antigravity_hooks(log, config, dry_run=True).changed
    assert not config.exists()
    assert not log.exists()
    config.parent.mkdir(parents=True)
    custom = {"type": "command", "command": "./format"}
    original = {
        "formatter": {"Stop": [custom]},
        "sidepulse": {"Stop": [custom], "enabled": False,
                      "PreToolUse": [{"matcher": "view_file", "hooks": [custom]}]},
    }
    config.write_text(json.dumps(original))
    assert configure_antigravity_hooks(log, config).changed
    assert not configure_antigravity_hooks(log, config).changed
    assert detect_log_path("antigravity", tmp_path) == log
    detected = detect_antigravity_config(tmp_path)
    assert not detected.hooks_enabled
    assert set(detected.hook_events) == {"SessionStart", "PreInvocation", "PreToolUse", "PostToolUse", "Stop"}
    assert configure_antigravity_hooks(log, config, uninstall=True).changed
    assert json.loads(config.read_text()) == original
    assert not configure_antigravity_hooks(log, config, uninstall=True).changed


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("SIDEPULSE_DISABLE_EVENT_SOCKET", raising=False)
    state = ProgramState(publisher="test-sidepulse")
    server = ProgramServer(state)
    server.start()
    try:
        yield state
    finally:
        server.stop()
        state.close()


def test_installed_commands_report_native_lifecycle(tmp_path, daemon):
    config = antigravity_config_path(tmp_path)
    log = tmp_path / "events with spaces"
    configure_antigravity_hooks(log, config)
    assert detect_antigravity_config(tmp_path).hooks_enabled
    group = json.loads(config.read_text())["sidepulse"]
    base = {"conversationId": "session-1", "workspacePaths": ["/work/example"],
            "transcriptPath": r"C:\Users\a\.gemini\antigravity-cli\brain\session-1\log"}
    for event, extra, state in [
        ("SessionStart", {}, "idle"),
        ("PreInvocation", {"lastUserInput": "Update documentation"}, "working"),
        ("PreToolUse", {"toolCall": {"name": "view_file"}}, "working"),
        ("PostToolUse", {"toolCall": {"name": "view_file"}, "error": "File missing"}, "error"),
        ("PreToolUse", {"toolCall": {"name": "ask_question", "args": {
            "questions": [{"question": "Which project?"}]}}}, "blocked"),
        ("PostToolUse", {"toolCall": {"name": "ask_question"}}, "working"),
        ("PreToolUse", {"toolCall": {"name": "ask_permission"}}, "blocked"),
        ("Stop", {"terminationReason": "model_stop", "fullyIdle": False}, "working"),
        ("Stop", {"terminationReason": "error", "error": "Connection lost"}, "error"),
        ("Stop", {"terminationReason": "model_stop", "fullyIdle": True}, "done"),
    ]:
        entry = group[event][0]
        handler = entry["hooks"][0] if event.endswith("ToolUse") else entry
        result = subprocess.run(handler["command"], shell=True, capture_output=True,
                                text=True, input=json.dumps({**base, **extra}), timeout=10, check=True)
        assert json.loads(result.stdout) == ({"decision": "allow"} if event == "PreToolUse" else {})
        (status,) = daemon.statuses()
        assert status.provider == "antigravity"
        assert session_resume_command(status) == "cd /work/example && agy --conversation session-1"
        _, report = daemon.pending_events("test-device")[-1]
        assert report["status"]["state"] == state
        assert report["context"]["session_id"] == "session-1"
        assert report["context"]["origin"] == "Antigravity CLI"
        assert report["context"]["cwd"] == "/work/example"
        if event == "PreInvocation":
            assert report["context"]["title"] == "Update documentation"
        if state == "blocked":
            if extra["toolCall"]["name"] == "ask_question":
                assert report["status"]["msg"] == "Which project?"
                assert report["status"]["kind"] == "question"
            else:
                assert report["status"]["kind"] == "permission"


@pytest.mark.parametrize(("directory", "label"), [("antigravity", "Antigravity App"),
                                                  ("antigravity-ide", "Antigravity IDE")])
def test_desktop_origins(directory, label):
    raw = {"conversationId": "session", "transcriptPath": f"/a/{directory}/brain/session/log"}
    assert normalize_payload("Stop", raw)["agent_origin"] == label
    assert normalize_payload("Stop", {}) is None


def test_invalid_input_leaves_the_permission_decision_with_antigravity(tmp_path, monkeypatch, capsys):
    log = tmp_path / "events"
    monkeypatch.setattr(sys, "stdin", io.StringIO("invalid json"))
    assert hook_log_main("antigravity", log, event="PreToolUse") == 0
    assert json.loads(capsys.readouterr().out) == {"decision": "allow"}
    assert not log.exists()
