from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from unittest.mock import patch

from sidepulse.cli import build_sidepulse_parser, install_hook_results
from sidepulse.install import install_opencode_plugin, uninstall_opencode_plugin
from sidepulse.models import AgentMode, AgentStatus
from sidepulse.program_status import validate_context, validate_status
from sidepulse.providers import default_opencode_plugin_path, detect_opencode_config
from sidepulse.session_actions import session_resume_command

PLUGIN = Path(str(files("sidepulse.resources").joinpath("opencode", "sidepulse.ts")))
HARNESS = Path(__file__).with_name("opencode_plugin_harness.mjs")


def node_with_type_stripping() -> list[str] | None:
    node = shutil.which("node")
    if not node:
        return None
    version = subprocess.run([node, "--version"], capture_output=True, text=True).stdout.strip()
    match = re.fullmatch(r"v(\d+)\.(\d+)\..*", version)
    if not match or (int(match[1]), int(match[2])) < (22, 6):
        return None
    return [node, "--experimental-strip-types", "--no-warnings"]


def run_plugin(sessions: dict, events: list[dict], *, copies: int = 1, exit_code: int = 0,
               command: str | None = None) -> tuple[list[dict], str]:
    """Run the plugin's real entry point; a fake `sidepulse` records each report."""
    node = node_with_type_stripping()
    assert node is not None
    with tempfile.TemporaryDirectory() as tmp:
        output = Path(tmp) / "reports.jsonl"
        fake = Path(tmp) / "sidepulse"
        fake.write_text(
            "#!/bin/sh\n"
            f'[ "$1 $2" = "report --json" ] || exit 64\n'
            f'cat >> "{output}"\necho >> "{output}"\nexit {exit_code}\n'
        )
        fake.chmod(0o755)
        result = subprocess.run(
            [*node, str(HARNESS), str(PLUGIN), command or str(fake)],
            input=json.dumps({"sessions": sessions, "events": events, "copies": copies}),
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        lines = output.read_text().splitlines() if output.exists() else []
    return [json.loads(line) for line in lines if line], result.stderr


def event(event_id: str, event_type: str, **data) -> dict:
    return {"id": event_id, "type": event_type, "data": data}


class OpenCodeInstallTests(unittest.TestCase):
    def test_install_writes_the_bundled_plugin_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = default_opencode_plugin_path(Path(tmp))

            dry = install_opencode_plugin(plugin_path=target, dry_run=True)
            self.assertTrue(dry.changed)
            self.assertFalse(target.exists())

            first = install_opencode_plugin(plugin_path=target)
            second = install_opencode_plugin(plugin_path=target)

            self.assertTrue(first.changed)
            self.assertFalse(second.changed)
            self.assertIsNone(first.log_path)
            self.assertEqual(target.read_text(encoding="utf-8"), PLUGIN.read_text(encoding="utf-8"))
            self.assertEqual(target, Path(tmp) / ".config" / "opencode" / "plugins" / "sidepulse.ts")

    def test_install_and_uninstall_leave_an_unmanaged_plugin_alone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = default_opencode_plugin_path(Path(tmp))
            target.parent.mkdir(parents=True)
            target.write_text("export default {}\n", encoding="utf-8")

            installed = install_opencode_plugin(plugin_path=target)
            removed = uninstall_opencode_plugin(plugin_path=target)

            self.assertFalse(installed.changed)
            self.assertIn("not installed by SidePulse", installed.note)
            self.assertFalse(removed.changed)
            self.assertEqual(target.read_text(encoding="utf-8"), "export default {}\n")

    def test_uninstall_removes_the_managed_plugin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = default_opencode_plugin_path(Path(tmp))
            install_opencode_plugin(plugin_path=target)

            self.assertTrue(uninstall_opencode_plugin(plugin_path=target, dry_run=True).changed)
            self.assertTrue(target.exists())
            self.assertTrue(uninstall_opencode_plugin(plugin_path=target).changed)
            self.assertFalse(target.exists())
            self.assertFalse(uninstall_opencode_plugin(plugin_path=target).changed)

    def test_plugin_path_follows_xdg_config_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"XDG_CONFIG_HOME": tmp}):
            self.assertEqual(default_opencode_plugin_path(), Path(tmp) / "opencode" / "plugins" / "sidepulse.ts")

    def test_detection_reports_only_a_managed_plugin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.assertFalse(detect_opencode_config(home).hooks_enabled)

            install_opencode_plugin(plugin_path=default_opencode_plugin_path(home))
            config = detect_opencode_config(home)

            self.assertTrue(config.exists)
            self.assertTrue(config.hooks_enabled)
            self.assertEqual(config.log_paths, ())

    def test_setup_accepts_opencode_and_hook_log_does_not(self) -> None:
        parser = build_sidepulse_parser()
        with tempfile.TemporaryDirectory() as tmp:
            target = default_opencode_plugin_path(Path(tmp))
            args = parser.parse_args(["setup", "opencode", "--dry-run"])
            with patch("sidepulse.install.default_opencode_plugin_path", return_value=target):
                results = install_hook_results(args)

        self.assertEqual([result.provider for result in results], ["opencode"])
        self.assertTrue(results[0].dry_run)
        with self.assertRaises(SystemExit), patch("sys.stderr"):
            parser.parse_args(["hook-log", "--provider", "opencode", "--log", "/tmp/x.jsonl"])

    def test_resume_command_reopens_the_session(self) -> None:
        status = AgentStatus(
            provider="opencode",
            agent_id="opencode-ses_1",
            display_name="Fix login",
            mode=AgentMode.COMPLETED,
            updated_at=datetime.now(timezone.utc),
            event_name="session.execution.succeeded",
            session_id="ses_1",
            cwd="/work/my app",
        )
        self.assertEqual(session_resume_command(status), "cd '/work/my app' && opencode --session ses_1")


