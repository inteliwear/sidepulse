from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sidepulse.collector import mode_for_event, status_from_event
from sidepulse.install import (
    HermesHookConflict,
    install_hermes_hooks,
    uninstall_hermes_hooks,
)
from sidepulse.models import AgentMode, provider_label
from sidepulse.providers import (
    HERMES_EVENTS,
    canonical_event_name,
    detect_hermes_config,
    flatten_hermes_payload,
    parse_log_line,
)

SAMPLE_CONFIG = """\
# Hermes configuration.
model:
  default: muse-spark-1.2-contributor  # inline comment
  provider: meta

agent:
  max_verify_nudges: 3
"""


def hermes_line(event: str, **extra) -> str:
    """Build a payload in agent.shell_hooks._serialize_payload() shape."""
    payload = {
        "hook_event_name": event,
        "tool_name": extra.pop("tool_name", None),
        "tool_input": extra.pop("tool_input", None),
        "session_id": extra.pop("session_id", "sess-abc123"),
        "cwd": extra.pop("cwd", "/Users/nep/project"),
        "extra": extra,
    }
    payload["logged_at"] = "2026-08-13T10:00:00Z"
    return json.dumps(payload)


class HermesEventMappingTests(unittest.TestCase):
    def test_lifecycle_events_map_to_canonical_names(self):
        self.assertEqual(canonical_event_name("on_session_start"), "SessionStart")
        self.assertEqual(canonical_event_name("pre_llm_call"), "UserPromptSubmit")
        self.assertEqual(canonical_event_name("pre_tool_call"), "PreToolUse")
        self.assertEqual(canonical_event_name("post_tool_call"), "PostToolUse")
        self.assertEqual(canonical_event_name("on_session_end"), "Stop")
        self.assertEqual(canonical_event_name("on_session_finalize"), "SessionEnd")
        self.assertEqual(canonical_event_name("api_request_error"), "StopFailure")
        self.assertEqual(canonical_event_name("subagent_stop"), "SubagentStop")

    def test_every_installed_event_has_a_canonical_mapping(self):
        for event in HERMES_EVENTS:
            self.assertIsNotNone(canonical_event_name(event), event)

    def test_provider_label(self):
        self.assertEqual(provider_label("hermes"), "Hermes")


class HermesPayloadTests(unittest.TestCase):
    def test_extra_is_lifted_to_top_level(self):
        flat = flatten_hermes_payload(json.loads(hermes_line("post_tool_call", duration_ms=42)))
        self.assertEqual(flat["duration_ms"], 42)
        self.assertNotIn("extra", flat)

    def test_result_is_aliased_to_tool_response(self):
        flat = flatten_hermes_payload(
            json.loads(hermes_line("post_tool_call", result='{"output": "hi"}'))
        )
        self.assertEqual(flat["tool_response"], '{"output": "hi"}')

    def test_top_level_keys_win_over_extra(self):
        raw = json.loads(hermes_line("pre_tool_call", tool_name="terminal"))
        raw["extra"]["session_id"] = "should-not-win"
        flat = flatten_hermes_payload(raw)
        self.assertEqual(flat["session_id"], "sess-abc123")

    def test_failed_turn_becomes_blocked_error(self):
        record = parse_log_line(
            "hermes", hermes_line("on_session_end", completed=False, failed=True)
        )
        self.assertEqual(mode_for_event(record), AgentMode.BLOCKED_ERROR)

    def test_interrupted_turn_waits_for_input(self):
        record = parse_log_line(
            "hermes", hermes_line("on_session_end", completed=False, interrupted=True)
        )
        self.assertEqual(mode_for_event(record), AgentMode.WAITING_FOR_INPUT)

    def test_completed_turn_is_completed(self):
        record = parse_log_line("hermes", hermes_line("on_session_end", completed=True))
        self.assertEqual(mode_for_event(record), AgentMode.COMPLETED)

    def test_api_request_error_is_blocked_error(self):
        record = parse_log_line("hermes", hermes_line("api_request_error", error="429"))
        self.assertEqual(mode_for_event(record), AgentMode.BLOCKED_ERROR)

    def test_pre_tool_call_produces_tool_running_status(self):
        record = parse_log_line(
            "hermes",
            hermes_line("pre_tool_call", tool_name="terminal", tool_input={"command": "ls"}),
        )
        status = status_from_event(record)
        self.assertEqual(status.provider, "hermes")
        self.assertEqual(status.mode, AgentMode.TOOL_RUNNING)
        self.assertEqual(status.tool_name, "terminal")
        self.assertEqual(status.cwd, "/Users/nep/project")

    def test_subagent_stop_carries_child_summary_and_agent_id(self):
        record = parse_log_line(
            "hermes",
            hermes_line(
                "subagent_stop",
                session_id="",
                parent_session_id="parent-sess",
                child_role="researcher",
                child_status="completed",
                child_summary="Found the answer.",
            ),
        )
        self.assertEqual(record.agent_id, "parent-sess:researcher")
        self.assertEqual(record.message, "Found the answer.")
        self.assertEqual(mode_for_event(record), AgentMode.COMPLETED)


class HermesInstallTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config = self.tmp / "config.yaml"
        self.log = self.tmp / "hermes.jsonl"
        self.addCleanup(self._tmp.cleanup)

    def install(self, **kwargs):
        return install_hermes_hooks(log_path=self.log, config_path=self.config, **kwargs)

    def test_install_creates_hooks_block_and_preserves_comments(self):
        self.config.write_text(SAMPLE_CONFIG)
        result = self.install()

        self.assertTrue(result.changed)
        text = self.config.read_text()
        self.assertIn("# Hermes configuration.", text)
        self.assertIn("inline comment", text)
        self.assertIn("hooks:", text)
        for event in HERMES_EVENTS:
            self.assertIn(f"  {event}:", text)
        self.assertIn(str(self.log), text)

    def test_installed_block_is_valid_yaml_matching_hermes_schema(self):
        yaml = self._require_yaml()
        self.config.write_text(SAMPLE_CONFIG)
        self.install()

        data = yaml.safe_load(self.config.read_text())
        self.assertEqual(data["model"]["provider"], "meta")
        for event in HERMES_EVENTS:
            entries = data["hooks"][event]
            self.assertIsInstance(entries, list)
            self.assertIn("hook_entry.py", entries[0]["command"])
            self.assertIn("--provider hermes", entries[0]["command"])
            self.assertIsInstance(entries[0]["timeout"], int)

    def test_install_is_idempotent(self):
        self.config.write_text(SAMPLE_CONFIG)
        self.install()
        first = self.config.read_text()

        second_result = self.install()
        self.assertFalse(second_result.changed)
        self.assertEqual(self.config.read_text(), first)

    def test_uninstall_restores_original_text(self):
        self.config.write_text(SAMPLE_CONFIG)
        self.install()
        result = uninstall_hermes_hooks(log_path=self.log, config_path=self.config)

        self.assertTrue(result.changed)
        self.assertEqual(self.config.read_text(), SAMPLE_CONFIG)

    def test_uninstall_keeps_unmanaged_hooks(self):
        self.config.write_text(
            SAMPLE_CONFIG + "\nhooks:\n  transform_llm_output:\n    - command: /bin/mine\n"
        )
        self.install()
        uninstall_hermes_hooks(log_path=self.log, config_path=self.config)

        text = self.config.read_text()
        self.assertIn("transform_llm_output:", text)
        self.assertIn("/bin/mine", text)
        self.assertNotIn("hook_entry.py", text)

    def test_conflicting_hand_written_event_raises(self):
        self.config.write_text(
            SAMPLE_CONFIG + "\nhooks:\n  pre_tool_call:\n    - command: /bin/mine\n"
        )
        with self.assertRaises(HermesHookConflict) as ctx:
            self.install()
        self.assertIn("pre_tool_call", str(ctx.exception))
        self.assertNotIn("hook_entry.py", self.config.read_text())

    def test_dry_run_does_not_write(self):
        self.config.write_text(SAMPLE_CONFIG)
        result = self.install(dry_run=True)

        self.assertTrue(result.changed)
        self.assertEqual(self.config.read_text(), SAMPLE_CONFIG)

    def test_detect_reports_installed_events(self):
        home = self.tmp / "home"
        (home / ".hermes").mkdir(parents=True)
        config = home / ".hermes" / "config.yaml"
        config.write_text(SAMPLE_CONFIG)
        install_hermes_hooks(log_path=self.log, config_path=config)

        detected = detect_hermes_config(home)
        self.assertTrue(detected.exists)
        self.assertTrue(detected.hooks_enabled)
        self.assertIn("PreToolUse", detected.hook_events)
        self.assertIn("SessionStart", detected.hook_events)
        self.assertEqual(detected.log_paths[0], self.log)

    def test_detect_ignores_unmanaged_hooks(self):
        home = self.tmp / "home"
        (home / ".hermes").mkdir(parents=True)
        config = home / ".hermes" / "config.yaml"
        config.write_text(
            SAMPLE_CONFIG + "\nhooks:\n  pre_llm_call:\n    - command: /bin/mine --log /tmp/mine.log\n"
        )

        detected = detect_hermes_config(home)
        self.assertTrue(detected.exists)
        self.assertFalse(detected.hooks_enabled)
        self.assertEqual(detected.hook_events, ())
        self.assertEqual(detected.log_paths, ())

    def test_detect_separates_managed_from_unmanaged_hooks(self):
        home = self.tmp / "home"
        (home / ".hermes").mkdir(parents=True)
        config = home / ".hermes" / "config.yaml"
        config.write_text(
            SAMPLE_CONFIG
            + "\nhooks:\n  transform_llm_output:\n    - command: /bin/mine --log /tmp/mine.log\n"
        )
        install_hermes_hooks(log_path=self.log, config_path=config)

        detected = detect_hermes_config(home)
        self.assertIn("PreToolUse", detected.hook_events)
        self.assertEqual(detected.log_paths, (self.log,))
        self.assertNotIn(Path("/tmp/mine.log"), detected.log_paths)

    def test_detect_missing_config(self):
        detected = detect_hermes_config(self.tmp / "nowhere")
        self.assertFalse(detected.exists)
        self.assertFalse(detected.hooks_enabled)
        self.assertEqual(detected.hook_events, ())

    def _require_yaml(self):
        try:
            import yaml
        except ImportError:  # pragma: no cover - depends on the test environment
            self.skipTest("PyYAML not installed")
        return yaml


if __name__ == "__main__":
    unittest.main()
