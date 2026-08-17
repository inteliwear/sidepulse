from __future__ import annotations

import io
import json
import plistlib
from pathlib import Path

from sidepulse import cli
from sidepulse.collector import StatusMetadata, status_from_event
from sidepulse.providers import parse_log_line
from sidepulse.remote_hosts import (
    RemoteHost,
    _emit_envelope,
    configured_remote_logs,
    load_remote_hosts,
    qualify_remote_line,
    remove_remote_host,
    save_remote_hosts,
    ssh_stream_command,
    upsert_remote_host,
)
from sidepulse.remote_launch import (
    LAUNCH_AGENT_LABEL,
    build_remote_launch_agent_plist,
    build_remote_launcher_script,
    install_remote_launch_agent,
)


def test_remote_host_config_round_trip(tmp_path: Path) -> None:
    config = tmp_path / "remote-hosts.json"
    first = RemoteHost("macmini", "mini")
    second = RemoteHost("build-2", "developer@build-2", ("codex",))

    save_remote_hosts((first, second), config)

    assert load_remote_hosts(config) == (second, first)
    data = json.loads(config.read_text())
    assert data["version"] == 1
    assert data["hosts"][1]["providers"] == ["codex", "claude"]


def test_upsert_and_remove_remote_host(tmp_path: Path) -> None:
    config = tmp_path / "remote-hosts.json"
    upsert_remote_host(RemoteHost("macmini", "old"), config)
    upsert_remote_host(RemoteHost("macmini", "mini", ("claude",)), config)

    assert load_remote_hosts(config) == (RemoteHost("macmini", "mini", ("claude",)),)
    target, changed = remove_remote_host("macmini", config)
    assert target == config
    assert changed
    assert load_remote_hosts(config) == ()


def test_remote_host_rejects_unsafe_names_and_targets() -> None:
    for name in ("", "bad name", "../mini"):
        try:
            RemoteHost(name, "mini")
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted unsafe name {name!r}")

    try:
        RemoteHost("mini", "mini\nmalicious")
    except ValueError:
        pass
    else:
        raise AssertionError("accepted SSH target containing a newline")


def test_qualify_remote_codex_line_namespaces_ids_and_origin() -> None:
    line = {
        "logged_at": "2026-08-17T10:00:00Z",
        "event": {
            "hook_event_name": "PreToolUse",
            "session_id": "session-1",
            "turnId": "turn-1",
            "agent_id": "agent-1",
        },
    }

    result = qualify_remote_line("codex", line, "macmini")
    event = result["event"]

    assert event["session_id"] == "remote:macmini:session-1"
    assert event["turnId"] == "remote:macmini:turn-1"
    assert event["agent_id"] == "remote:macmini:agent-1"
    assert event["agent_origin"] == "Codex on macmini"
    assert event["sidepulse_remote_host"] == "macmini"
    assert line["event"]["session_id"] == "session-1"
    record = parse_log_line("codex", json.dumps(result))
    assert record is not None
    status = status_from_event(record, StatusMetadata())
    assert status is not None
    assert status.origin == "Codex on macmini"
    assert status.agent_id == "codex:agent:remote:macmini:agent-1"


def test_qualify_remote_claude_line_is_idempotent() -> None:
    line = {
        "hook_event_name": "Stop",
        "session_id": "remote:macmini:session-1",
        "last_assistant_message": "Finished.",
    }

    result = qualify_remote_line("claude", line, "macmini")
    result = qualify_remote_line("claude", result, "macmini")

    assert result["session_id"] == "remote:macmini:session-1"
    assert result["agent_origin"] == "Claude on macmini"


def test_ssh_stream_command_uses_outbound_keepalive_and_login_shell() -> None:
    command = ssh_stream_command(RemoteHost("macmini", "mini"), replay_lines=12)

    assert command[0:2] == ["ssh", "-T"]
    assert "BatchMode=yes" in command
    assert "ServerAliveInterval=15" in command
    assert command[-2] == "mini"
    assert "zsh -lic" in command[-1]
    assert "remote-agent stream" in command[-1]
    assert "--replay-lines 12" in command[-1]


def test_emit_envelope_ignores_invalid_json() -> None:
    output = io.StringIO()
    _emit_envelope("codex", "not-json", output)
    _emit_envelope("codex", '{"event":{"hook_event_name":"Stop"}}', output)

    lines = output.getvalue().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["provider"] == "codex"


def test_configured_remote_logs_uses_each_hosts_providers(tmp_path: Path) -> None:
    config = tmp_path / "remote-hosts.json"
    home = tmp_path / "home"
    save_remote_hosts((RemoteHost("mini", "mini", ("codex", "claude")),), config)

    logs = configured_remote_logs(config_path=config, home=home)

    assert logs == (
        ("codex", home / ".local/state/sidepulse/agent-monitor/remote/mini/codex.jsonl"),
        ("claude", home / ".local/state/sidepulse/agent-monitor/remote/mini/claude.jsonl"),
    )


def test_remote_cli_shapes() -> None:
    parser = cli.build_sidepulse_parser()
    add = parser.parse_args(["remote", "add", "macmini", "--ssh", "mini"])
    stream = parser.parse_args(
        ["remote-agent", "stream", "--provider", "codex", "--replay-lines", "10"]
    )

    assert add.name == "macmini"
    assert add.ssh_target == "mini"
    assert stream.provider == ["codex"]
    assert stream.replay_lines == 10


def test_remote_launch_agent_files(tmp_path: Path) -> None:
    plist_path = tmp_path / "LaunchAgents" / "io.sidepulse.remotehosts.plist"
    launcher_path = tmp_path / "SidePulse Remote Hosts"
    result = install_remote_launch_agent(
        start=False,
        plist_path=plist_path,
        launcher_path=launcher_path,
        python_executable="/example/python3",
    )

    assert result.changed
    assert launcher_path.stat().st_mode & 0o111
    assert "remote monitor" in build_remote_launcher_script("/example/python3")
    plist = plistlib.loads(plist_path.read_bytes())
    assert plist["Label"] == LAUNCH_AGENT_LABEL
    assert plist["KeepAlive"] is True
    assert plist["ProgramArguments"] == [str(launcher_path)]
    assert build_remote_launch_agent_plist(
        python_executable="/example/python3",
        launcher_path=launcher_path,
    )["Label"] == LAUNCH_AGENT_LABEL
