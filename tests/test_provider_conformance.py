from __future__ import annotations

import json
import unittest

from sidepulse.collector import mode_for_event
from sidepulse.models import AgentMode
from sidepulse.providers import HOOK_PROVIDERS, parse_log_line


class ProviderConformanceTests(unittest.TestCase):
    CASES = (("SessionStart", AgentMode.IDLE_READY), ("UserPromptSubmit", AgentMode.WORKING), ("PreToolUse", AgentMode.TOOL_RUNNING), ("PostToolUse", AgentMode.WORKING), ("Stop", AgentMode.COMPLETED))

    def test_all_providers_conform_to_core_lifecycle(self) -> None:
        for provider in HOOK_PROVIDERS:
            for event_name, expected_mode in self.CASES:
                with self.subTest(provider=provider, event=event_name):
                    event = parse_log_line(provider, json.dumps({"hook_event_name": event_name, "session_id": f"{provider}-session", "cwd": "/repo", "tool_name": "shell" if "Tool" in event_name else None, "timestamp": "2026-08-15T00:00:00Z"}))
                    self.assertIsNotNone(event)
                    assert event is not None
                    self.assertEqual(event.provider, provider)
                    self.assertEqual(event.session_id, f"{provider}-session")
                    self.assertEqual(mode_for_event(event), expected_mode)

    def test_common_snake_and_camel_case_payloads_normalize_equally(self) -> None:
        snake = parse_log_line("kiro", '{"hook_event_name":"pre_tool_use","session_id":"s","tool_name":"shell"}')
        camel = parse_log_line("kiro", '{"hookEventName":"preToolUse","sessionId":"s","toolName":"shell"}')
        assert snake is not None and camel is not None
        self.assertEqual((snake.event_name, snake.session_id, snake.tool_name), (camel.event_name, camel.session_id, camel.tool_name))
