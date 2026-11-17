from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from hook_assertions import command_text, fail_open

from sidepulse import install as install_mod
from sidepulse import providers as providers_mod
from sidepulse.cli import (
    build_sidepulse_parser,
    cmd_doctor,
    cmd_uninstall,
    install_hook_results,
)
from sidepulse.collector import AgentMonitor, SourceSpec, default_sources
from sidepulse.hook import hook_log_main
from sidepulse.models import AgentMode, AgentStatus
from sidepulse.origin import ProcessInfo, detect_agent_origin, origin_from_processes
from sidepulse.program_adapter import hook_report
from sidepulse.program_presentation import ProgramMonitor
from sidepulse.program_status import ProgramState, validate_context, validate_status
from sidepulse.providers import (
    extract_log_paths_from_command,
    parse_log_line,
)
from sidepulse.session_actions import session_resume_command
from sidepulse.settings import AgentMonitorSettings

MOSHI_HOOKS_JSON = (
    json.dumps(
        {
            "moshi-hook": {
                "PreInvocation": [
                    {
                        "command": "'/opt/homebrew/opt/moshi-hook/bin/moshi-hook' antigravity-hook PreInvocation",
                        "timeout": 5,
                        "type": "command",
                    }
                ],
                "Stop": [
                    {
                        "command": "'/opt/homebrew/opt/moshi-hook/bin/moshi-hook' antigravity-hook Stop",
                        "timeout": 5,
                        "type": "command",
                    }
                ],
                "enabled": True,
            }
        },
        indent=2,
    )
    + "\n"
)


def synthetic_agy_payload(
    hook_event_name: str,
    *,
    conversation_id: str = "00000000-0000-4000-8000-000000000001",
    cwd: str = "/work/demo-app",
    surface_dir: str = "antigravity-cli",
    last_user_input: str = "Run the unit tests",
    **extra,
) -> dict:
    base = {
        "conversationId": conversation_id,
        "cwd": cwd,
        "workspacePaths": [cwd] if cwd else [],
        "transcriptPath": (
            f"/Users/example/.gemini/{surface_dir}/brain/{conversation_id}"
            "/.system_generated/logs/transcript_full.jsonl"
        ),
        "artifactDirectoryPath": f"/Users/example/.gemini/{surface_dir}/brain/{conversation_id}",
        "modelName": "gemini-3.8-flash-low",
        "lastUserInput": last_user_input,
        "hookEventName": hook_event_name,
    }
    base.update(extra)
    return base


