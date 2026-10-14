from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from sidepulse.cli import build_parser
from sidepulse.collector import LiveAgentMonitor, default_sources
from sidepulse.copilot_hook import normalize_payload
from sidepulse.hook import hook_log_main
from sidepulse.install import install_copilot_hooks, uninstall_copilot_hooks
from sidepulse.ipc import HookEventServer
from sidepulse.models import AgentMode
from sidepulse.origin import ProcessInfo, origin_from_environment, origin_from_processes
from sidepulse.providers import COPILOT_EVENTS, default_copilot_hook_config_path, detect_copilot_config, parse_log_line
from sidepulse.settings import AgentMonitorSettings


class CopilotInstallationTests(unittest.TestCase):
    def test_install_refresh_uninstall_preserve_other_hooks_and_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / ".copilot/hooks/sidepulse.json"
            config.parent.mkdir(parents=True)
            original = {"version": 1, "description": "Existing observer", "hooks": {
                "agentStop": [{"type": "command", "bash": "notify.sh"}, "future-hook"],
                "customEvent": [],
            }}
            config.write_text(json.dumps(original))
            log = root / "state/copilot.jsonl"
            first = install_copilot_hooks(config_path=config, log_path=log)
            self.assertTrue(first.changed)
            self.assertEqual(json.loads(first.backup_path.read_text()), original)
            self.assertFalse(first.backup_path.name.endswith(".json"))
            data = json.loads(config.read_text())
            self.assertEqual(data["hooks"]["agentStop"][:2], original["hooks"]["agentStop"])
            self.assertNotIn("permissionRequest", data["hooks"])
            self.assertEqual(data["hooks"]["notification"][-1]["matcher"],
                             "permission_prompt|elicitation_dialog")
            self.assertFalse(install_copilot_hooks(config_path=config, log_path=log).changed)
            replacement = root / "other/copilot.jsonl"
            install_copilot_hooks(config_path=config, log_path=replacement)
            detected = detect_copilot_config(root)
            self.assertTrue(detected.hooks_enabled)
            self.assertEqual(detected.log_paths, (replacement,))
            self.assertEqual(set(detected.hook_events), set(COPILOT_EVENTS))
            uninstall_copilot_hooks(config_path=config, log_path=replacement)
            self.assertEqual(json.loads(config.read_text()), original)

    def test_dry_run_and_invalid_config_never_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "hooks/sidepulse.json"
            log = root / "state/copilot.jsonl"
            self.assertTrue(install_copilot_hooks(config_path=config, log_path=log, dry_run=True).changed)
            self.assertFalse(config.parent.exists())
            self.assertFalse(log.parent.exists())
            config.parent.mkdir()
            for text in ('[]', '{"version":2}', '{"hooks":[]}', '{"hooks":{"preToolUse":{}}}'):
                config.write_text(text)
                with self.assertRaises(ValueError):
                    install_copilot_hooks(config_path=config, log_path=log)
                self.assertEqual(config.read_text(), text)

    def test_uninstall_removes_owned_file_and_preserves_user_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / ".copilot/hooks/sidepulse.json"
            settings = root / ".copilot/settings.json"
            config.parent.mkdir(parents=True)
            settings.write_text('{"disableAllHooks":true}')
            log = root / "copilot.jsonl"
            install_copilot_hooks(config_path=config, log_path=log)
            self.assertFalse(detect_copilot_config(root).hooks_enabled)
            self.assertEqual(settings.read_text(), '{"disableAllHooks":true}')
            self.assertTrue(uninstall_copilot_hooks(config_path=config, log_path=log, dry_run=True).changed)
            self.assertTrue(config.exists())
            uninstall_copilot_hooks(config_path=config, log_path=log)
            self.assertFalse(config.exists())
            self.assertFalse(uninstall_copilot_hooks(config_path=config, log_path=log).changed)

    def test_install_and_uninstall_preserve_unrelated_hook_entry_scripts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "hooks.json"
            original = {"version": 1, "hooks": {"agentStop": [{
                "type": "command",
                "bash": "python /tmp/custom-observer/hook_entry.py --provider copilot",
            }]}}
            config.write_text(json.dumps(original))
            log = root / "copilot.jsonl"
            install_copilot_hooks(config_path=config, log_path=log)
            self.assertEqual(json.loads(config.read_text())["hooks"]["agentStop"][0],
                             original["hooks"]["agentStop"][0])
            uninstall_copilot_hooks(config_path=config, log_path=log)
            self.assertEqual(json.loads(config.read_text()), original)

    def test_doctor_respects_disabled_file_and_unsupported_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / ".copilot/hooks/sidepulse.json"
            install_copilot_hooks(config_path=config, log_path=root / "copilot.jsonl")
            data = json.loads(config.read_text())
            data["disableAllHooks"] = True
            config.write_text(json.dumps(data))
            self.assertFalse(detect_copilot_config(root).hooks_enabled)
            self.assertFalse(install_copilot_hooks(config_path=config, log_path=root / "copilot.jsonl").changed)
            self.assertTrue(json.loads(config.read_text())["disableAllHooks"])
            data["disableAllHooks"] = False
            data["version"] = 2
            config.write_text(json.dumps(data))
            self.assertFalse(detect_copilot_config(root).hooks_enabled)

    def test_copilot_home_and_cli_support(self):
        with patch.dict(os.environ, {"COPILOT_HOME": "/tmp/custom-copilot"}):
            self.assertEqual(default_copilot_hook_config_path(), Path("/tmp/custom-copilot/hooks/sidepulse.json"))
            self.assertEqual(default_copilot_hook_config_path(Path("/tmp/user")),
                             Path("/tmp/user/.copilot/hooks/sidepulse.json"))
        for command in ("install", "uninstall", "status"):
            argv = [command] + (["copilot"] if command != "status" else [])
            args = build_parser().parse_args(argv + ["--copilot-log", "/tmp/copilot.jsonl"])
            self.assertEqual(args.copilot_log, Path("/tmp/copilot.jsonl"))
        self.assertIn("copilot", [s.provider for s in default_sources(AgentMonitorSettings())])


