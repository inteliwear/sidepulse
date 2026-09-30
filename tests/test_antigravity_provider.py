from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from sidepulse.antigravity_hook import normalize_payload, response_for_event
from sidepulse.install import install_antigravity_hooks, uninstall_antigravity_hooks
from sidepulse.models import AgentMode
from sidepulse.collector import AgentMonitor, SourceSpec
from sidepulse.providers import detect_antigravity_config


class AntigravityProviderTests(unittest.TestCase):
    def test_installer_preserves_other_integrations_and_is_detectable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            config = home / ".gemini" / "config" / "hooks.json"
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({"other-integration": {"enabled": True}}))
            log = home / "state" / "antigravity.jsonl"

            result = install_antigravity_hooks(
                config_path=config,
                log_path=log,
                python_executable="python3",
            )

            self.assertTrue(result.changed)
            data = json.loads(config.read_text())
            self.assertIn("other-integration", data)
            self.assertEqual(
                set(data["sidepulse-agent-monitor"]),
                {"PreInvocation", "PostInvocation", "PreToolUse", "PostToolUse", "Stop"},
            )
            detected = detect_antigravity_config(home)
            self.assertTrue(detected.hooks_enabled)
            self.assertEqual(detected.log_paths, (log,))
            command = data["sidepulse-agent-monitor"]["Stop"][0]["command"]
            self.assertIn("hook_entry.py", command)
            self.assertIn("--provider antigravity", command)
            self.assertIn("--event Stop", command)

    def test_uninstaller_removes_only_sidepulse_integration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "hooks.json"
            log = root / "antigravity.jsonl"
            config.write_text(
                json.dumps(
                    {
                        "other-integration": {"enabled": True},
                        "sidepulse-agent-monitor": {"Stop": []},
                    }
                )
            )

            uninstall_antigravity_hooks(config_path=config, log_path=log)

            self.assertEqual(
                json.loads(config.read_text()),
                {"other-integration": {"enabled": True}},
            )

    def test_quota_error_stop_completes_the_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = root / "antigravity.jsonl"
            normalized = normalize_payload(
                "Stop",
                {
                    "conversationId": "conversation-1",
                    "fullyIdle": True,
                    "terminationReason": "ERROR",
                    "error": "RESOURCE_EXHAUSTED: Individual quota reached",
                    "workspacePaths": ["/tmp/project"],
                },
            )
            normalized["logged_at"] = datetime.now(timezone.utc).isoformat()
            log.write_text(json.dumps(normalized) + "\n")

            snapshot = AgentMonitor(
                sources=(SourceSpec("antigravity", log),),
                stale_after_seconds=10**9,
            ).snapshot()

            self.assertEqual(snapshot.aggregate.mode, AgentMode.COMPLETED)
            self.assertIn("quota reached", snapshot.statuses[0].message)

    def test_background_stop_remains_working(self) -> None:
        normalized = normalize_payload(
            "Stop",
            {"conversationId": "conversation-1", "fullyIdle": False},
        )
        self.assertEqual(normalized["hook_event_name"], "PostToolUse")

    def test_hook_responses_preserve_antigravity_contract(self) -> None:
        self.assertEqual(response_for_event("PreToolUse"), {"decision": "allow"})
        self.assertEqual(response_for_event("PreInvocation"), {"injectSteps": []})
        self.assertEqual(response_for_event("Stop"), {"decision": ""})

    def test_shared_hook_entry_writes_antigravity_protocol_response(self) -> None:
        import io
        import os
        import sys
        from contextlib import redirect_stdout
        from unittest.mock import patch

        from sidepulse.hook import hook_log_main

        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "antigravity.jsonl"
            buffer = io.StringIO()
            with (
                patch.dict(os.environ, {"SIDEPULSE_DISABLE_EVENT_SOCKET": "1"}),
                patch.object(sys, "stdin", io.StringIO('{"conversationId":"c1"}')),
                redirect_stdout(buffer),
            ):
                code = hook_log_main("antigravity", log, event="PreToolUse")

            self.assertEqual(code, 0)
            self.assertEqual(json.loads(buffer.getvalue().strip()), {"decision": "allow"})
            self.assertTrue(log.exists())
            self.assertIn("PreToolUse", log.read_text())


if __name__ == "__main__":
    unittest.main()