class AntigravityInstallTests(unittest.TestCase):
    def test_install_preserves_moshi_entry_byte_for_byte(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            config = home / ".gemini" / "config" / "hooks.json"
            config.parent.mkdir(parents=True)
            config.write_text(MOSHI_HOOKS_JSON, encoding="utf-8")
            original_bytes = config.read_bytes()
            original_moshi_slice = MOSHI_HOOKS_JSON.strip()[1:-1].strip()
            log = home / "state" / "antigravity.jsonl"

            result = install_mod.install_antigravity_hooks(
                log_path=log,
                config_path=config,
                python_executable="python3",
            )

            self.assertTrue(result.changed)
            self.assertIsNotNone(result.backup_path)
            self.assertEqual(result.backup_path.read_bytes(), original_bytes)
            self.assertTrue(log.exists())

            updated_text = config.read_text(encoding="utf-8")
            self.assertIn(original_moshi_slice, updated_text)

            data = json.loads(updated_text)
            self.assertEqual(data["moshi-hook"], json.loads(MOSHI_HOOKS_JSON)["moshi-hook"])
            self.assertTrue(data["sidepulse"]["enabled"])
            for event_name in providers_mod.ANTIGRAVITY_MONITOR_EVENTS:
                entries = data["sidepulse"][event_name]
                self.assertEqual(len(entries), 1)
                if event_name in providers_mod.ANTIGRAVITY_GROUPED_EVENTS:
                    self.assertEqual(entries[0]["matcher"], "*")
                    cmd = entries[0]["hooks"][0]["command"]
                else:
                    self.assertNotIn("matcher", entries[0])
                    cmd = entries[0]["command"]
                self.assertTrue(fail_open(cmd), cmd)
                self.assertIn("--provider antigravity", command_text(cmd))
                self.assertIn(f"--event {event_name}", command_text(cmd))
                self.assertEqual(extract_log_paths_from_command(cmd), (log,))

            removed = install_mod.uninstall_antigravity_hooks(log_path=log, config_path=config)
            self.assertTrue(removed.changed)
            self.assertEqual(config.read_bytes(), original_bytes)

    def test_install_twice_adds_no_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            config = providers_mod.default_antigravity_hook_config_path(home)
            log = home / "state" / "antigravity.jsonl"

            first = install_mod.install_antigravity_hooks(
                log_path=log,
                config_path=config,
                python_executable="python3",
            )
            first_bytes = config.read_bytes()
            second = install_mod.install_antigravity_hooks(
                log_path=log,
                config_path=config,
                python_executable="python3",
            )

            self.assertTrue(first.changed)
            self.assertFalse(second.changed)
            self.assertIsNone(second.backup_path)
            self.assertEqual(config.read_bytes(), first_bytes)

    def test_uninstall_removes_only_sidepulse_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            config = providers_mod.default_antigravity_hook_config_path(home)
            log = home / "state" / "antigravity.jsonl"
            config.parent.mkdir(parents=True)
            config.write_text(
                json.dumps(
                    {
                        "moshi-hook": json.loads(MOSHI_HOOKS_JSON)["moshi-hook"],
                        "sidepulse": {
                            "enabled": True,
                            "Stop": [{"type": "command", "command": "echo custom-keep"}],
                            "PreToolUse": [
                                {
                                    "matcher": "run_command",
                                    "hooks": [{"type": "command", "command": "echo guard-keep"}],
                                }
                            ],
                        },
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            install_mod.install_antigravity_hooks(
                log_path=log, config_path=config, python_executable="python3"
            )
            removed = install_mod.uninstall_antigravity_hooks(config_path=config)

            self.assertTrue(removed.changed)
            data = json.loads(config.read_text(encoding="utf-8"))
            self.assertEqual(data["moshi-hook"], json.loads(MOSHI_HOOKS_JSON)["moshi-hook"])
            self.assertEqual(
                data["sidepulse"]["Stop"],
                [{"type": "command", "command": "echo custom-keep"}],
            )
            self.assertEqual(
                data["sidepulse"]["PreToolUse"],
                [
                    {
                        "matcher": "run_command",
                        "hooks": [{"type": "command", "command": "echo guard-keep"}],
                    }
                ],
            )
            self.assertNotIn("SessionStart", data["sidepulse"])

    def test_uninstall_deletes_file_when_only_sidepulse_was_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            config = providers_mod.default_antigravity_hook_config_path(home)
            log = home / "state" / "antigravity.jsonl"
            install_mod.install_antigravity_hooks(
                log_path=log, config_path=config, python_executable="python3"
            )

            first = install_mod.uninstall_antigravity_hooks(log_path=log, config_path=config)
            second = install_mod.uninstall_antigravity_hooks(log_path=log, config_path=config)

            self.assertTrue(first.changed)
            self.assertFalse(config.exists())
            self.assertFalse(second.changed)

    def test_dry_run_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            config = providers_mod.default_antigravity_hook_config_path(home)
            log = home / "state" / "antigravity.jsonl"

            dry_install = install_mod.install_antigravity_hooks(
                log_path=log,
                config_path=config,
                dry_run=True,
                python_executable="python3",
            )
            self.assertTrue(dry_install.changed)
            self.assertTrue(dry_install.dry_run)
            self.assertFalse(config.exists())
            self.assertFalse(log.exists())

            config.parent.mkdir(parents=True)
            config.write_text(MOSHI_HOOKS_JSON, encoding="utf-8")
            install_mod.install_antigravity_hooks(
                log_path=log, config_path=config, python_executable="python3"
            )
            installed_bytes = config.read_bytes()

            dry_uninstall = install_mod.uninstall_antigravity_hooks(
                log_path=log,
                config_path=config,
                dry_run=True,
            )
            self.assertTrue(dry_uninstall.changed)
            self.assertTrue(dry_uninstall.dry_run)
            self.assertEqual(config.read_bytes(), installed_bytes)

    def test_malformed_config_is_refused_without_being_overwritten(self) -> None:
        for bad_content in ("{not valid json\n", "[1, 2, 3]\n", '"just-a-string"\n'):
            with self.subTest(content=bad_content), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                config = providers_mod.default_antigravity_hook_config_path(home)
                config.parent.mkdir(parents=True)
                config.write_bytes(bad_content.encode("utf-8"))
                log = home / "state" / "antigravity.jsonl"

                with self.assertRaises(ValueError):
                    install_mod.install_antigravity_hooks(log_path=log, config_path=config)
                self.assertEqual(config.read_text(encoding="utf-8"), bad_content)
                self.assertEqual(list(config.parent.glob("hooks.json.bak.*")), [])

                with self.assertRaises(ValueError):
                    install_mod.uninstall_antigravity_hooks(log_path=log, config_path=config)
                self.assertEqual(config.read_text(encoding="utf-8"), bad_content)

    def test_doctor_and_detection_report_config_events_and_disabled_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.assertFalse(providers_mod.detect_antigravity_config(home).exists)
            self.assertFalse(providers_mod.detect_antigravity_config(home).hooks_enabled)

            config = providers_mod.default_antigravity_hook_config_path(home)
            log = home / "state" / "antigravity.jsonl"
            install_mod.install_antigravity_hooks(
                log_path=log, config_path=config, python_executable="python3"
            )

            detected = providers_mod.detect_antigravity_config(home)
            self.assertTrue(detected.exists)
            self.assertTrue(detected.hooks_enabled)
            self.assertEqual(
                detected.hook_events, tuple(sorted(providers_mod.ANTIGRAVITY_MONITOR_EVENTS))
            )
            self.assertEqual(detected.log_paths, (log,))

            buffer = io.StringIO()
            args = build_sidepulse_parser().parse_args(["doctor", "--json"])
            with patch("sidepulse.providers.Path.home", return_value=home), redirect_stdout(buffer):
                self.assertEqual(cmd_doctor(args), 0)
            doctor_payload = json.loads(buffer.getvalue())
            agy_row = next(
                row for row in doctor_payload["providers"] if row["provider"] == "antigravity"
            )
            self.assertTrue(agy_row["hooks_enabled"])
            self.assertEqual(agy_row["log_paths"], [str(log)])

            # Disabled bundle is reported as not enabled.
            data = json.loads(config.read_text(encoding="utf-8"))
            data["sidepulse"]["enabled"] = False
            config.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            self.assertFalse(providers_mod.detect_antigravity_config(home).hooks_enabled)

    def test_cli_setup_uninstall_and_default_sources_wire_antigravity(self) -> None:
        parser = build_sidepulse_parser()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            config = providers_mod.default_antigravity_hook_config_path(home)
            custom_log = home / "custom" / "agy.jsonl"

            args = parser.parse_args(
                ["setup", "--agent", "antigravity", "--antigravity-log", str(custom_log)]
            )
            with patch(
                "sidepulse.install.default_antigravity_hook_config_path",
                return_value=config,
            ):
                results = install_hook_results(args)
            self.assertEqual([r.provider for r in results], ["antigravity"])
            self.assertEqual(results[0].log_path, custom_log)
            self.assertTrue(config.exists())

            uninstall_args = parser.parse_args(
                ["setup", "antigravity", "--uninstall", "--antigravity-log", str(custom_log)]
            )
            out = io.StringIO()
            with (
                patch(
                    "sidepulse.install.default_antigravity_hook_config_path",
                    return_value=config,
                ),
                redirect_stdout(out),
            ):
                self.assertEqual(cmd_uninstall(uninstall_args), 0)
            self.assertFalse(config.exists())

        providers = [source.provider for source in default_sources(AgentMonitorSettings())]
        self.assertIn("antigravity", providers)


class AntigravityMappingAndRuntimeTests(unittest.TestCase):
    def test_pre_tool_use_stdout_returns_ask_and_other_events_return_empty_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"SIDEPULSE_DISABLE_EVENT_SOCKET": "1"}
        ):
            log = Path(tmp) / "antigravity.jsonl"
            for event_name, expected_stdout in (
                ("PreToolUse", '{"decision":"ask"}\n'),
                ("SessionStart", "{}\n"),
                ("PreInvocation", "{}\n"),
                ("PostToolUse", "{}\n"),
                ("Stop", "{}\n"),
            ):
                payload = json.dumps(synthetic_agy_payload(event_name))
                buf = io.StringIO()
                with patch("sys.stdin", io.StringIO(payload)), redirect_stdout(buf):
                    rc = hook_log_main("antigravity", log, event=event_name)
                self.assertEqual(rc, 0)
                self.assertEqual(buf.getvalue(), expected_stdout)

            # Even hostile non-JSON stdin still produces valid JSON stdout for Antigravity.
            buf = io.StringIO()
            with patch("sys.stdin", io.StringIO("not-json")), redirect_stdout(buf):
                rc = hook_log_main("antigravity", log, event="PreToolUse")
            self.assertEqual(rc, 0)
            self.assertEqual(buf.getvalue(), '{"decision":"ask"}\n')

    @unittest.skipIf(os.name == "nt", "POSIX shell fallback check")
    def test_shell_command_emits_valid_json_when_python_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "antigravity.jsonl"
            pre_cmd = install_mod.antigravity_hook_command(
                "PreToolUse", log, python_executable="/nonexistent/python3"
            )
            stop_cmd = install_mod.antigravity_hook_command(
                "Stop", log, python_executable="/nonexistent/python3"
            )
            pre_res = subprocess.run(
                pre_cmd,
                shell=True,
                input="{}",
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            stop_res = subprocess.run(
                stop_cmd,
                shell=True,
                input="{}",
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(pre_res.returncode, 0)
            self.assertEqual(pre_res.stdout, '{"decision":"ask"}\n')
            self.assertEqual(stop_res.returncode, 0)
            self.assertEqual(stop_res.stdout, "{}\n")

    def test_step1_synthetic_payloads_map_to_expected_states(self) -> None:
        sequence = [
            (
                synthetic_agy_payload("SessionStart"),
                AgentMode.IDLE_READY,
                "idle",
                None,
                None,
            ),
            (
                synthetic_agy_payload(
                    "PreInvocation",
                    invocationNum=0,
                    initialNumSteps=1,
                    lastUserInput="Check test suite",
                ),
                AgentMode.WORKING,
                "working",
                None,
                None,
            ),
            (
                synthetic_agy_payload(
                    "PreToolUse",
                    stepIdx=2,
                    toolCall={
                        "id": "call_100",
                        "name": "run_command",
                        "args": {"CommandLine": "pytest -q"},
                    },
                ),
                AgentMode.TOOL_RUNNING,
                "working",
                "run_command",
                None,
            ),
            (
                synthetic_agy_payload(
                    "PostToolUse",
                    stepIdx=2,
                    error="",
                    toolCall={
                        "id": "call_100",
                        "name": "run_command",
                        "args": {"CommandLine": "pytest -q"},
                    },
                ),
                AgentMode.WORKING,
                "working",
                "run_command",
                None,
            ),
            (
                synthetic_agy_payload(
                    "PreToolUse",
                    stepIdx=4,
                    toolCall={
                        "id": "call_101",
                        "name": "ask_question",
                        "args": {
                            "questions": [
                                {
                                    "question": "Which target environment?",
                                    "options": ["staging", "prod"],
                                    "is_multi_select": False,
                                }
                            ]
                        },
                    },
                ),
                AgentMode.WAITING_FOR_INPUT,
                "blocked",
                "ask_question",
                "Which target environment?",
            ),
            (
                synthetic_agy_payload(
                    "PostToolUse",
                    stepIdx=4,
                    error="",
                    toolCall={"id": "call_101", "name": "ask_question", "args": {}},
                ),
                AgentMode.WORKING,
                "working",
                "ask_question",
                None,
            ),
            (
                synthetic_agy_payload(
                    "PostToolUse",
                    stepIdx=5,
                    error="file does not exist",
                    toolCall={
                        "id": "call_102",
                        "name": "read_file",
                        "args": {"Path": "/work/demo-app/missing.txt"},
                    },
                ),
                AgentMode.BLOCKED_ERROR,
                "error",
                "read_file",
                "file does not exist",
            ),
            (
                synthetic_agy_payload(
                    "Stop",
                    executionNum=0,
                    fullyIdle=True,
                    continuationActive=False,
                    error="",
                    terminationReason="NO_TOOL_CALL",
                ),
                AgentMode.COMPLETED,
                "done",
                None,
                None,
            ),
            (
                synthetic_agy_payload(
                    "Stop",
                    executionNum=0,
                    fullyIdle=True,
                    continuationActive=False,
                    error="",
                    terminationReason="USER_CANCELED",
                ),
                AgentMode.IDLE_READY,
                "idle",
                None,
                None,
            ),
            (
                synthetic_agy_payload(
                    "Stop",
                    executionNum=0,
                    fullyIdle=True,
                    continuationActive=False,
                    error="Quota exhausted",
                    terminationReason="ERROR",
                ),
                AgentMode.BLOCKED_ERROR,
                "error",
                None,
                "Quota exhausted",
            ),
        ]

        store = ProgramState()
        program_monitor = ProgramMonitor(store)
        base_time = datetime.now(timezone.utc) - timedelta(seconds=30)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                log = Path(tmp) / "antigravity.jsonl"
                for index, (payload, expected_mode, expected_state, expected_tool, expected_msg) in enumerate(
                    sequence
                ):
                    row = {
                        **payload,
                        "logged_at": (base_time + timedelta(seconds=index)).isoformat(),
                    }
                    with log.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(row) + "\n")

                    report = hook_report("antigravity", row)
                    self.assertIsNotNone(report)
                    validate_status(report["status"])
                    validate_context(report["context"])
                    self.assertEqual(report["status"]["state"], expected_state)
                    self.assertEqual(report["status"]["app"], "antigravity")
                    if expected_state == "blocked":
                        self.assertEqual(report["status"]["kind"], "question")
                    if expected_msg is not None:
                        self.assertEqual(report["status"]["msg"], expected_msg)
                    store.submit(report)

                    daemon_snapshot = program_monitor.snapshot()
                    self.assertEqual(daemon_snapshot.aggregate.mode, expected_mode)

                    collector_snapshot = AgentMonitor(
                        sources=(SourceSpec("antigravity", log),),
                        stale_after_seconds=3600,
                        idle_visible_seconds=3600,
                    ).snapshot()
                    self.assertEqual(collector_snapshot.aggregate.mode, expected_mode)
                    latest = collector_snapshot.statuses[0]
                    self.assertEqual(latest.provider, "antigravity")
                    self.assertEqual(latest.origin, "Antigravity CLI")
                    self.assertEqual(latest.cwd, "/work/demo-app")
                    self.assertEqual(latest.tool_name, expected_tool)
                    if index >= 1:
                        self.assertIn("Check test suite", latest.display_name)
        finally:
            store.close()

    def test_workspace_paths_fallback_and_surface_origins(self) -> None:
        for surface_dir, expected_origin in (
            ("antigravity-cli", "Antigravity CLI"),
            ("antigravity", "Antigravity App"),
            ("antigravity-ide", "Antigravity IDE"),
        ):
            payload = synthetic_agy_payload(
                "PreInvocation",
                cwd="",
                surface_dir=surface_dir,
            )
            payload["workspacePaths"] = ["/work/fallback-repo"]
            record = parse_log_line("antigravity", json.dumps(payload))
            self.assertIsNotNone(record)
            self.assertEqual(record.cwd, "/work/fallback-repo")
            self.assertEqual(record.origin, expected_origin)

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"SIDEPULSE_DISABLE_EVENT_SOCKET": "1"}, clear=False
        ):
            os.environ.pop("PLUGIN_ROOT", None)
            log = Path(tmp) / "antigravity.jsonl"
            empty_cwd_payload = json.dumps(
                synthetic_agy_payload("PreInvocation", cwd="", workspacePaths=[])
            )
            buf = io.StringIO()
            with (
                patch("os.getcwd", return_value="/work/interactive-tui"),
                patch("sys.stdin", io.StringIO(empty_cwd_payload)),
                redirect_stdout(buf),
            ):
                self.assertEqual(hook_log_main("antigravity", log, event="PreInvocation"), 0)
            logged = parse_log_line("antigravity", log.read_text(encoding="utf-8").splitlines()[0])
            self.assertIsNotNone(logged)
            self.assertEqual(logged.cwd, "/work/interactive-tui")

        self.assertEqual(
            detect_agent_origin(
                "antigravity",
                env={"ANTIGRAVITY_APP_DATA_DIR": "/Users/example/.gemini/antigravity-cli"},
            ).label,
            "Antigravity CLI",
        )
        self.assertEqual(
            origin_from_processes(
                "antigravity",
                (ProcessInfo(10, 1, "/opt/homebrew/bin/agy", "agy -p hello"),),
            ).label,
            "Antigravity CLI",
        )
        self.assertEqual(
            origin_from_processes(
                "antigravity",
                (
                    ProcessInfo(
                        11,
                        1,
                        "/Applications/Antigravity.app/Contents/MacOS/Antigravity",
                        "/Applications/Antigravity.app/Contents/MacOS/Antigravity",
                    ),
                ),
            ).label,
            "Antigravity App",
        )

    def test_unknown_events_and_malformed_lines_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "antigravity.jsonl"
            lines = [
                "",
                "not-json",
                "[]",
                json.dumps({"conversationId": "c1"}),
                json.dumps(synthetic_agy_payload("BeforeModel")),
                json.dumps(synthetic_agy_payload("SomeFutureEvent")),
            ]
            log.write_text("\n".join(lines) + "\n", encoding="utf-8")

            for line in lines:
                self.assertIsNone(parse_log_line("antigravity", line))

            self.assertIsNone(hook_report("antigravity", synthetic_agy_payload("SomeFutureEvent")))
            snapshot = AgentMonitor(sources=(SourceSpec("antigravity", log),)).snapshot()
            self.assertEqual(snapshot.statuses, ())
            self.assertEqual(snapshot.aggregate.mode, AgentMode.IDLE_READY)

    def test_resume_command_reopens_the_conversation(self) -> None:
        status = AgentStatus(
            provider="antigravity",
            agent_id="antigravity:session:conv-1",
            display_name="Fix tests",
            mode=AgentMode.COMPLETED,
            updated_at=datetime.now(timezone.utc),
            event_name="Stop",
            session_id="00000000-0000-4000-8000-000000000001",
            cwd="/work/my project",
        )
        self.assertEqual(
            session_resume_command(status),
            "cd '/work/my project' && agy --conversation 00000000-0000-4000-8000-000000000001",
        )


if __name__ == "__main__":
    unittest.main()
