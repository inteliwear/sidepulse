from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sidepulse.install import install_kiro_hooks, uninstall_kiro_hooks
from sidepulse.providers import KIRO_EVENTS, detect_kiro_config, parse_log_line


class KiroProviderTests(unittest.TestCase):
    def test_install_detect_and_uninstall_managed_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            config = home / ".kiro" / "agents" / "sidepulse.json"
            log = home / "state" / "kiro.jsonl"
            installed = install_kiro_hooks(config_path=config, log_path=log, python_executable="python3")
            self.assertTrue(installed.changed)
            data = json.loads(config.read_text())
            self.assertIn("SidePulse lifecycle monitoring", data["description"])
            self.assertEqual(data["tools"], ["*"])
            self.assertEqual(set(data["hooks"]), {"agentSpawn", "userPromptSubmit", "preToolUse", "postToolUse", "stop"})
            self.assertEqual(set(detect_kiro_config(home).hook_events), set(KIRO_EVENTS))
            self.assertFalse(install_kiro_hooks(config_path=config, log_path=log, python_executable="python3").changed)
            self.assertTrue(uninstall_kiro_hooks(config_path=config, log_path=log).changed)
            self.assertFalse(config.exists())

    def test_install_refuses_unmanaged_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "sidepulse.json"
            config.write_text('{"name": "mine"}\n')
            with self.assertRaisesRegex(ValueError, "unmanaged Kiro agent"):
                install_kiro_hooks(config_path=config, log_path=Path(tmp) / "kiro.jsonl")

    def test_kiro_payload_aliases(self) -> None:
        event = parse_log_line("kiro", json.dumps({"hook_event_name": "agentSpawn", "session_id": "session-1", "cwd": "/repo"}))
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.event_name, "SessionStart")
        stopped = parse_log_line("kiro", json.dumps({"hook_event_name": "stop", "session_id": "session-1", "assistant_response": "Done"}))
        self.assertIsNotNone(stopped)
        assert stopped is not None
        self.assertEqual(stopped.raw["last_assistant_message"], "Done")