@unittest.skipUnless(node_with_type_stripping() and os.name != "nt", "needs Node.js 22.6 or newer and a POSIX shell")
class OpenCodePluginTests(unittest.TestCase):
    sessions = {
        "ses_a": {"title": "Fix login", "location": {"directory": "/work"}},
        "ses_child": {"parentID": "ses_a", "title": "Explore auth"},
    }

    def states(self, events: list[dict]) -> list[tuple]:
        return [
            (report["status"]["state"], report["status"].get("kind"), report["status"].get("msg"))
            for report in run_plugin(self.sessions, events)[0]
        ]

    def test_reports_are_valid_program_status(self) -> None:
        reports, _ = run_plugin(self.sessions, [
            event("e1", "session.execution.started", sessionID="ses_a"),
            event("e2", "permission.asked", id="per_1", sessionID="ses_a", action="shell", resources=["ls"]),
        ])

        for report in reports:
            self.assertEqual((report["v"], report["type"], report["scope"]), (1, "program_status", "opencode-ses_a"))
            validate_status(report["status"])
            validate_context(report["context"])
        self.assertEqual([report["request_id"] for report in reports], ["opencode-e1", "opencode-e2"])
        self.assertEqual(reports[0]["status"], {"state": "working", "app": "opencode", "title": "Fix login"})
        self.assertEqual(reports[0]["context"], {
            "provider": "opencode", "session_id": "ses_a", "event_name": "session.execution.started",
            "cwd": "/work", "title": "Fix login",
        })

    def test_lifecycle_maps_to_states(self) -> None:
        self.assertEqual(self.states([
            event("e1", "session.execution.started", sessionID="ses_a"),
            event("e2", "session.execution.succeeded", sessionID="ses_a"),
            event("e3", "session.execution.started", sessionID="ses_a"),
            event("e4", "session.execution.failed", sessionID="ses_a", error={"type": "x", "message": "boom"}),
            event("e5", "session.execution.started", sessionID="ses_a"),
            event("e6", "session.execution.interrupted", sessionID="ses_a", reason="user"),
            event("e7", "session.execution.interrupted", sessionID="ses_a", reason="shutdown"),
            event("e8", "session.deleted", sessionID="ses_a"),
        ]), [
            ("working", None, None), ("done", None, None), ("working", None, None), ("error", None, None),
            ("working", None, None), ("idle", None, None), ("clear", None, None), ("clear", None, None),
        ])

    def test_subagent_prompts_block_the_parent_until_every_prompt_is_answered(self) -> None:
        self.assertEqual(self.states([
            event("e1", "session.execution.started", sessionID="ses_a"),
            event("e2", "session.execution.started", sessionID="ses_child"),
            event("e3", "permission.asked", id="per_1", sessionID="ses_child", action="shell", resources=["rm -rf build"]),
            event("e4", "form.created", form={"id": "frm_1", "sessionID": "ses_a", "title": "Which database?", "fields": []}),
            event("e5", "permission.replied", sessionID="ses_child", requestID="per_1", reply="once"),
            event("e6", "permission.replied", sessionID="ses_child", requestID="per_1", reply="once"),
            event("e7", "form.cancelled", id="frm_1", sessionID="ses_a"),
            event("e8", "session.execution.succeeded", sessionID="ses_child"),
            event("e9", "session.execution.succeeded", sessionID="ses_a"),
        ]), [
            ("working", None, None),
            ("blocked", "permission", "shell: rm -rf build"),
            ("blocked", "question", "Which database?"),
            ("blocked", "question", "Which database?"),
            ("working", None, None),
            ("done", None, None),
        ])

    def test_permission_message_prefers_opencode_text_and_is_bounded(self) -> None:
        reports, _ = run_plugin(self.sessions, [
            event("e1", "permission.asked", id="per_1", sessionID="ses_a", action="edit",
                  resources=["a.py"], message="Edit\n\x07a.py " + "x" * 400),
        ])
        message = reports[0]["status"]["msg"]
        self.assertTrue(message.startswith("Edit a.py x"))
        self.assertEqual(len(message), 240)
        self.assertTrue(message.endswith("…"))

    def test_rename_updates_the_title(self) -> None:
        reports, _ = run_plugin(self.sessions, [
            event("e1", "session.execution.started", sessionID="ses_a"),
            event("e2", "session.renamed", sessionID="ses_a", title="Fix login bug"),
            event("e3", "session.execution.succeeded", sessionID="ses_a"),
        ])
        self.assertEqual([report["status"].get("title") for report in reports], ["Fix login", "Fix login bug", "Fix login bug"])

    def test_each_event_is_reported_once_when_several_copies_load(self) -> None:
        reports, _ = run_plugin(self.sessions, [
            event("e1", "session.execution.started", sessionID="ses_a"),
            event("e2", "session.execution.succeeded", sessionID="ses_a"),
        ], copies=3)
        self.assertEqual([report["request_id"] for report in reports], ["opencode-e1", "opencode-e2"])

    def test_a_failing_or_missing_sidepulse_is_reported_once_and_ignored(self) -> None:
        events = [
            event("e1", "session.execution.started", sessionID="ses_a"),
            event("e2", "session.execution.succeeded", sessionID="ses_a"),
        ]
        reports, stderr = run_plugin(self.sessions, events, exit_code=1)
        self.assertEqual(len(reports), 2)
        self.assertEqual(stderr.count("[sidepulse] status report failed (exit 1)"), 1)

        reports, stderr = run_plugin(self.sessions, events, command="/nonexistent/sidepulse")
        self.assertEqual(reports, [])
        self.assertEqual(stderr.count("[sidepulse] status report failed"), 1)

    def test_unknown_sessions_and_untracked_replies_report_nothing(self) -> None:
        self.assertEqual(self.states([
            event("e1", "session.execution.started", sessionID="ses_missing"),
            event("e2", "permission.replied", sessionID="ses_a", requestID="per_x", reply="once"),
            event("e3", "session.deleted", sessionID="ses_a"),
        ]), [])


if __name__ == "__main__":
    unittest.main()