class CopilotEventTests(unittest.TestCase):
    def ingest(self, monitor, payload):
        record = parse_log_line("copilot", json.dumps(payload))
        self.assertIsNotNone(record)
        monitor.ingest_record(record)

    def test_native_events_drive_live_status_per_session(self):
        monitor = LiveAgentMonitor()
        timestamp = int(datetime.now(timezone.utc).timestamp() * 1000)
        common = {"sessionId": "copilot-session", "timestamp": timestamp, "cwd": "/tmp/project"}
        scenarios = (
            ("userPromptSubmitted", {"prompt": "Fix the tests"}, AgentMode.WORKING),
            ("sessionStart", {"initialPrompt": "Fix the tests"}, AgentMode.WORKING),
            ("preToolUse", {"toolName": "bash", "toolArgs": '{"command":"pytest"}'}, AgentMode.TOOL_RUNNING),
            ("postToolUse", {"toolName": "bash", "toolResult": {"resultType": "success"}}, AgentMode.WORKING),
            ("preToolUse", {"toolName": "ask_user"}, AgentMode.WAITING_FOR_INPUT),
            ("postToolUse", {"toolName": "ask_user", "toolResult": {"resultType": "success"}}, AgentMode.WORKING),
            ("notification", {"notification_type": "permission_prompt", "message": "Run command?"}, AgentMode.WAITING_FOR_INPUT),
            ("postToolUse", {"toolName": "bash"}, AgentMode.WORKING),
            ("postToolUseFailure", {"toolName": "bash", "error": "failed"}, AgentMode.BLOCKED_ERROR),
            ("agentStop", {}, AgentMode.COMPLETED),
        )
        for event, fields, expected in scenarios:
            with self.subTest(event=event, fields=fields):
                payload = normalize_payload(event, common | fields)
                self.ingest(monitor, payload)
                snapshot = monitor.snapshot()
                self.assertEqual(snapshot.aggregate.mode, expected)
                self.assertEqual(snapshot.statuses[0].session_id, "copilot-session")
                self.assertAlmostEqual(snapshot.statuses[0].updated_at.timestamp(), timestamp / 1000, places=3)
        self.assertEqual(len(monitor.snapshot().statuses), 1)

    def test_pascalcase_payloads_and_session_end_reasons(self):
        timestamp = datetime.now(timezone.utc).isoformat()
        payload = {"timestamp": timestamp, "session_id": "ide-session", "cwd": "/tmp/idea"}
        self.assertEqual(normalize_payload("PreToolUse", payload | {"tool_name": "Bash"})["hook_event_name"], "PreToolUse")
        for reason, expected in (("complete", "SessionEnd"), ("abort", "Interrupt"),
                                 ("user_exit", "Interrupt"), ("error", "StopFailure"),
                                 ("timeout", "StopFailure")):
            self.assertEqual(normalize_payload("sessionEnd", payload | {"reason": reason})["hook_event_name"], expected)
        monitor = LiveAgentMonitor()
        self.ingest(monitor, normalize_payload("userPromptSubmitted", payload))
        self.ingest(monitor, normalize_payload("errorOccurred", payload | {"error": {"message": "network failed"}}))
        self.assertEqual(monitor.snapshot().aggregate.mode, AgentMode.BLOCKED_ERROR)
        # Explicit handoff markers are supported with the same line-based
        # syntax as the existing providers.
        self.ingest(monitor, normalize_payload("agentStop", payload | {"response": "Choose a region.\n<!-- sidepulse:ask -->"}))
        self.assertEqual(monitor.snapshot().aggregate.mode, AgentMode.WAITING_FOR_INPUT)

    def test_app_and_ide_sessions_remain_distinct_and_aggregate(self):
        monitor = LiveAgentMonitor()
        for session, event, fields in (
            ("app", "preToolUse", {"toolName": "bash"}),
            ("ide", "notification", {"notification_type": "elicitation_dialog"}),
        ):
            self.ingest(monitor, normalize_payload(event, {"sessionId": session} | fields))
        self.assertEqual(len(monitor.snapshot().statuses), 2)
        self.assertEqual(monitor.snapshot().aggregate.mode, AgentMode.WAITING_FOR_INPUT)
        self.ingest(monitor, normalize_payload("agentStop", {"sessionId": "ide"}))
        self.assertEqual(monitor.snapshot().aggregate.mode, AgentMode.TOOL_RUNNING)

    def test_installed_shell_commands_log_and_deliver_to_live_socket(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "hooks.json"
            log = root / "copilot.jsonl"
            socket = root / "state/sidepulse/agent-monitor/events.sock"
            monitor = LiveAgentMonitor()
            install_copilot_hooks(config_path=config, log_path=log, python_executable=sys.executable)
            hooks = json.loads(config.read_text())["hooks"]
            completed = threading.Event()

            def receive(provider, row):
                self.ingest(monitor, row)
                if row.get("hook_event_name") == "Stop":
                    completed.set()

            server = HookEventServer(receive, socket_path=socket)
            server.start()
            try:
                for event in ("userPromptSubmitted", "preToolUse", "agentStop"):
                    command = hooks[event][0]["bash"]
                    result = subprocess.run(command, shell=True, input=json.dumps({"sessionId": "smoke", "toolName": "bash"}),
                                            text=True, capture_output=True, timeout=5,
                                            env=os.environ | {"HOME": str(root),
                                                              "XDG_CONFIG_HOME": str(root / ".config"),
                                                              "XDG_STATE_HOME": str(root / "state"),
                                                              "SIDEPULSE_DISABLE_EVENT_SOCKET": "0",
                                                              "SIDEPULSE_AGENT_ORIGIN": "GitHub Copilot App"})
                    self.assertEqual(result.returncode, 0)
                    self.assertEqual(result.stdout, "")
                    self.assertEqual(result.stderr, "")
                self.assertTrue(completed.wait(3), "Hook command did not reach the live event socket")
            finally:
                server.stop()
            rows = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual([row["hook_event_name"] for row in rows], ["UserPromptSubmit", "PreToolUse", "Stop"])
            self.assertEqual(monitor.snapshot().aggregate.mode, AgentMode.COMPLETED)

    def test_hook_never_returns_an_approval_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = io.StringIO()
            with patch.object(sys, "stdin", io.StringIO('{"sessionId":"s","toolName":"bash"}')), \
                 patch.dict(os.environ, {"SIDEPULSE_DISABLE_EVENT_SOCKET": "1", "XDG_STATE_HOME": tmp}), redirect_stdout(output):
                code = hook_log_main("copilot", Path(tmp) / "copilot.jsonl", event="preToolUse")
            self.assertEqual(code, 0)
            self.assertEqual(output.getvalue(), "")

    def test_origins_identify_the_copilot_surfaces(self):
        app = origin_from_environment("copilot", {"__CFBundleIdentifier": "com.github.githubapp"})
        self.assertEqual(app.label, "GitHub Copilot App")
        ide = origin_from_environment("copilot", {"__CFBundleIdentifier": "com.jetbrains.intellij"})
        self.assertEqual(ide.label, "Copilot in JetBrains IDE")
        process = ProcessInfo(1, 0, "idea", "/Applications/IntelliJ IDEA.app/Contents/MacOS/idea")
        self.assertEqual(origin_from_processes("copilot", (process,)).label, "Copilot in JetBrains IDE")


if __name__ == "__main__":
    unittest.main()
