from __future__ import annotations

import argparse
import importlib
import importlib.util
import io
import json
import socket
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "integrations" / "hermes"
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))


def load_plugin_module():
    spec = importlib.util.spec_from_file_location(
        "hermes_sidepulse_under_test",
        PLUGIN_ROOT / "__init__.py",
        submodule_search_locations=[str(PLUGIN_ROOT)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load Hermes SidePulse plugin")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakePluginContext:
    profile_name = "default"

    def __init__(self, config=None) -> None:
        self.config = config or {}
        self.hooks = {}
        self.cli_commands = {}

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def register_hook(self, name, callback) -> None:
        self.hooks[name] = callback

    def register_cli_command(self, *, name, help, setup_fn, handler_fn) -> None:
        self.cli_commands[name] = {
            "help": help,
            "setup_fn": setup_fn,
            "handler_fn": handler_fn,
        }


class HermesBridgeTests(unittest.TestCase):
    def test_event_carries_profile_selector_for_scoped_session_lookup(self) -> None:
        bridge = importlib.import_module("bridge")
        self.assertIn("profile_name", bridge.PluginSettings.__dataclass_fields__)
        settings = bridge.PluginSettings(
            agent_id="Homelab",
            profile_name="homelab",
        )

        event = bridge.translate_hook(
            "pre_llm_call",
            {"session_id": "session-1", "platform": "desktop"},
            settings,
            now="2026-08-14T14:00:00Z",
        )

        self.assertEqual(event["hermes_profile"], "homelab")

    def test_translate_hook_rejects_missing_profile_provenance(self) -> None:
        bridge = importlib.import_module("bridge")

        event = bridge.translate_hook(
            "pre_llm_call",
            {"session_id": "private-session", "platform": "desktop"},
            bridge.PluginSettings(agent_id="EDI", profile_name=""),
            now="2026-08-14T14:00:00Z",
        )

        self.assertIsNone(event)

    def test_pre_llm_event_exposes_only_status_metadata(self) -> None:
        bridge = importlib.import_module("bridge")

        settings = bridge.PluginSettings(agent_id="EDI", profile_name="default")
        event = bridge.translate_hook(
            "pre_llm_call",
            {
                "session_id": "session-1",
                "turn_id": "turn-1",
                "platform": "desktop",
                "user_message": "private user prompt",
                "conversation_history": [
                    {"role": "user", "content": "private history"}
                ],
            },
            settings,
            now="2026-08-14T14:00:00Z",
        )

        self.assertEqual(
            event,
            {
                "logged_at": "2026-08-14T14:00:00Z",
                "hook_event_name": "UserPromptSubmit",
                "integration": "hermes-plugin",
                "agent_id": "EDI:4c60b8409c66",
                "agent_origin": "Hermes Desktop",
                "agent_origin_kind": "hermes_desktop",
                "sidepulse_mode": "working",
                "session_id": "session-1",
                "hermes_profile": "default",
                "turn_id": "turn-1",
                "surface": "desktop",
            },
        )
        serialized = json.dumps(event)
        self.assertNotIn("private user prompt", serialized)
        self.assertNotIn("private history", serialized)

    def test_session_key_is_never_persisted_as_session_id(self) -> None:
        bridge = importlib.import_module("bridge")
        event = bridge.translate_hook(
            "pre_approval_request",
            {
                "session_key": "telegram:private-chat-12345",
                "surface": "gateway",
            },
            bridge.PluginSettings(agent_id="EDI", profile_name="default"),
            now="2026-08-14T14:00:00Z",
        )

        self.assertIsNotNone(event)
        self.assertNotIn("session_id", event)
        self.assertNotIn("private-chat-12345", json.dumps(event))

    def test_structured_tool_fallback_cannot_leak_arguments(self) -> None:
        bridge = importlib.import_module("bridge")
        marker = "PRIVATE_TOOL_ARGUMENT"
        event = bridge.translate_hook(
            "pre_tool_call",
            {
                "tool": {
                    "name": "terminal",
                    "args": {"command": f"printf {marker}"},
                },
                "session_id": "session-1",
                "turn_id": "turn-1",
                "platform": "desktop",
            },
            bridge.PluginSettings(agent_id="EDI", profile_name="default"),
            now="2026-08-14T12:00:00+00:00",
        )
        serialized = json.dumps(event)
        self.assertNotIn(marker, serialized)
        self.assertNotIn("tool_name", event)

    def test_lifecycle_hooks_map_to_expected_sidepulse_states(self) -> None:
        bridge = importlib.import_module("bridge")
        settings = bridge.PluginSettings(agent_id="EDI", profile_name="default")
        cases = [
            ("on_session_start", {}, "SessionStart", "idle_ready"),
            ("on_session_finalize", {"reason": "shutdown"}, "SessionFinalize", "completed"),
            ("pre_tool_call", {"tool_name": "terminal"}, "PreToolUse", "tool_running"),
            (
                "pre_tool_call",
                {"tool_name": "clarify"},
                "PermissionRequest",
                "waiting_for_input",
            ),
            (
                "post_tool_call",
                {"tool_name": "terminal", "status": "ok"},
                "PostToolUse",
                "working",
            ),
            (
                "post_tool_call",
                {"tool_name": "terminal", "status": "error"},
                "PostToolUseFailure",
                "blocked_error",
            ),
            ("pre_approval_request", {}, "PermissionRequest", "waiting_for_input"),
            (
                "post_approval_response",
                {"choice": "deny"},
                "PermissionDenied",
                "blocked_error",
            ),
            (
                "post_approval_response",
                {"choice": "once"},
                "UserPromptSubmit",
                "working",
            ),
            ("api_request_error", {}, "StopFailure", "blocked_error"),
            ("on_session_reset", {}, "SessionStart", "idle_ready"),
        ]

        for hook_name, extra, expected_event, expected_mode in cases:
            with self.subTest(hook_name=hook_name, extra=extra):
                payload = {
                    "session_id": "session-1",
                    "turn_id": "turn-1",
                    "platform": "desktop",
                    **extra,
                }
                event = bridge.translate_hook(
                    hook_name,
                    payload,
                    settings,
                    now="2026-08-14T14:00:00Z",
                )
                self.assertIsNotNone(event)
                self.assertEqual(event["hook_event_name"], expected_event)
                self.assertEqual(event["sidepulse_mode"], expected_mode)
                if "tool_name" in extra:
                    self.assertEqual(event["tool_name"], extra["tool_name"])

    def test_approval_delivery_failures_map_to_blocked_error(self) -> None:
        bridge = importlib.import_module("bridge")
        settings = bridge.PluginSettings(agent_id="EDI", profile_name="default")
        failure_choices = (
            "notify_failed",
            "transport_busy",
            "transport_error",
            "transport_interrupted",
            "transport_timeout",
            "transport_invalid",
            "transport_stale",
        )

        for choice in failure_choices:
            with self.subTest(choice=choice):
                event = bridge.translate_hook(
                    "post_approval_response",
                    {
                        "session_id": "session-1",
                        "platform": "desktop",
                        "choice": choice,
                    },
                    settings,
                    now="2026-08-14T14:00:00Z",
                )
                self.assertEqual(event["hook_event_name"], "PermissionDenied")
                self.assertEqual(event["sidepulse_mode"], "blocked_error")

    def test_same_profile_sessions_emit_distinct_status_identities(self) -> None:
        bridge = importlib.import_module("bridge")
        settings = bridge.PluginSettings(agent_id="EDI", profile_name="default")
        first = bridge.translate_hook(
            "pre_llm_call",
            {"session_id": "session-a", "platform": "desktop"},
            settings,
            now="2026-08-14T14:00:00Z",
        )
        second = bridge.translate_hook(
            "pre_llm_call",
            {"session_id": "session-b", "platform": "desktop"},
            settings,
            now="2026-08-14T14:00:01Z",
        )

        self.assertNotEqual(first["agent_id"], second["agent_id"])
        self.assertRegex(first["agent_id"], r"^EDI:[0-9a-f]{12}$")
        self.assertRegex(second["agent_id"], r"^EDI:[0-9a-f]{12}$")
        self.assertNotIn("session-a", first["agent_id"])
        self.assertNotIn("session-b", second["agent_id"])

    def test_final_response_uses_explicit_marker_without_persisting_response(
        self,
    ) -> None:
        bridge = importlib.import_module("bridge")
        event = bridge.translate_hook(
            "post_llm_call",
            {
                "session_id": "session-1",
                "platform": "desktop",
                "assistant_response": "Sensitive response text.\n<!-- sidepulse:ask -->",
            },
            bridge.PluginSettings(agent_id="EDI", profile_name="default"),
            now="2026-08-14T14:00:00Z",
        )

        self.assertEqual(event["hook_event_name"], "Stop")
        self.assertEqual(event["sidepulse_mode"], "waiting_for_input")
        self.assertNotIn("Sensitive response text", json.dumps(event))

    def test_casual_work_invitation_does_not_leave_agent_waiting(self) -> None:
        bridge = importlib.import_module("bridge")
        event = bridge.translate_hook(
            "post_llm_call",
            {
                "session_id": "session-1",
                "platform": "desktop",
                "assistant_response": "Got it — I'm here. What do you want to work on?",
            },
            bridge.PluginSettings(agent_id="EDI", profile_name="developer"),
            now="2026-08-16T00:49:43Z",
        )

        self.assertEqual(event["hook_event_name"], "Stop")
        self.assertEqual(event["sidepulse_mode"], "completed")

    def test_emit_hook_logs_metadata_and_sends_sidepulse_socket_event(self) -> None:
        bridge = importlib.import_module("bridge")
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            state_dir = Path(tmp)
            socket_path = state_dir / "events.sock"
            received = []
            ready = threading.Event()

            def receive_one() -> None:
                server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    server.bind(str(socket_path))
                    server.listen(1)
                    ready.set()
                    connection, _ = server.accept()
                    with connection:
                        chunks = []
                        while True:
                            chunk = connection.recv(65536)
                            if not chunk:
                                break
                            chunks.append(chunk)
                    received.append(json.loads(b"".join(chunks).decode("utf-8")))
                finally:
                    server.close()

            receiver = threading.Thread(target=receive_one, daemon=True)
            receiver.start()
            self.assertTrue(ready.wait(timeout=2))

            result = bridge.emit_hook(
                "pre_tool_call",
                {
                    "session_id": "session-1",
                    "turn_id": "turn-1",
                    "platform": "desktop",
                    "tool_name": "terminal",
                    "args": {"command": "private command"},
                },
                bridge.PluginSettings(
                    agent_id="EDI",
                    profile_name="default",
                    state_dir=state_dir,
                    socket_path=socket_path,
                ),
                now="2026-08-14T14:00:00Z",
            )
            receiver.join(timeout=2)

            self.assertTrue(result.logged)
            self.assertTrue(result.delivered)
            self.assertEqual(received[0]["provider"], "hermes")
            self.assertEqual(received[0]["line"]["integration"], "hermes-plugin")
            self.assertEqual(received[0]["line"]["hook_event_name"], "PreToolUse")
            self.assertNotIn("private command", json.dumps(received[0]))
            log_lines = (state_dir / "hermes.jsonl").read_text().splitlines()
            self.assertEqual(len(log_lines), 1)
            self.assertEqual(json.loads(log_lines[0]), received[0]["line"])

    def test_event_log_is_created_owner_read_write_only(self) -> None:
        bridge = importlib.import_module("bridge")
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            state_dir = Path(tmp)
            previous_umask = __import__("os").umask(0o022)
            try:
                result = bridge.emit_hook(
                    "pre_llm_call",
                    {"session_id": "session-1", "platform": "desktop"},
                    bridge.PluginSettings(
                        profile_name="default",
                        state_dir=state_dir,
                        socket_path=state_dir / "missing.sock",
                    ),
                    now="2026-08-14T14:00:00Z",
                )
            finally:
                __import__("os").umask(previous_umask)

            self.assertTrue(result.logged)
            mode = (state_dir / "hermes.jsonl").stat().st_mode & 0o777
            self.assertEqual(mode, 0o600)

    def test_surface_recovery_requires_matching_profile_provenance(self) -> None:
        bridge = importlib.import_module("bridge")
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            state_dir = Path(tmp)
            alpha = bridge.PluginSettings(
                agent_id="EDI",
                profile_name="alpha",
                state_dir=state_dir,
                log_events=True,
            )
            beta = bridge.PluginSettings(
                agent_id="EDI",
                profile_name="beta",
                state_dir=state_dir,
                log_events=True,
            )
            alpha_event = bridge.translate_hook(
                "on_session_activate",
                {
                    "session_id": "imported-session",
                    "platform": "desktop",
                    "activation_mode": "working",
                },
                alpha,
                now="2026-08-14T14:00:00Z",
            )
            missing_profile_event = {
                **alpha_event,
                "surface": "cli",
                "sidepulse_mode": "blocked_error",
            }
            missing_profile_event.pop("hermes_profile")
            (state_dir / "hermes.jsonl").write_text(
                json.dumps(alpha_event)
                + "\n"
                + json.dumps(missing_profile_event)
                + "\n",
                encoding="utf-8",
            )

            self.assertEqual(
                bridge.recover_session_surface(alpha, "imported-session"),
                "desktop",
            )
            self.assertEqual(
                bridge.recover_session_mode(alpha, "imported-session"),
                "working",
            )
            self.assertEqual(
                bridge.recover_session_surface(beta, "imported-session"),
                "",
            )
            self.assertEqual(
                bridge.recover_session_mode(beta, "imported-session"),
                "",
            )

    def test_surface_recovery_skips_deeply_nested_corrupt_line(self) -> None:
        bridge = importlib.import_module("bridge")
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            state_dir = Path(tmp)
            event_log = state_dir / "hermes.jsonl"
            event_log.write_text(
                json.dumps(
                    {
                        "agent_id": bridge._status_agent_id(
                            "Hermes", "session-a", "default"
                        ),
                        "hook_event_name": "SessionStart",
                        "integration": "hermes-plugin",
                        "hermes_profile": "default",
                        "session_id": "session-a",
                        "surface": "desktop",
                    }
                )
                + "\n"
                + "[" * 2_000
                + "0"
                + "]" * 2_000
                + "\n",
                encoding="utf-8",
            )

            recovered = bridge.recover_session_surface(
                bridge.PluginSettings(
                    profile_name="default",
                    state_dir=state_dir,
                    log_events=True,
                ),
                "session-a",
            )

            self.assertEqual(recovered, "desktop")

    def test_surface_recovery_caps_read_when_log_grows_concurrently(self) -> None:
        bridge = importlib.import_module("bridge")
        initial = (
            json.dumps(
                {
                    "agent_id": bridge._status_agent_id("Hermes", "session-a", "default"),
                    "hook_event_name": "SessionStart",
                    "integration": "hermes-plugin",
                    "hermes_profile": "default",
                    "session_id": "session-a",
                    "surface": "desktop",
                }
            ).encode("utf-8")
            + b"\n"
        )

        class GrowingLog(io.BytesIO):
            def __init__(self) -> None:
                super().__init__(initial)
                self.grow_on_next_seek = False
                self.read_sizes = []

            def seek(self, offset, whence=0):
                position = super().seek(offset, whence)
                if whence == 2:
                    self.grow_on_next_seek = True
                elif self.grow_on_next_seek:
                    self.grow_on_next_seek = False
                    original_position = position
                    super().seek(0, 2)
                    super().write(b"{}\n" * (1024 * 1024))
                    super().seek(original_position)
                return position

            def read(self, size: int | None = -1):
                self.read_sizes.append(size)
                return super().read(size)

        growing_log = GrowingLog()
        with patch("builtins.open", return_value=growing_log):
            recovered = bridge.recover_session_surface(
                bridge.PluginSettings(profile_name="default", log_events=True),
                "session-a",
            )

        self.assertEqual(recovered, "desktop")
        self.assertEqual(growing_log.read_sizes, [len(initial)])
        self.assertLessEqual(growing_log.read_sizes[0], 1024 * 1024)

    def test_surface_recovery_ignores_stale_cli_approval_record(self) -> None:
        bridge = importlib.import_module("bridge")
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            state_dir = Path(tmp)
            event_log = state_dir / "hermes.jsonl"
            records = (
                {
                    "agent_id": bridge._status_agent_id("Hermes", "session-a", "default"),
                    "hook_event_name": "SessionStart",
                    "integration": "hermes-plugin",
                    "hermes_profile": "default",
                    "session_id": "session-a",
                    "surface": "desktop",
                },
                {
                    "agent_id": bridge._status_agent_id("Hermes", "session-a", "default"),
                    "hook_event_name": "PermissionRequest",
                    "integration": "hermes-plugin",
                    "hermes_profile": "default",
                    "session_id": "session-a",
                    "surface": "cli",
                },
                {
                    "agent_id": bridge._status_agent_id("Hermes", "session-a", "default"),
                    "hook_event_name": "PermissionDenied",
                    "integration": "hermes-plugin",
                    "hermes_profile": "default",
                    "session_id": "session-a",
                    "surface": "cli",
                },
            )
            event_log.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )

            recovered = bridge.recover_session_surface(
                bridge.PluginSettings(
                    profile_name="default",
                    state_dir=state_dir,
                    log_events=True,
                ),
                "session-a",
            )

            self.assertEqual(recovered, "desktop")

    def test_emit_hook_degrades_to_log_when_status_bar_socket_is_absent(self) -> None:
        bridge = importlib.import_module("bridge")
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            state_dir = Path(tmp)
            result = bridge.emit_hook(
                "pre_llm_call",
                {"session_id": "session-1", "platform": "desktop"},
                bridge.PluginSettings(
                    profile_name="default",
                    state_dir=state_dir,
                    socket_path=state_dir / "missing.sock",
                ),
                now="2026-08-14T14:00:00Z",
            )

            self.assertTrue(result.logged)
            self.assertFalse(result.delivered)
            self.assertTrue((state_dir / "hermes.jsonl").is_file())

    def test_successful_session_end_does_not_overwrite_final_response_state(
        self,
    ) -> None:
        bridge = importlib.import_module("bridge")
        event = bridge.translate_hook(
            "on_session_end",
            {"completed": True, "interrupted": False},
            bridge.PluginSettings(),
            now="2026-08-14T14:00:00Z",
        )
        self.assertIsNone(event)


class HermesPluginRegistrationTests(unittest.TestCase):
    def test_register_wires_observer_hooks_without_model_facing_tools(self) -> None:
        plugin = load_plugin_module()
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            state_dir = Path(tmp)
            ctx = FakePluginContext(
                {
                    "agent_id": "EDI",
                    "state_dir": str(state_dir),
                    "socket_path": str(state_dir / "missing.sock"),
                }
            )

            plugin.register(ctx)

            self.assertEqual(
                set(ctx.hooks),
                {
                    "on_session_start",
                    "on_session_activate",
                    "on_session_reset",
                    "pre_llm_call",
                    "pre_tool_call",
                    "post_tool_call",
                    "pre_approval_request",
                    "post_approval_response",
                    "api_request_error",
                    "post_llm_call",
                    "on_session_end",
                    "on_session_finalize",
                },
            )
            self.assertEqual(set(ctx.cli_commands), {"sidepulse"})
            result = ctx.hooks["pre_llm_call"](
                session_id="session-1",
                turn_id="turn-1",
                platform="desktop",
                user_message="private prompt",
            )
            self.assertIsNone(result)
            event = json.loads((state_dir / "hermes.jsonl").read_text().strip())
            self.assertRegex(event["agent_id"], r"^EDI:[0-9a-f]{12}$")
            self.assertEqual(event["surface"], "desktop")
            self.assertNotIn("private prompt", json.dumps(event))

    def test_session_finalize_emits_completed_event(self) -> None:
        plugin = load_plugin_module()
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            state_dir = Path(tmp)
            ctx = FakePluginContext(
                {
                    "agent_id": "EDI",
                    "profile_name": "default",
                    "state_dir": str(state_dir),
                    "socket_path": str(state_dir / "missing.sock"),
                }
            )
            plugin.register(ctx)

            ctx.hooks["on_session_finalize"](
                session_id="session-finalize",
                platform="desktop",
                reason="shutdown",
            )

            events = [
                json.loads(line)
                for line in (state_dir / "hermes.jsonl").read_text().splitlines()
            ]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["hook_event_name"], "SessionFinalize")
            self.assertEqual(events[0]["sidepulse_mode"], "completed")
            self.assertEqual(events[0]["surface"], "desktop")

    def test_session_activation_reasserts_running_wait_state_or_idle(self) -> None:
        plugin = load_plugin_module()
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            state_dir = Path(tmp)
            ctx = FakePluginContext(
                {
                    "agent_id": "EDI",
                    "state_dir": str(state_dir),
                    "socket_path": str(state_dir / "missing.sock"),
                }
            )
            plugin.register(ctx)

            ctx.hooks["pre_tool_call"](
                session_id="session-ask",
                platform="desktop",
                tool_name="clarify",
            )
            ctx.hooks["on_session_activate"](
                session_id="session-ask",
                platform="desktop",
                running=True,
            )
            ctx.hooks["on_session_activate"](
                session_id="session-idle",
                platform="desktop",
                running=False,
            )

            events = [
                json.loads(line)
                for line in (state_dir / "hermes.jsonl").read_text().splitlines()
            ]
            self.assertEqual(events[-2]["hook_event_name"], "SessionActivate")
            self.assertEqual(events[-2]["sidepulse_mode"], "waiting_for_input")
            self.assertEqual(events[-1]["hook_event_name"], "SessionActivate")
            self.assertEqual(events[-1]["sidepulse_mode"], "idle_ready")

    def test_running_session_activation_defaults_to_working(self) -> None:
        plugin = load_plugin_module()
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            state_dir = Path(tmp)
            ctx = FakePluginContext(
                {
                    "agent_id": "EDI",
                    "state_dir": str(state_dir),
                    "socket_path": str(state_dir / "missing.sock"),
                }
            )
            plugin.register(ctx)

            ctx.hooks["on_session_activate"](
                session_id="session-working",
                platform="desktop",
                running=True,
            )

            event = json.loads((state_dir / "hermes.jsonl").read_text().strip())
            self.assertEqual(event["sidepulse_mode"], "working")

    def test_sessionless_tool_hook_is_not_emitted(self) -> None:
        plugin = load_plugin_module()
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            state_dir = Path(tmp)
            ctx = FakePluginContext(
                {
                    "agent_id": "EDI",
                    "state_dir": str(state_dir),
                    "socket_path": str(state_dir / "missing.sock"),
                }
            )
            plugin.register(ctx)

            ctx.hooks["pre_tool_call"](tool_name="terminal")

            self.assertFalse((state_dir / "hermes.jsonl").exists())

    def test_profileless_context_does_not_emit_observer_status(self) -> None:
        plugin = load_plugin_module()

        class ProfilelessContext(FakePluginContext):
            profile_name = None

        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            state_dir = Path(tmp)
            ctx = ProfilelessContext(
                {
                    "agent_id": "EDI",
                    "state_dir": str(state_dir),
                    "socket_path": str(state_dir / "missing.sock"),
                }
            )

            settings = plugin._settings_from_context(ctx)
            self.assertEqual(settings.profile_name, "")
            plugin.register(ctx)
            with patch.object(plugin, "emit_hook") as emit:
                ctx.hooks["pre_llm_call"](
                    session_id="private-session",
                    turn_id="private-turn",
                    platform="desktop",
                )

            emit.assert_not_called()
            self.assertFalse((state_dir / "hermes.jsonl").exists())

    def test_session_activation_recovery_is_scoped_to_agent_provenance(self) -> None:
        plugin = load_plugin_module()
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            state_dir = Path(tmp)
            shared_config = {
                "state_dir": str(state_dir),
                "socket_path": str(state_dir / "missing.sock"),
            }
            agent_a = FakePluginContext({**shared_config, "agent_id": "Agent A"})
            agent_b = FakePluginContext({**shared_config, "agent_id": "Agent B"})
            plugin.register(agent_a)
            plugin.register(agent_b)

            agent_a.hooks["pre_tool_call"](
                session_id="shared-session",
                platform="desktop",
                tool_name="clarify",
            )
            agent_b.hooks["on_session_activate"](
                session_id="shared-session",
                running=True,
            )

            events = [
                json.loads(line)
                for line in (state_dir / "hermes.jsonl").read_text().splitlines()
            ]
            activation = events[-1]
            self.assertEqual(activation["hook_event_name"], "SessionActivate")
            self.assertEqual(activation["sidepulse_mode"], "working")
            self.assertEqual(activation["surface"], "unknown")
            self.assertNotEqual(activation["agent_id"], events[-2]["agent_id"])

    def test_running_activation_does_not_reuse_wait_state_from_other_session(self) -> None:
        plugin = load_plugin_module()
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            state_dir = Path(tmp)
            ctx = FakePluginContext(
                {
                    "agent_id": "EDI",
                    "state_dir": str(state_dir),
                    "socket_path": str(state_dir / "missing.sock"),
                }
            )
            plugin.register(ctx)

            ctx.hooks["pre_tool_call"](
                session_id="session-a",
                platform="desktop",
                tool_name="clarify",
            )
            ctx.hooks["on_session_activate"](
                session_id="session-b",
                platform="desktop",
                running=True,
            )

            events = [
                json.loads(line)
                for line in (state_dir / "hermes.jsonl").read_text().splitlines()
            ]
            self.assertEqual(events[-1]["sidepulse_mode"], "working")

    def test_approval_surface_does_not_replace_desktop_session_surface(self) -> None:
        plugin = load_plugin_module()
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            state_dir = Path(tmp)
            ctx = FakePluginContext(
                {
                    "agent_id": "EDI",
                    "state_dir": str(state_dir),
                    "socket_path": str(state_dir / "missing.sock"),
                }
            )
            plugin.register(ctx)

            ctx.hooks["pre_llm_call"](
                session_id="session-1",
                turn_id="turn-1",
                platform="desktop",
            )
            ctx.hooks["pre_approval_request"](
                session_key="telegram:private-chat-12345",
                turn_id="turn-1",
                surface="gateway",
            )

            events = [
                json.loads(line)
                for line in (state_dir / "hermes.jsonl").read_text().splitlines()
            ]
            approval = events[-1]
            self.assertEqual(approval["session_id"], "session-1")
            self.assertEqual(approval["surface"], "desktop")
            self.assertEqual(approval["agent_origin"], "Hermes Desktop")
            self.assertNotIn("private-chat-12345", json.dumps(approval))

    def test_session_end_clears_turn_surface_correlation(self) -> None:
        plugin = load_plugin_module()
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            state_dir = Path(tmp)
            ctx = FakePluginContext(
                {
                    "agent_id": "EDI",
                    "state_dir": str(state_dir),
                    "socket_path": str(state_dir / "missing.sock"),
                }
            )
            plugin.register(ctx)

            ctx.hooks["pre_llm_call"](
                session_id="session-1",
                turn_id="turn-1",
                platform="desktop",
            )
            ctx.hooks["on_session_end"](
                session_id="session-1",
                turn_id="turn-1",
                completed=True,
                interrupted=False,
            )
            ctx.hooks["pre_approval_request"](
                session_key="telegram:private-chat-12345",
                turn_id="turn-1",
                surface="gateway",
            )

            events = [
                json.loads(line)
                for line in (state_dir / "hermes.jsonl").read_text().splitlines()
            ]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["hook_event_name"], "UserPromptSubmit")
            self.assertEqual(events[0]["session_id"], "session-1")
            self.assertNotIn("private-chat-12345", json.dumps(events))

    def test_tool_hook_reuses_desktop_surface_from_same_session(self) -> None:
        plugin = load_plugin_module()
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            state_dir = Path(tmp)
            ctx = FakePluginContext(
                {
                    "agent_id": "EDI",
                    "state_dir": str(state_dir),
                    "socket_path": str(state_dir / "missing.sock"),
                }
            )
            plugin.register(ctx)

            ctx.hooks["pre_llm_call"](
                session_id="session-1",
                turn_id="turn-1",
                platform="desktop",
            )
            ctx.hooks["pre_tool_call"](
                session_id="session-1",
                turn_id="turn-1",
                tool_name="terminal",
            )

            events = [
                json.loads(line)
                for line in (state_dir / "hermes.jsonl").read_text().splitlines()
            ]
            self.assertEqual(events[-1]["surface"], "desktop")
            self.assertEqual(events[-1]["agent_origin"], "Hermes Desktop")

    def test_surface_cache_survives_plugin_reregistration(self) -> None:
        plugin = load_plugin_module()
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            state_dir = Path(tmp)
            config = {
                "agent_id": "EDI",
                "state_dir": str(state_dir),
                "socket_path": str(state_dir / "missing.sock"),
            }
            first_context = FakePluginContext(config)
            plugin.register(first_context)
            first_context.hooks["pre_llm_call"](
                session_id="session-1",
                turn_id="turn-1",
                platform="desktop",
            )

            second_context = FakePluginContext(config)
            reloaded_plugin = load_plugin_module()
            reloaded_plugin.register(second_context)
            second_context.hooks["pre_tool_call"](
                session_id="session-1",
                turn_id="turn-1",
                tool_name="terminal",
            )

            events = [
                json.loads(line)
                for line in (state_dir / "hermes.jsonl").read_text().splitlines()
            ]
            self.assertEqual(events[-1]["surface"], "desktop")
            self.assertEqual(events[-1]["agent_origin"], "Hermes Desktop")

    def test_string_false_disables_event_logging(self) -> None:
        plugin = load_plugin_module()
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            state_dir = Path(tmp)
            ctx = FakePluginContext(
                {
                    "agent_id": "EDI",
                    "state_dir": str(state_dir),
                    "socket_path": str(state_dir / "missing.sock"),
                    "log_events": "false",
                }
            )
            plugin.register(ctx)

            ctx.hooks["pre_llm_call"](
                session_id="session-1",
                turn_id="turn-1",
                platform="desktop",
            )

            self.assertFalse((state_dir / "hermes.jsonl").exists())

    def test_manifest_declares_cross_profile_shared_scope(self) -> None:
        manifest = (PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8")
        self.assertRegex(manifest, r"(?m)^profile_scope:\s*shared\s*$")

    def test_doctor_command_reports_transport_paths_as_json(self) -> None:
        plugin = load_plugin_module()
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            state_dir = Path(tmp)
            event_log = state_dir / "hermes.jsonl"
            event_log.write_text('{"hook_event_name":"SessionStart"}\n')
            ctx = FakePluginContext(
                {
                    "agent_id": "EDI",
                    "state_dir": str(state_dir),
                    "socket_path": str(state_dir / "missing.sock"),
                }
            )
            plugin.register(ctx)
            command = ctx.cli_commands["sidepulse"]
            parser = argparse.ArgumentParser()
            command["setup_fn"](parser)
            args = parser.parse_args(["doctor", "--json"])

            output = io.StringIO()
            with redirect_stdout(output):
                command["handler_fn"](args)
            report = json.loads(output.getvalue())

            self.assertEqual(report["agent_id"], "EDI")
            self.assertEqual(report["provider"], "hermes")
            self.assertEqual(report["state_dir"], str(state_dir))
            self.assertTrue(report["event_log_exists"])
            self.assertFalse(report["socket_exists"])
            self.assertEqual(report["socket_path"], str(state_dir / "missing.sock"))

    def test_cli_test_command_emits_requested_status(self) -> None:
        plugin = load_plugin_module()
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            state_dir = Path(tmp)
            ctx = FakePluginContext(
                {
                    "state_dir": str(state_dir),
                    "socket_path": str(state_dir / "missing.sock"),
                }
            )
            plugin.register(ctx)
            command = ctx.cli_commands["sidepulse"]
            parser = argparse.ArgumentParser()
            command["setup_fn"](parser)
            args = parser.parse_args(["test", "--mode", "done", "--json"])

            output = io.StringIO()
            with redirect_stdout(output):
                command["handler_fn"](args)
            report = json.loads(output.getvalue())

            self.assertEqual(report["mode"], "completed")
            self.assertTrue(report["logged"])
            self.assertFalse(report["delivered"])
            event = json.loads((state_dir / "hermes.jsonl").read_text().strip())
            self.assertEqual(event["hook_event_name"], "Stop")
            self.assertEqual(event["sidepulse_mode"], "completed")


class HermesCompatScriptTests(unittest.TestCase):
    def test_compare_hooks_fails_when_required_hook_disappears(self) -> None:
        script_path = PLUGIN_ROOT / "scripts" / "check_hermes_compat.py"
        spec = importlib.util.spec_from_file_location("sidepulse_hermes_compat", script_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        result = module.compare_hooks(
            ["pre_llm_call", "post_llm_call", "on_session_activate"],
            ["pre_llm_call", "post_llm_call"],
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["missing"], ["on_session_activate"])

    def test_plugin_yaml_lists_every_required_hook(self) -> None:
        script_path = PLUGIN_ROOT / "scripts" / "check_hermes_compat.py"
        spec = importlib.util.spec_from_file_location("sidepulse_hermes_compat_yaml", script_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        hooks = module.hooks_from_plugin_yaml(PLUGIN_ROOT / "plugin.yaml")
        self.assertEqual(sorted(hooks), sorted(module.REQUIRED_HOOKS))


if __name__ == "__main__":
    unittest.main()
