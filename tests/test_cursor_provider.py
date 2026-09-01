from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from sidepulse.collector import AgentMonitor, SourceSpec
from sidepulse.cursor_hook import normalize_payload
from sidepulse.install import install_cursor_hooks, uninstall_cursor_hooks
from sidepulse.models import AgentMode
from sidepulse.providers import detect_cursor_config


class CursorProviderTests(unittest.TestCase):
    def test_installer_preserves_existing_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            config = home / ".cursor" / "hooks.json"
            config.parent.mkdir(parents=True)
            config.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "hooks": {"stop": [{"command": "python notify.py"}]},
                    }
                )
            )
            log = home / "state" / "cursor.jsonl"

            install_cursor_hooks(
                config_path=config,
                log_path=log,
                python_executable="python3",
            )

            data = json.loads(config.read_text())
            stop_commands = [entry["command"] for entry in data["hooks"]["stop"]]
            self.assertIn("python notify.py", stop_commands)
            # Cursor routes through the shared hook_entry dispatcher, like every
            # other provider, so the hook keeps working when sidepulse is not on
            # the interpreter's import path.
            self.assertTrue(any("hook_entry.py" in item for item in stop_commands))
            self.assertTrue(any("--provider cursor" in item for item in stop_commands))
            self.assertTrue(any("--event stop" in item for item in stop_commands))
            detected = detect_cursor_config(home)
            self.assertTrue(detected.hooks_enabled)
            self.assertEqual(detected.log_paths, (log,))

    def test_uninstaller_removes_only_sidepulse_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "hooks.json"
            log = root / "cursor.jsonl"
            config.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "hooks": {"stop": [{"command": "python notify.py"}]},
                    }
                )
            )
            install_cursor_hooks(config_path=config, log_path=log)

            uninstall_cursor_hooks(config_path=config, log_path=log)

            data = json.loads(config.read_text())
            self.assertEqual(data["hooks"]["stop"], [{"command": "python notify.py"}])
            self.assertFalse(
                any(
                    "sidepulse.cursor_hook" in str(entry.get("command") or "")
                    for entries in data["hooks"].values()
                    for entry in entries
                )
            )

    def test_installer_preserves_unknown_hook_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "hooks.json"
            log = root / "cursor.jsonl"
            config.write_text(
                json.dumps({"version": 1, "hooks": {"stop": ["future-hook"]}})
            )

            install_cursor_hooks(config_path=config, log_path=log)

            self.assertIn("future-hook", json.loads(config.read_text())["hooks"]["stop"])

    def test_prompt_and_stop_map_to_working_then_completed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "cursor.jsonl"
            now = datetime.now(timezone.utc).isoformat()
            prompt = normalize_payload(
                "beforeSubmitPrompt",
                {
                    "conversation_id": "cursor-conversation",
                    "workspace_roots": ["/tmp/project"],
                    "prompt": "fix the test",
                },
            )
            prompt["logged_at"] = now
            stopped = normalize_payload(
                "stop",
                {"conversation_id": "cursor-conversation", "status": "completed"},
            )
            stopped["logged_at"] = now
            log.write_text(json.dumps(prompt) + "\n" + json.dumps(stopped) + "\n")

            snapshot = AgentMonitor(
                sources=(SourceSpec("cursor", log),),
                stale_after_seconds=3600,
            ).snapshot()

            self.assertEqual(snapshot.aggregate.mode, AgentMode.COMPLETED)
            self.assertEqual(snapshot.statuses[0].origin, "Cursor")

    def test_shell_hook_maps_to_tool_activity(self) -> None:
        normalized = normalize_payload(
            "beforeShellExecution",
            {"conversation_id": "cursor-conversation", "command": "pytest"},
        )
        self.assertEqual(normalized["hook_event_name"], "PreToolUse")
        self.assertEqual(normalized["tool_name"], "shell")

    def test_generic_tool_hooks_map_to_activity_and_failure(self) -> None:
        before = normalize_payload(
            "preToolUse",
            {
                "conversation_id": "cursor-conversation",
                "tool_name": "read_file",
                "tool_input": {"path": "README.md"},
            },
        )
        failed = normalize_payload(
            "postToolUseFailure",
            {
                "conversation_id": "cursor-conversation",
                "tool_name": "read_file",
                "error_message": "permission denied",
            },
        )

        self.assertEqual(before["hook_event_name"], "PreToolUse")
        self.assertEqual(before["tool_name"], "read_file")
        self.assertEqual(failed["hook_event_name"], "StopFailure")
        self.assertEqual(failed["message"], "permission denied")


if __name__ == "__main__":
    unittest.main()
