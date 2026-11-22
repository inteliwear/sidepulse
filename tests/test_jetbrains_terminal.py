import json
from dataclasses import replace
from unittest.mock import patch

import pytest

from sidepulse.collector import status_from_event
from sidepulse.hook import format_hook_payload
from sidepulse.origin import detect_agent_origin
from sidepulse.providers import parse_log_line
from sidepulse.session_actions import (
    IdeOpenTarget,
    available_session_open_targets,
    default_session_open_target,
)


@pytest.fixture
def phpstorm_status():
    env = {
        "TERMINAL_EMULATOR": "JetBrains-JediTerm",
        "__CFBundleIdentifier": "com.jetbrains.PhpStorm",
        "CLAUDE_CODE_ENTRYPOINT": "cli",
    }
    with patch.dict("os.environ", env, clear=True):
        payload = format_hook_payload(
            "claude", '{"hook_event_name":"UserPromptSubmit","session_id":"test","prompt":"hello"}'
        )
    assert payload["agent_origin_kind"] == "claude_cli"
    return status_from_event(parse_log_line("claude", json.dumps(payload)))


def test_phpstorm_hook_prefers_ide(phpstorm_status):
    assert phpstorm_status.origin == "Claude CLI in PhpStorm"
    with patch("sidepulse.session_actions.installed_app_paths_for_bundle_id", return_value=("/ide",)) as lookup:
        assert default_session_open_target(phpstorm_status) == (
            "jetbrains", ("ide", IdeOpenTarget("com.jetbrains.PhpStorm", None, ide_name="PhpStorm"))
        )
    lookup.assert_called_once_with("com.jetbrains.PhpStorm")


@pytest.mark.parametrize(
    ("provider", "env", "kind"),
    [
        ("claude", {"TERMINAL_EMULATOR": "JetBrains-JediTerm"}, "claude_cli"),
        ("codex", {"__CFBundleIdentifier": "com.jetbrains.PhpStorm"}, "codex_cli"),
        ("grok", {"__CFBundleIdentifier": "com.jetbrains.newide"}, "grok_cli"),
        ("junie", {"__CFBundleIdentifier": "com.jetbrains.PhpStorm"}, "junie_ide"),
        ("junie", {"__CFBundleIdentifier": "com.jetbrains.PhpStorm", "TERMINAL_EMULATOR": "JetBrains-JediTerm"}, "junie_cli"),
        ("claude", {"CLAUDE_CODE_ENTRYPOINT": "cli"}, "claude_cli"),
        ("claude", {"CLAUDE_CODE_ENTRYPOINT": "cli", "TERM_PROGRAM": "vscode"}, "claude_vscode"),
    ],
)
def test_terminal_origin(provider, env, kind):
    with patch("sidepulse.origin.process_ancestry", return_value=()):
        assert detect_agent_origin(provider, env=env).kind == kind


def test_unavailable_ide_falls_back_to_terminal(phpstorm_status):
    with patch("sidepulse.session_actions.installed_app_paths_for_bundle_id", return_value=()):
        assert default_session_open_target(phpstorm_status)[0] == "terminal"


def test_old_unknown_origin_skips_unregistered_vscode(phpstorm_status):
    status = replace(phpstorm_status, origin="Claude")
    with patch("sidepulse.session_actions.vscode_url_available", return_value=False):
        assert "vscode" not in available_session_open_targets(status)
        assert default_session_open_target(status)[0] == "terminal"
