from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from sidepulse.codex_hook import TURN_CONTEXT_READ_LIMIT, normalize_payload
from sidepulse.collector import AgentMonitor, LiveAgentMonitor, SourceSpec
from sidepulse.hook import format_hook_payload, hook_log_main
from sidepulse.models import AgentMode
from sidepulse.providers import parse_log_line


class CodexApprovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.transcript = self.root / "rollout.jsonl"
        self.payload = {
            "hook_event_name": "PermissionRequest",
            "session_id": "approval-session",
            "turn_id": "approval-turn",
            "tool_name": "mcp__idea__execute_terminal_command",
            "tool_input": {"command": "git fetch upstream"},
            "transcript_path": str(self.transcript),
        }

    def context(self, reviewer, turn="approval-turn"):
        return {"type": "turn_context", "payload": {
            "turn_id": turn, "approvals_reviewer": reviewer,
        }}

    def write_contexts(self, *contexts):
        self.transcript.write_text("\n".join(json.dumps(c) for c in contexts) + "\n")

    def test_only_matching_turn_metadata_identifies_automatic_review(self):
        self.write_contexts(self.context("auto_review"), self.context("user", "other-turn"))
        for tool in ("Bash", "apply_patch", "mcp__idea__execute_terminal_command"):
            with self.subTest(tool=tool):
                normalized = normalize_payload(self.payload | {"tool_name": tool})
                self.assertEqual(normalized["sidepulse_approvals_reviewer"], "auto_review")
                self.assertEqual(normalized["tool_input"], self.payload["tool_input"])
        self.assertNotIn("sidepulse_approvals_reviewer", self.payload)

    def test_latest_context_does_not_reuse_an_older_automatic_reviewer(self):
        for reviewer in ("user", None, "future-reviewer", {}, []):
            with self.subTest(reviewer=reviewer):
                self.write_contexts(self.context("auto_review"), self.context(reviewer))
                normalized = normalize_payload(self.payload)
                self.assertNotEqual(normalized.get("sidepulse_approvals_reviewer"), "auto_review")

    def test_app_and_computer_use_approvals_keep_manual_behavior(self):
        self.write_contexts(self.context("auto_review"))
        for tool in ("mcp__codex_apps__github_create_pull_request", "mcp__cua_repl__js", "unknown", None):
            with self.subTest(tool=tool):
                normalized = normalize_payload(self.payload | {"tool_name": tool})
                self.assertNotIn("sidepulse_approvals_reviewer", normalized)
                line = {"logged_at": datetime.now(timezone.utc).isoformat(), "event": normalized}
                record = parse_log_line("codex", json.dumps(line))
                monitor = LiveAgentMonitor()
                monitor.ingest_record(record)
                self.assertEqual(monitor.snapshot().aggregate.mode, AgentMode.WAITING_FOR_INPUT)

    def test_missing_changed_or_malformed_metadata_is_conservative(self):
        texts = (
            "not-json\n", "[]\n", '{"type":"turn_context","payload":[]}\n',
            json.dumps(self.context("auto_review", "other-turn")) + "\n",
        )
        for text in texts:
            with self.subTest(text=text):
                self.transcript.write_text(text)
                self.assertNotIn("sidepulse_approvals_reviewer", normalize_payload(self.payload))
        self.transcript.unlink()
        self.assertNotIn("sidepulse_approvals_reviewer", normalize_payload(self.payload))
        self.assertNotIn("sidepulse_approvals_reviewer", normalize_payload(
            self.payload | {"transcript_path": "bad\0path"}))

    def test_partial_or_corrupt_new_context_does_not_reuse_old_automatic_setting(self):
        for newer in ('{"type":"turn_context","payload":', 'not-json "turn_context"\n'):
            with self.subTest(newer=newer):
                self.write_contexts(self.context("auto_review"))
                with self.transcript.open("a") as handle:
                    handle.write(newer)
                self.assertNotIn("sidepulse_approvals_reviewer", normalize_payload(self.payload))

    def test_read_is_bounded_and_does_not_open_special_files(self):
        # A context outside the bounded tail cannot silently hide an approval.
        self.write_contexts(self.context("auto_review"))
        with self.transcript.open("a") as handle:
            handle.write("x" * (TURN_CONTEXT_READ_LIMIT + 1) + "\n")
        self.assertNotIn("sidepulse_approvals_reviewer", normalize_payload(self.payload))
        self.transcript.unlink()
        os.mkfifo(self.transcript)
        self.assertNotIn("sidepulse_approvals_reviewer", normalize_payload(self.payload))
        self.transcript.unlink()
        self.transcript.mkdir()
        self.assertNotIn("sidepulse_approvals_reviewer", normalize_payload(self.payload))

    def test_long_turn_still_finds_reviewer_metadata(self):
        self.write_contexts(self.context("auto_review"), {
            "type": "response_item", "payload": {"content": "x" * (1536 * 1024)},
        })
        self.assertEqual(normalize_payload(self.payload)["sidepulse_approvals_reviewer"], "auto_review")

    def test_non_permission_events_do_not_read_the_transcript(self):
        for event in ("PreToolUse", "PostToolUse", "Stop"):
            with self.subTest(event=event), patch("sidepulse.codex_hook.os.open") as opened:
                normalize_payload(self.payload | {"hook_event_name": event})
                opened.assert_not_called()

    def lines(self, reviewer):
        self.write_contexts(self.context(reviewer))
        common = self.payload | {"hook_event_name": "PreToolUse"}
        unrelated = common | {"tool_name": "Bash", "tool_input": {"command": "git status"}}
        # The first call is cancelled without a PostToolUse event. A later call
        # runs normally; no new user prompt resets the session in between.
        return [format_hook_payload("codex", json.dumps(p), include_origin=False) for p in (
            common,
            self.payload,
            unrelated,
            unrelated | {"hook_event_name": "PostToolUse", "tool_response": "clean"},
        )]

    def test_cancelled_automatic_review_does_not_leave_live_or_file_monitor_asking(self):
        lines = self.lines("auto_review")
        live = LiveAgentMonitor()
        for line in lines:
            live.ingest_record(parse_log_line("codex", json.dumps(line)))
        self.assertEqual(live.snapshot().aggregate.mode, AgentMode.WORKING)
        self.assertEqual(live.pending_permissions_by_key, {})
        log = self.root / "codex.jsonl"
        log.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
        replay = AgentMonitor(sources=(SourceSpec("codex", log),))
        self.assertEqual(replay.snapshot().aggregate.mode, AgentMode.WORKING)

    def test_manual_review_stays_asking_during_unrelated_parallel_tools(self):
        lines = self.lines("user")
        live = LiveAgentMonitor()
        for line in lines:
            live.ingest_record(parse_log_line("codex", json.dumps(line)))
        self.assertEqual(live.snapshot().aggregate.mode, AgentMode.WAITING_FOR_INPUT)
        self.assertTrue(live.pending_permissions_by_key)
        log = self.root / "codex.jsonl"
        log.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
        replay = AgentMonitor(sources=(SourceSpec("codex", log),))
        self.assertEqual(replay.snapshot().aggregate.mode, AgentMode.WAITING_FOR_INPUT)

    def test_automatic_review_does_not_clear_another_pending_manual_approval(self):
        self.write_contexts(self.context("auto_review"))
        manual = self.payload | {
            "tool_name": "mcp__codex_apps__github_create_pull_request",
            "tool_input": {"repository_full_name": "example/demo", "head": "feature", "base": "main"},
        }
        live = LiveAgentMonitor()
        for payload in (manual, self.payload, self.payload | {"hook_event_name": "PostToolUse"}):
            line = format_hook_payload("codex", json.dumps(payload), include_origin=False)
            live.ingest_record(parse_log_line("codex", json.dumps(line)))
        self.assertEqual(live.snapshot().aggregate.mode, AgentMode.WAITING_FOR_INPUT)
        self.assertEqual(len(live.pending_permissions_by_key["codex:session:approval-session"]), 1)
        # Argument ordering must not prevent the matching completion clearing Ask.
        finished = manual | {"hook_event_name": "PostToolUse", "tool_input": {
            "base": "main", "head": "feature", "repository_full_name": "example/demo",
        }}
        line = format_hook_payload("codex", json.dumps(finished), include_origin=False)
        live.ingest_record(parse_log_line("codex", json.dumps(line)))
        self.assertEqual(live.snapshot().aggregate.mode, AgentMode.WORKING)
        self.assertEqual(live.pending_permissions_by_key, {})

    def test_hook_remains_silent_and_delivers_annotated_event_without_transcript_content(self):
        self.write_contexts({"type": "response_item", "payload": {"content": "private prompt"}},
                            self.context("auto_review"))
        captured = io.StringIO()
        env = {"HOME": str(self.root), "XDG_STATE_HOME": str(self.root / "state"),
               "XDG_CONFIG_HOME": str(self.root / "config"), "SIDEPULSE_DISABLE_EVENT_SOCKET": "0"}
        log = self.root / "codex.jsonl"
        with patch.dict(os.environ, env), patch.object(sys, "stdin", io.StringIO(json.dumps(self.payload))), \
                patch("sidepulse.hook.send_hook_event") as sent, redirect_stdout(captured):
            result = hook_log_main("codex", log)
        self.assertEqual(result, 0)
        self.assertEqual(captured.getvalue(), "")
        line = json.loads(log.read_text())
        self.assertEqual(line["event"]["sidepulse_approvals_reviewer"], "auto_review")
        self.assertNotIn("private prompt", log.read_text())
        sent.assert_any_call("codex", line)


if __name__ == "__main__":
    unittest.main()
