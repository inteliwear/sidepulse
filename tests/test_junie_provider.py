from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from sidepulse.collector import (
    AgentMonitor,
    LiveAgentMonitor,
    SourceSpec,
    agent_status_from_dict,
)
from sidepulse.cli import build_parser
from sidepulse.hook import normalize_junie_payload
from sidepulse.install import install_junie_hooks, uninstall_junie_hooks
from sidepulse.models import AgentMode, AgentStatus
from sidepulse.providers import JUNIE_MONITOR_EVENTS, detect_junie_config, parse_log_line
from sidepulse.jetbrains import JetBrainsIDE, JetBrainsLaunch
from sidepulse.session_actions import (
    SESSION_OPEN_JETBRAINS,
    IdeOpenTarget,
    available_session_open_actions,
    default_session_open_action,
    session_jetbrains_target,
    session_open_action_label,
    session_open_target,
    session_resume_command,
)


class JunieProviderTests(unittest.TestCase):
    def test_junie_project_path_takes_precedence_over_hook_cwd(self) -> None:
        record = parse_log_line(
            "junie",
            json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "junie-session",
                    "cwd": "/Users/example/.junie",
                    "project_path": "/Users/example/work/project",
                }
            ),
        )

        self.assertIsNotNone(record)
        self.assertEqual(record.cwd, "/Users/example/work/project")

    def test_junie_index_supplies_session_title_and_project_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            project = home / "work" / "project"
            index = home / ".junie" / "sessions" / "index.jsonl"
            index.parent.mkdir(parents=True)
            index.write_text(
                json.dumps(
                    {
                        "sessionId": "junie-session",
                        "taskName": "Improve session opening",
                        "projectDir": str(project),
                    }
                )
                + "\n"
            )
            with patch("sidepulse.collector.Path.home", return_value=home):
                record = parse_log_line(
                    "junie",
                    json.dumps(
                        {
                            "hook_event_name": "UserPromptSubmit",
                            "session_id": "junie-session",
                            "cwd": "/Users/example/.junie",
                            "project_path": str(project),
                            "prompt": "A fallback prompt title",
                        }
                    ),
                )
                monitor = LiveAgentMonitor(sources=())
                monitor.ingest_record(record)
                status = monitor.statuses_by_key[record.status_key]
                restored = agent_status_from_dict(
                    {
                        **status.to_dict(),
                        "cwd": "/Users/example/.junie",
                        "display_name": "Junie session junie-se",
                    }
                )

            self.assertIn("Improve session opening", status.display_name)
            self.assertNotIn("A fallback prompt title", status.display_name)
            self.assertEqual(restored.cwd, str(project))
            self.assertIn("Improve session opening", restored.display_name)

    def test_junie_jetbrains_action_uses_index_project_and_labels_selected_ide(self) -> None:
        status = AgentStatus(
            provider="junie",
            agent_id="junie:session:session-123",
            display_name="Junie session",
            mode=AgentMode.COMPLETED,
            updated_at=datetime.now(timezone.utc),
            event_name="Stop",
            session_id="session-123",
            cwd="/tmp/hook-directory",
        )
        ide = JetBrainsIDE(
            "IntelliJ IDEA", "com.jetbrains.intellij", "JetBrains", "IntelliJIdea"
        )
        with (
            patch(
                "sidepulse.session_actions.junie_session_project_dir",
                return_value="/tmp/indexed-project",
            ),
            patch(
                "sidepulse.session_actions.jetbrains_launch_for_project",
                return_value=JetBrainsLaunch(ide, "/Applications/IntelliJ IDEA.app"),
            ) as resolve,
        ):
            expected = IdeOpenTarget(
                ide.bundle_id,
                "/tmp/indexed-project",
                "/Applications/IntelliJ IDEA.app",
            )
            self.assertEqual(session_jetbrains_target(status), expected)
            self.assertEqual(
                session_open_target(status, SESSION_OPEN_JETBRAINS),
                ("ide", expected),
            )
            self.assertEqual(
                session_open_action_label(status, SESSION_OPEN_JETBRAINS),
                "Open in IntelliJ IDEA",
            )
            self.assertEqual(
                default_session_open_action(
                    AgentStatus(
                        **{
                            **status.__dict__,
                            "origin": "Junie in JetBrains IDE",
                        }
                    )
                ),
                SESSION_OPEN_JETBRAINS,
            )
        resolve.assert_called_with("/tmp/indexed-project")

    def test_junie_jetbrains_action_falls_back_to_cwd_and_hides_when_unavailable(
        self,
    ) -> None:
        status = AgentStatus(
            provider="junie",
            agent_id="junie:session:session-456",
            display_name="Junie session",
            mode=AgentMode.COMPLETED,
            updated_at=datetime.now(timezone.utc),
            event_name="Stop",
            session_id="session-456",
            cwd="/tmp/project-from-cwd",
        )
        ide = JetBrainsIDE(
            "IntelliJ IDEA", "com.jetbrains.intellij", "JetBrains", "IntelliJIdea"
        )
        with (
            patch(
                "sidepulse.session_actions.junie_session_project_dir", return_value=None
            ),
            patch(
                "sidepulse.session_actions.jetbrains_launch_for_project",
                return_value=JetBrainsLaunch(ide, None),
            ) as resolve,
        ):
            self.assertEqual(
                session_jetbrains_target(status),
                IdeOpenTarget(ide.bundle_id, status.cwd, None),
            )
            resolve.assert_called_once_with(status.cwd)

        with (
            patch(
                "sidepulse.session_actions.junie_session_project_dir", return_value=None
            ),
            patch(
                "sidepulse.session_actions.jetbrains_launch_for_project",
                return_value=None,
            ),
        ):
            self.assertNotIn(
                SESSION_OPEN_JETBRAINS,
                available_session_open_actions(status),
            )

        claude_status = AgentStatus(
            **{**status.__dict__, "provider": "claude"}
        )
        self.assertIsNone(session_open_target(claude_status, SESSION_OPEN_JETBRAINS))
    def test_installer_preserves_existing_config_and_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            config = home / ".junie" / "config.json"
            config.parent.mkdir(parents=True)
            config.write_text(
                json.dumps(
                    {
                        "model": "sonnet",
                        "hooks": {
                            "PreToolUse": [
                                {
                                    "matcher": "Bash",
                                    "hooks": [
                                        {"type": "command", "command": "python guard.py"}
                                    ],
                                }
                            ]
                        },
                    }
                )
            )
            log = home / "state" / "junie.jsonl"

            result = install_junie_hooks(
                config_path=config,
                log_path=log,
                python_executable="python3",
            )

            self.assertTrue(result.changed)
            data = json.loads(config.read_text())
            self.assertEqual(data["model"], "sonnet")
            self.assertEqual(set(data["hooks"]), set(JUNIE_MONITOR_EVENTS))
            self.assertNotIn("PermissionRequest", data["hooks"])
            pre_tool_commands = [
                hook["command"]
                for entry in data["hooks"]["PreToolUse"]
                for hook in entry["hooks"]
            ]
            self.assertIn("python guard.py", pre_tool_commands)
            self.assertTrue(any("--provider junie" in command for command in pre_tool_commands))
            self.assertTrue(log.exists())

            second = install_junie_hooks(
                config_path=config,
                log_path=log,
                python_executable="python3",
            )
            self.assertFalse(second.changed)

            detected = detect_junie_config(home)
            self.assertTrue(detected.hooks_enabled)
            self.assertEqual(detected.hook_events, tuple(sorted(JUNIE_MONITOR_EVENTS)))
            self.assertEqual(detected.log_paths, (log,))

    def test_uninstaller_removes_only_sidepulse_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.json"
            log = root / "junie.jsonl"
            config.write_text(
                json.dumps(
                    {
                        "model": "gpt",
                        "hooks": {
                            "Stop": [
                                {
                                    "hooks": [
                                        {"type": "command", "command": "python notify.py"}
                                    ]
                                }
                            ]
                        },
                    }
                )
            )
            install_junie_hooks(config_path=config, log_path=log)

            result = uninstall_junie_hooks(config_path=config, log_path=log)

            self.assertTrue(result.changed)
            data = json.loads(config.read_text())
            self.assertEqual(data["model"], "gpt")
            self.assertEqual(
                data["hooks"]["Stop"],
                [{"hooks": [{"type": "command", "command": "python notify.py"}]}],
            )

    def test_junie_events_map_to_sidepulse_states(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "junie.jsonl"
            timestamp = datetime.now(timezone.utc).isoformat()
            rows = [
                {
                    "logged_at": timestamp,
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "junie-session",
                    "cwd": "/tmp/project",
                    "prompt": "fix the test",
                },
                {
                    "logged_at": timestamp,
                    "hook_event_name": "PreToolUse",
                    "tool_name": "Bash",
                    "tool_input": {"command": "pytest"},
                },
                {
                    "logged_at": timestamp,
                    "hook_event_name": "Stop",
                    "last_assistant_message": "Fixed the test.",
                },
            ]
            normalized_rows = []
            for row in rows:
                normalized = normalize_junie_payload(row, log, process_id=123)
                normalized_rows.append(normalized)
                with log.open("a") as handle:
                    handle.write(json.dumps(normalized) + "\n")

            self.assertEqual(normalized_rows[-1]["session_id"], "junie-session")
            self.assertEqual(normalized_rows[-1]["cwd"], "/tmp/project")

            snapshot = AgentMonitor(
                sources=(SourceSpec("junie", log),),
                stale_after_seconds=3600,
            ).snapshot()

            self.assertEqual(snapshot.aggregate.mode, AgentMode.COMPLETED)
            self.assertEqual(snapshot.statuses[0].provider, "junie")
            self.assertEqual(snapshot.statuses[0].session_id, "junie-session")
            self.assertEqual(
                snapshot.statuses[0].display_name,
                "project: fix the test (junie-se)",
            )

    def test_cli_accepts_junie_and_custom_log_path(self) -> None:
        args = build_parser().parse_args(
            ["install", "junie", "--junie-log", "/tmp/custom-junie.jsonl"]
        )

        self.assertEqual(args.provider, "junie")
        self.assertEqual(args.junie_log, Path("/tmp/custom-junie.jsonl"))

    def test_session_can_resume_in_junie_cli(self) -> None:
        status = AgentStatus(
            provider="junie",
            agent_id="junie:session:session-123",
            display_name="Junie session",
            mode=AgentMode.COMPLETED,
            updated_at=datetime.now(timezone.utc),
            event_name="Stop",
            session_id="session-123",
            cwd="/tmp/project path",
        )

        self.assertEqual(
            session_resume_command(status),
            "cd '/tmp/project path' && junie --session-id=session-123 --resume",
        )


if __name__ == "__main__":
    unittest.main()
