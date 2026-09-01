from __future__ import annotations

import json
import os
import plistlib
import re
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sidepulse.audit import (
    append_status_audit_record,
    append_status_history_record,
    default_status_history_log_path,
    export_status_audit_csv,
    export_status_audit_html,
    read_status_history_records,
    read_status_audit_records,
    status_history_record,
)
from sidepulse.battery import (
    BATTERY_CHARGING_MINT,
    BatteryLedController,
    BatterySnapshot,
    parse_ioreg_battery_plist,
    program_for_battery,
)
from sidepulse import collector as collector_module
from sidepulse import cli as cli_module
from sidepulse.collector import (
    AgentMonitor,
    LiveAgentMonitor,
    MonitorSnapshot,
    SourceSpec,
    default_sources,
)
from sidepulse.cli import build_parser, visible_watch_statuses
from sidepulse.device_writer import (
    DeviceWriteError,
    discover_devices,
    normalize_led_text,
    validate_led_text,
    write_led_program,
)
from sidepulse.hook import format_hook_payload, routed_hook_payload, write_hook_payload
from sidepulse.ipc import HookEventServer, send_hook_event
from sidepulse.install import (
    hook_command,
    install_claude_hooks,
    install_codex_hooks,
    install_grok_hooks,
    uninstall_claude_hooks,
    uninstall_codex_hooks,
    uninstall_grok_hooks,
    update_codex_trusted_hashes,
)
from sidepulse.keep_awake import KeepAwakeController, status_file_for_target
from sidepulse.led_status import (
    AgentLedController,
    LedDisplayState,
    apply_brightness,
    builtin_animation_file_name,
    builtin_animation_program,
    display_state_for_mode,
    led_count_for_target,
    program_for_agent_mode,
    program_for_display_state,
    write_mode_to_leds,
)
from sidepulse.lid_sleep import (
    ClosedLidAwakeController,
    IOREG_SLEEP_DISABLED_COMMAND,
    MacSleepSnapshot,
    PMSET_ASSERTIONS_COMMAND,
    SleepHelperRequiredError,
    closed_lid_awake_should_hold,
    parse_bool_ioreg_property,
    parse_pmset_assertions,
    read_mac_sleep_snapshot,
    run_sudo_pmset_disablesleep,
    sleep_helper_sudoers_rule,
)
from sidepulse.models import AgentMode, AgentStatus, AggregateStatus
from sidepulse.origin import ProcessInfo, origin_from_processes
from sidepulse.providers import (
    detect_grok_config,
    default_log_path,
    default_state_dir,
    parse_log_line,
)
from sidepulse.sd_eject_guard_launch import (
    SD_EJECT_GUARD_BINARY_NAME,
    SD_EJECT_GUARD_DISPLAY_NAME,
    SD_EJECT_GUARD_LABEL,
    SD_EJECT_GUARD_LOG_MAX_BYTES,
    SdEjectGuardInstallError,
    SdEjectGuardPaths,
    build_sd_eject_guard_plist,
    install_sd_eject_guard,
    read_log_tail,
    sd_eject_guard_installed,
    stop_sd_eject_guard,
    truncate_log_if_large,
    uninstall_sd_eject_guard,
)
from sidepulse.session_actions import (
    SESSION_OPEN_APP,
    SESSION_OPEN_TERMINAL,
    SESSION_OPEN_VSCODE,
    default_session_open_action,
    session_deep_link,
    session_open_target,
    session_resume_command,
    session_vscode_link,
)
from sidepulse.settings import (
    ANIMATION_STATE_LID_CLOSED,
    ANIMATION_STATE_LID_OPEN,
    ANIMATION_STATES,
    AGENT_ANIMATION_AMBER_PULSE,
    AGENT_ANIMATION_CYAN_COMPLETE,
    AGENT_ANIMATION_CYAN_ROLL,
    AGENT_ANIMATION_CUSTOM,
    AGENT_ANIMATION_DEFAULT,
    AGENT_ANIMATION_EMBER_ATTENTION,
    AGENT_ANIMATION_EMBER_COMPLETE,
    AGENT_ANIMATION_EMBER_IDLE,
    AGENT_ANIMATION_EMBER_LID_OPEN,
    AGENT_ANIMATION_EMBER_TIDE,
    AGENT_ANIMATION_IDLE_PULSE,
    AGENT_ANIMATION_KITT,
    AGENT_ANIMATION_KITT_RED,
    AGENT_ANIMATION_LID_CLOSED,
    AGENT_ANIMATION_LID_OPEN,
    AGENT_ANIMATION_NIGHT_RIDER,
    AGENT_ANIMATION_IMMEDIATE_OFF,
    AGENT_ANIMATION_SLOW_OFF,
    AGENT_ANIMATION_PROFILE_CYAN,
    AGENT_ANIMATION_PROFILE_EMBER,
    AGENT_ANIMATION_PROFILE_PURPLE,
    AGENT_ANIMATION_PURPLE_ATTENTION,
    AGENT_ANIMATION_PURPLE_COMPLETE,
    AGENT_ANIMATION_PURPLE_IDLE,
    AGENT_ANIMATION_PURPLE_LID_OPEN,
    AGENT_ANIMATION_PURPLE_TIDE,
    AGENT_ANIMATION_SOLID_GREEN,
    AGENT_ANIMATION_WORKING_MODES,
    CLOSED_LID_AWAKE_AGENTS,
    CLOSED_LID_AWAKE_ALWAYS,
    CLOSED_LID_AWAKE_NEVER,
    DEFAULT_IDLE_TIMEOUT_SECONDS,
    DEFAULT_HISTORY_TIMEFRAME_SECONDS,
    DEFAULT_RECENT_SESSION_RETENTION_SECONDS,
    HISTORY_TIMEFRAME_24H_SECONDS,
    HISTORY_TIMEFRAME_48H_SECONDS,
    LID_ANIMATION_CLOSED,
    LID_ANIMATION_OPEN,
    LED_DISPLAY_CUSTOM,
    SLEEP_PREVENTION_AGENTS,
    SLEEP_PREVENTION_ALWAYS,
    SLEEP_PREVENTION_NEVER,
    TERMINAL_APP_ALACRITTY,
    TERMINAL_APP_CUSTOM,
    TERMINAL_APP_GHOSTTY,
    TERMINAL_APP_ITERM,
    TERMINAL_APP_KITTY,
    TERMINAL_APP_TERMINAL,
    TERMINAL_APP_WARP,
    TERMINAL_APP_WEZTERM,
    AgentMonitorSettings,
    DeviceDisplaySetting,
    agent_animation_profile_document,
    agent_animation_profile_id,
    builtin_agent_animation_profiles,
    custom_agent_animation_id,
    default_agent_animation_id,
    default_config_dir,
    default_lid_animation,
    default_settings_path,
    load_settings,
    save_settings,
    with_imported_agent_animation_profile,
)
from sidepulse.status_bar_launch import (
    LAUNCH_AGENT_LABEL,
    STATUS_BAR_DISPLAY_NAME,
    build_launch_agent_plist,
    build_status_bar_launcher_script,
    install_launch_agent,
    launch_agent_installed,
)


class FakeProcess:
    def __init__(self) -> None:
        self.terminated = False
        self.killed = False

    def poll(self):
        return 0 if self.terminated or self.killed else None

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self) -> None:
        self.killed = True


class AgentMonitorTests(unittest.TestCase):
    def test_aggregates_highest_priority_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            codex = base / "codex.jsonl"
            claude = base / "claude.jsonl"

            codex.write_text(
                json.dumps(
                    {
                        "logged_at": "2026-06-20T06:00:00Z",
                        "event": {
                            "hook_event_name": "PreToolUse",
                            "session_id": "codex-session",
                            "tool_name": "Bash",
                        },
                    }
                )
                + "\n"
            )
            claude.write_text(
                json.dumps(
                    {
                        "logged_at": "2026-06-20T06:00:01Z",
                        "hook_event_name": "Notification",
                        "session_id": "claude-session",
                        "notification_type": "idle_prompt",
                        "message": "Claude is waiting for your input",
                    }
                )
                + "\n"
            )

            monitor = AgentMonitor(
                sources=(SourceSpec("codex", codex), SourceSpec("claude", claude)),
                stale_after_seconds=999999999,
                tool_running_timeout_seconds=0,
            )
            snapshot = monitor.snapshot()

            self.assertEqual(snapshot.aggregate.mode, AgentMode.WAITING_FOR_INPUT)
            self.assertEqual(len(snapshot.statuses), 2)

    def test_hook_log_writes_provider_formats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            codex = base / "codex.jsonl"
            claude = base / "claude.jsonl"
            grok = base / "grok.jsonl"

            write_hook_payload(
                "codex",
                codex,
                '{"hook_event_name":"Stop","session_id":"abc"}',
            )
            write_hook_payload(
                "claude",
                claude,
                '{"hook_event_name":"Stop","session_id":"xyz"}',
            )
            write_hook_payload(
                "grok",
                grok,
                '{"hookEventName":"stop","sessionId":"grok-session"}',
            )

            codex_obj = json.loads(codex.read_text())
            claude_obj = json.loads(claude.read_text())
            grok_obj = json.loads(grok.read_text())

            self.assertIn("event", codex_obj)
            self.assertEqual(codex_obj["event"]["session_id"], "abc")
            self.assertNotIn("event", claude_obj)
            self.assertEqual(claude_obj["session_id"], "xyz")
            self.assertNotIn("event", grok_obj)
            self.assertEqual(grok_obj["sessionId"], "grok-session")
            self.assertTrue(
                datetime.fromisoformat(codex_obj["logged_at"].replace("Z", "+00:00")).tzinfo
                is not None
            )

    def test_hook_payload_stamps_origin_from_vscode_environment(self) -> None:
        with patch.dict(os.environ, {"TERM_PROGRAM": "vscode"}, clear=True):
            line = format_hook_payload(
                "claude",
                '{"hook_event_name":"UserPromptSubmit","session_id":"claude-session","prompt":"hi"}',
            )

        self.assertEqual(line["agent_origin"], "Claude in VS Code")
        record = parse_log_line("claude", json.dumps(line))
        self.assertIsNotNone(record)
        self.assertEqual(record.origin, "Claude in VS Code")
        status = collector_module.status_from_event(record)
        self.assertIsNotNone(status)
        self.assertEqual(status.origin, "Claude in VS Code")

    def test_codex_hook_payload_stamps_origin_from_app_process(self) -> None:
        processes = (
            ProcessInfo(
                pid=100,
                ppid=1,
                comm="/Applications/ChatGPT.app/Contents/MacOS/ChatGPT",
                command="/Applications/ChatGPT.app/Contents/MacOS/ChatGPT",
            ),
        )
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("sidepulse.origin.process_ancestry", return_value=processes),
        ):
            line = format_hook_payload(
                "codex",
                '{"hook_event_name":"UserPromptSubmit","session_id":"codex-session","prompt":"hi"}',
            )

        self.assertEqual(line["event"]["agent_origin"], "Codex UI")
        record = parse_log_line("codex", json.dumps(line))
        self.assertIsNotNone(record)
        self.assertEqual(record.origin, "Codex UI")

    def test_origin_process_detection_distinguishes_claude_surfaces(self) -> None:
        self.assertEqual(
            origin_from_processes(
                "claude",
                (
                    ProcessInfo(
                        pid=100,
                        ppid=1,
                        comm="/Applications/Visual Studio Code.app/Contents/MacOS/Electron",
                        command="/Applications/Visual Studio Code.app/Contents/MacOS/Electron",
                    ),
                ),
            ).label,
            "Claude in VS Code",
        )
        self.assertEqual(
            origin_from_processes(
                "claude",
                (ProcessInfo(pid=100, ppid=1, comm="/opt/homebrew/bin/claude", command="claude"),),
            ).label,
            "Claude Code CLI",
        )

    def test_grok_log_line_normalizes_camel_case_payload(self) -> None:
        record = parse_log_line(
            "grok",
            json.dumps(
                {
                    "hookEventName": "pre_tool_use",
                    "sessionId": "grok-session",
                    "workspaceRoot": "/tmp/project",
                    "toolName": "run_terminal_command",
                    "toolInput": {"command": "date"},
                    "timestamp": "2026-07-18T12:00:00Z",
                }
            ),
        )

        self.assertIsNotNone(record)
        self.assertEqual(record.event_name, "PreToolUse")
        self.assertEqual(record.session_id, "grok-session")
        self.assertEqual(record.cwd, "/tmp/project")
        self.assertEqual(record.tool_name, "run_terminal_command")
        self.assertEqual(record.raw["tool_input"], {"command": "date"})

    def test_claude_compat_grok_payload_is_inferred_as_grok(self) -> None:
        record = parse_log_line(
            "claude",
            json.dumps(
                {
                    "hookEventName": "notification",
                    "sessionId": "019f7724-da8c-7df0-b41d-bda99e0cac9f",
                    "workspaceRoot": "/Users/pero/git/ai_food/",
                    "transcriptPath": (
                        "/Users/pero/.grok/sessions/%2FUsers%2Fpero%2Fgit%2Fai_food/"
                        "019f7724-da8c-7df0-b41d-bda99e0cac9f/updates.jsonl"
                    ),
                    "notificationType": "idle_prompt",
                    "message": "Turn complete",
                    "timestamp": "2026-07-18T21:55:14Z",
                }
            ),
        )

        self.assertIsNotNone(record)
        self.assertEqual(record.provider, "grok")
        self.assertEqual(record.event_name, "Notification")
        status = collector_module.status_from_event(
            record,
            collector_module.StatusMetadata(cwd=record.cwd, title="ai_food"),
        )
        self.assertIsNotNone(status)
        self.assertEqual(status.mode, AgentMode.COMPLETED)
        self.assertEqual(status.display_name, "ai_food (019f7724)")

    def test_claude_waiting_notification_still_requires_input(self) -> None:
        record = parse_log_line(
            "claude",
            json.dumps(
                {
                    "hook_event_name": "Notification",
                    "session_id": "claude-session",
                    "notification_type": "idle_prompt",
                    "message": "Claude is waiting for your input",
                    "logged_at": "2026-07-18T21:55:14Z",
                }
            ),
        )

        self.assertIsNotNone(record)
        status = collector_module.status_from_event(record)
        self.assertIsNotNone(status)
        self.assertEqual(status.mode, AgentMode.WAITING_FOR_INPUT)

    def test_claude_compat_grok_payload_routes_to_grok_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            claude = base / "claude.jsonl"
            grok = base / "grok.jsonl"
            payload = json.dumps(
                {
                    "hookEventName": "notification",
                    "sessionId": "grok-session",
                    "workspaceRoot": "/tmp/project",
                    "transcriptPath": "/Users/pero/.grok/sessions/project/grok-session/updates.jsonl",
                    "notificationType": "idle_prompt",
                    "message": "Turn complete",
                }
            )

            with (
                patch("sidepulse.hook.detect_log_path", return_value=grok),
                patch.dict(os.environ, {"TERM_PROGRAM": "Apple_Terminal"}, clear=True),
            ):
                provider, path, line = routed_hook_payload("claude", claude, payload)

            self.assertEqual(provider, "grok")
            self.assertEqual(path, grok)
            self.assertEqual(line["sessionId"], "grok-session")
            self.assertEqual(line["agent_origin"], "Grok CLI")

    def test_status_audit_log_exports_csv_and_html(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            log = base / "event-status.jsonl"
            record = parse_log_line(
                "grok",
                json.dumps(
                    {
                        "hookEventName": "notification",
                        "sessionId": "grok-session",
                        "workspaceRoot": "/tmp/project",
                        "notificationType": "idle_prompt",
                        "message": "Turn complete",
                        "timestamp": "2026-07-18T21:55:14Z",
                    }
                ),
            )
            self.assertIsNotNone(record)
            status = collector_module.status_from_event(record)
            self.assertIsNotNone(status)

            append_status_audit_record(record, status, path=log)
            records = read_status_audit_records(log)

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["hook_event"], "Notification")
            self.assertEqual(records[0]["status"], "completed")
            csv_path = base / "debug.csv"
            html_path = base / "debug.html"
            self.assertEqual(export_status_audit_csv(csv_path, source=log), 1)
            self.assertEqual(export_status_audit_html(html_path, source=log), 1)
            self.assertIn("hook_event,status", csv_path.read_text())
            self.assertIn("SidePulse Agent Debug Log", html_path.read_text())

    def test_status_history_record_includes_charger_power_and_sleep_state(self) -> None:
        record = status_history_record(
            agent_mode=AgentMode.WORKING.value,
            display_status="Working",
            battery=BatterySnapshot(
                percent=57,
                is_charging=True,
                is_plugged=True,
                battery_watts=42.53,
                adapter_connected=True,
                adapter_watts=86,
                adapter_voltage=20.2,
                adapter_current=4.25,
                adapter_name="USB-C Power Adapter",
                adapter_manufacturer="Apple",
                adapter_model="A2166",
            ),
            mac_sleep=MacSleepSnapshot(
                sleep_disabled=False,
                prevent_system_sleep=True,
                prevent_user_idle_system_sleep=False,
                prevent_user_idle_display_sleep=True,
                user_is_active=True,
            ),
            lid_closed=False,
            keep_awake_requested=True,
            keep_awake_active=True,
            sleep_prevention_policy=SLEEP_PREVENTION_AGENTS,
            sleep_prevention_battery_safeguard_active=False,
            sleep_prevention_min_battery_percent=20,
            closed_lid_awake_requested=False,
            closed_lid_awake_active=False,
            recorded_at=datetime(2026, 7, 20, 12, 30, tzinfo=timezone.utc),
        )

        self.assertEqual(record["recorded_at"], "2026-07-20T12:30:00Z")
        self.assertEqual(record["agent_status"], AgentMode.WORKING.value)
        self.assertEqual(record["display_status"], "Working")
        self.assertEqual(record["battery_level"], 57)
        self.assertEqual(record["battery_power_watts"], 42.53)
        self.assertTrue(record["charger_connected"])
        self.assertTrue(record["adapter_connected"])
        self.assertEqual(record["charger_power_watts"], 86.0)
        self.assertEqual(record["adapter_watts"], 86)
        self.assertEqual(record["adapter_voltage"], 20.2)
        self.assertEqual(record["adapter_current"], 4.25)
        self.assertEqual(record["adapter_name"], "USB-C Power Adapter")
        self.assertEqual(record["sleep_prevention_policy"], SLEEP_PREVENTION_AGENTS)
        self.assertFalse(record["sleep_prevention_battery_safeguard_active"])
        self.assertEqual(record["sleep_prevention_min_battery_percent"], 20)
        self.assertEqual(record["mac_sleep_status"], "prevented")
        self.assertTrue(record["mac_sleep_prevented"])
        self.assertEqual(record["lid_status"], "open")

    def test_status_history_log_round_trips_and_uses_xdg_state_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "status-history.jsonl"
            first = {"recorded_at": "2026-07-20T12:00:00Z", "display_status": "Idle"}
            second = {"recorded_at": "2026-07-20T12:01:00Z", "display_status": "Done"}

            append_status_history_record(first, path=log)
            append_status_history_record(second, path=log)

            self.assertEqual(read_status_history_records(log), [first, second])
            self.assertEqual(read_status_history_records(log, limit=1), [second])
            self.assertEqual(
                default_status_history_log_path(Path("/Users/example")),
                Path("/Users/example")
                / ".local"
                / "state"
                / "sidepulse"
                / "agent-monitor"
                / "status-history.jsonl",
            )

    def test_status_history_log_write_errors_are_visible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            blocking_file = Path(tmp) / "not-a-directory"
            blocking_file.write_text("", encoding="utf-8")

            with self.assertRaises(OSError):
                append_status_history_record(
                    {"recorded_at": "2026-07-20T12:00:00Z"},
                    path=blocking_file / "status-history.jsonl",
                )

    def test_hook_event_server_receives_socket_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            received: list[tuple[str, dict]] = []
            server = HookEventServer(
                lambda provider, line: received.append((provider, line)),
                socket_path=Path(tmp) / "events.sock",
            )
            try:
                server.start()
                sent = send_hook_event(
                    "codex",
                    {
                        "logged_at": "2026-06-20T06:00:00Z",
                        "event": {
                            "hook_event_name": "Stop",
                            "session_id": "codex-session",
                            "last_assistant_message": "Done.",
                        },
                    },
                    socket_path=server.socket_path,
                    timeout=0.5,
                )

                deadline = time.time() + 1
                while sent and not received and time.time() < deadline:
                    time.sleep(0.01)

                self.assertTrue(sent)
                self.assertTrue(received)
                self.assertEqual(received[0][0], "codex")
                self.assertEqual(
                    received[0][1]["event"]["hook_event_name"],
                    "Stop",
                )
            finally:
                server.stop()

    def test_live_sidepulse_ingests_events_and_persists_latest_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            latest = base / "latest.json"
            source = SourceSpec("event-bus", base / "events.sock")
            monitor = LiveAgentMonitor(
                sources=(source,),
                stale_after_seconds=3600,
                latest_state_path=latest,
            )
            line = {
                "logged_at": datetime.now(timezone.utc).isoformat(),
                "event": {
                    "hook_event_name": "PreToolUse",
                    "session_id": "codex-session",
                    "cwd": "/tmp/project",
                    "tool_name": "Bash",
                    "agent_origin": "Codex UI",
                },
            }
            record = parse_log_line("codex", json.dumps(line))

            self.assertIsNotNone(record)
            monitor.ingest_record(record)
            snapshot = monitor.snapshot()

            self.assertEqual(snapshot.aggregate.mode, AgentMode.TOOL_RUNNING)
            self.assertEqual(snapshot.statuses[0].tool_name, "Bash")
            self.assertEqual(snapshot.statuses[0].origin, "Codex UI")
            self.assertTrue(latest.exists())

            reloaded = LiveAgentMonitor(
                sources=(source,),
                stale_after_seconds=3600,
                latest_state_path=latest,
            )
            self.assertEqual(reloaded.snapshot().aggregate.mode, AgentMode.TOOL_RUNNING)
            self.assertEqual(reloaded.snapshot().statuses[0].origin, "Codex UI")

    def test_status_bar_session_menu_title_is_task_and_project(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        now = datetime.now(timezone.utc)
        status = AgentStatus(
            provider="codex",
            agent_id="codex:session:019ee395",
            display_name="sidepulse: Refine README agent status modes (019ee395)",
            mode=AgentMode.COMPLETED,
            updated_at=now,
            event_name="Stop",
            session_id="019ee395",
            cwd="/Users/pero/pgit/sidepulse",
        )

        self.assertEqual(
            status_bar.menu_title_for_status(status, now),
            "Done  Refine README agent status modes\nsidepulse",
        )
        self.assertEqual(
            status_bar.session_detail_for_status(status, now).split(" · ")[0],
            "Done",
        )

    def test_status_bar_session_menu_title_suppresses_duplicate_project(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        now = datetime.now(timezone.utc)
        status = AgentStatus(
            provider="grok",
            agent_id="grok:session:019f7724",
            display_name="ai_food (019f7724)",
            mode=AgentMode.WAITING_FOR_INPUT,
            updated_at=now,
            event_name="Notification",
            session_id="019f7724",
            cwd="/Users/pero/git/ai_food",
        )

        self.assertEqual(status_bar.menu_title_for_status(status, now), "Ask  ai_food")

    def test_status_bar_grok_provider_uses_badge_icon(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        status = AgentStatus(
            provider="grok",
            agent_id="grok:session:abc",
            display_name="Grok abc",
            mode=AgentMode.WORKING,
            updated_at=datetime.now(timezone.utc),
            event_name="PreToolUse",
        )

        image = status_bar.provider_icon_for_status(status)

        self.assertIsNotNone(image)
        self.assertEqual(image.size().width, 18)
        self.assertEqual(image.size().height, 18)

    def test_status_bar_vscode_origin_uses_composite_app_icon(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        status = AgentStatus(
            provider="claude",
            agent_id="claude:session:abc",
            display_name="Claude abc",
            mode=AgentMode.WORKING,
            updated_at=datetime.now(timezone.utc),
            event_name="PreToolUse",
            origin="Claude in VS Code",
        )

        image = status_bar.session_origin_icon_for_status(status)

        self.assertIsNotNone(image)
        self.assertEqual(image.size().width, 24)
        self.assertEqual(image.size().height, 18)

    def test_status_bar_session_row_icon_combines_status_and_origin(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        status = AgentStatus(
            provider="claude",
            agent_id="claude:session:abc",
            display_name="Claude abc",
            mode=AgentMode.COMPLETED,
            updated_at=datetime.now(timezone.utc),
            event_name="Stop",
            origin="Claude in VS Code",
        )

        image = status_bar.session_row_icon_for_status(status)

        self.assertIsNotNone(image)
        self.assertGreater(image.size().width, 38)
        self.assertEqual(image.size().height, 18)

    def test_virtual_screen_bar_frame_covers_notch_plus_led_band(self) -> None:
        try:
            from sidepulse import virtual_device
        except (ImportError, SystemExit) as exc:
            self.skipTest(str(exc))

        screen = SimpleNamespace(
            frame=lambda: SimpleNamespace(
                origin=SimpleNamespace(x=0.0, y=0.0),
                size=SimpleNamespace(width=1512.0, height=982.0),
            ),
            safeAreaInsets=lambda: SimpleNamespace(top=32.0),
            auxiliaryTopLeftArea=lambda: SimpleNamespace(
                origin=SimpleNamespace(x=0.0, y=0.0),
                size=SimpleNamespace(width=640.0, height=24.0),
            ),
            auxiliaryTopRightArea=lambda: SimpleNamespace(
                origin=SimpleNamespace(x=872.0, y=0.0),
                size=SimpleNamespace(width=640.0, height=24.0),
            ),
        )

        self.assertEqual(
            virtual_device.virtual_window_frame_for_screen(screen),
            ((640.0, 945.0), (232.0, 37.0)),
        )
        self.assertEqual(
            virtual_device.led_band_rect(232.0),
            ((0.0, 0.0), (232.0, 5.0)),
        )

    def test_virtual_screen_bar_on_notchless_display_is_led_band_only(self) -> None:
        try:
            from sidepulse import virtual_device
        except (ImportError, SystemExit) as exc:
            self.skipTest(str(exc))

        screen = SimpleNamespace(
            frame=lambda: SimpleNamespace(
                origin=SimpleNamespace(x=0.0, y=0.0),
                size=SimpleNamespace(width=1920.0, height=1080.0),
            ),
            safeAreaInsets=lambda: SimpleNamespace(top=0.0),
            auxiliaryTopLeftArea=lambda: SimpleNamespace(
                origin=SimpleNamespace(x=0.0, y=0.0),
                size=SimpleNamespace(width=0.0, height=0.0),
            ),
            auxiliaryTopRightArea=lambda: SimpleNamespace(
                origin=SimpleNamespace(x=0.0, y=0.0),
                size=SimpleNamespace(width=0.0, height=0.0),
            ),
        )

        self.assertFalse(virtual_device.screen_has_notch(screen))
        self.assertEqual(
            virtual_device.virtual_window_frame_for_screen(screen),
            ((850.0, 1075.0), (220.0, 5.0)),
        )

    def test_virtual_screen_bar_redraws_at_60fps(self) -> None:
        try:
            from sidepulse import virtual_device
        except (ImportError, SystemExit) as exc:
            self.skipTest(str(exc))

        self.assertEqual(virtual_device.FRAME_RATE, 60.0)
        self.assertAlmostEqual(virtual_device.FRAME_INTERVAL, 1.0 / 60.0)

    def test_compact_preview_loops_one_shot_program_without_changing_loops(self) -> None:
        try:
            from sidepulse import virtual_device
        except (ImportError, SystemExit) as exc:
            self.skipTest(str(exc))

        one_shot = "off 90ms\n#00E5FF 180ms ease\noff 320ms"
        self.assertEqual(
            virtual_device.compact_preview_program(one_shot),
            f"{one_shot}\nrepeat",
        )
        looping = "#00E5FF 180ms ease\nrepeat"
        self.assertEqual(virtual_device.compact_preview_program(looping), looping)

        shutdown = (
            "0:#000000 75ms ease; 7:#000000 75ms ease\n"
            "1:#000000 75ms ease; 6:#000000 75ms ease"
        )
        shutdown_preview = virtual_device.compact_preview_program(shutdown)
        self.assertTrue(shutdown_preview.startswith("#00E5FF\n"))
        self.assertTrue(shutdown_preview.endswith("\nrepeat"))
        self.assertNotIn("#00E5FF", shutdown)

    def test_led_wasm_controller_uses_packaged_firmware_engine(self) -> None:
        try:
            from sidepulse.led_wasm import LedWasmUnavailableError, SdLedWasmController
        except ImportError as exc:
            self.skipTest(str(exc))

        try:
            controller = SdLedWasmController(led_count=8)
        except LedWasmUnavailableError as exc:
            self.skipTest(str(exc))

        result = controller.parse("brightness 128\n#00FF66", 0)

        self.assertTrue(result.ok)
        self.assertEqual(controller.step(0), [(0, 128, 51)] * 8)

    def test_led_wasm_controller_supports_sidepulse_dot_led_count(self) -> None:
        try:
            from sidepulse.led_wasm import LedWasmUnavailableError, SdLedWasmController
        except ImportError as exc:
            self.skipTest(str(exc))

        try:
            controller = SdLedWasmController(led_count=2)
        except LedWasmUnavailableError as exc:
            self.skipTest(str(exc))

        result = controller.parse("0:#FF0000; 1:#00FF00; 7:#FFFFFF", 0)

        self.assertTrue(result.ok)
        self.assertEqual(controller.step(0), [(255, 0, 0), (0, 255, 0)])

    def test_core_animation_programs_transition_from_current_pixels(self) -> None:
        try:
            from sidepulse.led_wasm import LedWasmUnavailableError, SdLedWasmController
        except ImportError as exc:
            self.skipTest(str(exc))

        try:
            controller = SdLedWasmController(led_count=8)
        except LedWasmUnavailableError as exc:
            self.skipTest(str(exc))

        controller.parse(builtin_animation_program("cyan-roll", 8), 0)
        working_pixels = controller.step(700)
        controller.parse(builtin_animation_program("solid-green", 8), 700)
        self.assertEqual(controller.step(700), working_pixels)
        transition_pixels = controller.step(860)
        self.assertNotEqual(transition_pixels, working_pixels)
        self.assertNotEqual(transition_pixels, [(0, 255, 102)] * 8)
        self.assertEqual(controller.step(1020), [(0, 255, 102)] * 8)

        controller.parse(builtin_animation_program("lid-closed", 8), 1020)
        green = (0, 255, 102)
        black = (0, 0, 0)
        self.assertEqual(controller.step(1020), [green] * 8)
        self.assertEqual(
            controller.step(1095),
            [black, green, green, green, green, green, green, black],
        )
        self.assertEqual(
            controller.step(1170),
            [black, black, green, green, green, green, black, black],
        )
        self.assertEqual(
            controller.step(1245),
            [black, black, black, green, green, black, black, black],
        )
        self.assertEqual(controller.step(1320), [black] * 8)

    def test_virtual_screen_bar_led_blend_spans_three_leds(self) -> None:
        try:
            from sidepulse import virtual_device
        except (ImportError, SystemExit) as exc:
            self.skipTest(str(exc))

        led_width = 10.0
        target_center = 35.0
        colors = [(0.0, 0.0, 0.0, 0.0)] * 8
        colors[3] = (0.0, 1.0, 0.0, 1.0)

        self.assertAlmostEqual(
            virtual_device.blended_led_color_at_x(colors, target_center, led_width)[1],
            1.0,
        )
        self.assertGreater(
            virtual_device.blended_led_color_at_x(
                colors, target_center - led_width, led_width
            )[1],
            0.0,
        )
        self.assertGreater(
            virtual_device.blended_led_color_at_x(
                colors, target_center + led_width, led_width
            )[1],
            0.0,
        )
        self.assertAlmostEqual(
            virtual_device.blended_led_color_at_x(
                colors, target_center - led_width * 1.5, led_width
            )[1],
            0.0,
        )
        self.assertAlmostEqual(
            virtual_device.blended_led_color_at_x(
                colors, target_center + led_width * 1.5, led_width
            )[1],
            0.0,
        )

    def test_status_bar_session_row_has_inline_options(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        now = datetime.now(timezone.utc)
        status = AgentStatus(
            provider="claude",
            agent_id="claude:session:abc",
            display_name="Claude abc",
            mode=AgentMode.WAITING_FOR_INPUT,
            updated_at=now,
            event_name="Notification",
            session_id="1ca4348e-2aec-4147-9e81-d7d56364d257",
            cwd="/Users/pero/pgit/sdstatus_bitbang",
        )
        target = SimpleNamespace(settings=AgentMonitorSettings())

        row = status_bar.build_session_menu_item(status, now, target)
        options = status_bar.build_session_options_menu(status, now, target)
        titles = [
            options.itemAtIndex_(index).title()
            for index in range(options.numberOfItems())
            if options.itemAtIndex_(index).title()
        ]

        self.assertEqual(row.title(), status_bar.native_session_menu_title(status))
        self.assertIsNotNone(row.image())
        self.assertIsNone(row.submenu())
        self.assertIsNone(row.view())
        self.assertEqual(row.representedObject(), status)
        self.assertTrue(any(title.startswith("Ask  Claude abc") for title in titles))
        self.assertIn("Open in VS Code", titles)
        self.assertIn("Resume in Terminal", titles)

    def test_status_bar_native_session_row_uses_task_title(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "peterkuhar.com"
            cwd = project / "functions"
            (project / ".git").mkdir(parents=True)
            cwd.mkdir()
            status = AgentStatus(
                provider="claude",
                agent_id="claude:session:b64a0d4b",
                display_name=(
                    "functions: allow me to chose timeframe "
                    "http://localhost:5001/pkuhar-com/us-central... (b64a0d4b)"
                ),
                mode=AgentMode.WORKING,
                updated_at=datetime.now(timezone.utc),
                event_name="PostToolUse",
                session_id="b64a0d4b-d828-4133-abb3-bdb4fafa7719",
                cwd=str(cwd),
                origin="Claude in VS Code",
            )

            title = status_bar.native_session_menu_title(status)

        self.assertIn("allow me to chose timeframe", title)
        self.assertIn("peterkuhar.com", title)
        self.assertNotIn("Working", title)
        self.assertNotIn("Claude in VS Code", title)
        self.assertNotEqual(title, "Working  Claude in VS Code  functions")

    def test_status_bar_session_row_shows_origin_when_known(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        now = datetime.now(timezone.utc)
        status = AgentStatus(
            provider="claude",
            agent_id="claude:session:abc",
            display_name="Claude abc",
            mode=AgentMode.WAITING_FOR_INPUT,
            updated_at=now,
            event_name="Notification",
            session_id="1ca4348e-2aec-4147-9e81-d7d56364d257",
            cwd="/Users/pero/pgit/sdstatus_bitbang",
            origin="Claude in VS Code",
        )

        self.assertTrue(
            status_bar.menu_title_for_status(status, now).startswith(
                "Ask  Claude in VS Code  Claude abc"
            )
        )
        self.assertIn("Claude in VS Code", status_bar.session_detail_for_status(status, now))
        self.assertEqual(status_bar.primary_session_open_action(status), SESSION_OPEN_VSCODE)

        target = SimpleNamespace(
            settings=AgentMonitorSettings().with_session_open_action(
                "claude",
                SESSION_OPEN_TERMINAL,
                "Claude in VS Code",
            )
        )
        options = status_bar.build_session_options_menu(status, now, target)
        by_title = {
            options.itemAtIndex_(index).title(): options.itemAtIndex_(index)
            for index in range(options.numberOfItems())
            if options.itemAtIndex_(index).title()
        }
        self.assertEqual(by_title["Resume in Terminal"].state(), 1)
        self.assertEqual(by_title["Open in VS Code"].state(), 0)

    def test_status_bar_recent_statuses_keeps_distinct_sessions_with_same_title(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        now = datetime.now(timezone.utc)
        older = AgentStatus(
            provider="grok",
            agent_id="grok:session:019ffd2e-2060-7ff2-842f-761cb458ccf4",
            display_name="msdosfs: What is here (019ffd2e)",
            mode=AgentMode.COMPLETED,
            updated_at=now - timedelta(seconds=20),
            event_name="Stop",
            session_id="019ffd2e-2060-7ff2-842f-761cb458ccf4",
            cwd="/Users/pero/temp/msdosfs",
            origin="Grok CLI",
        )
        newer = AgentStatus(
            provider="grok",
            agent_id="grok:session:019ffd37-1458-7d92-b077-3d0f92aedde4",
            display_name="msdosfs: What is here (019ffd37)",
            mode=AgentMode.COMPLETED,
            updated_at=now - timedelta(seconds=4),
            event_name="Stop",
            session_id="019ffd37-1458-7d92-b077-3d0f92aedde4",
            cwd="/Users/pero/temp/msdosfs",
            origin="Grok CLI",
        )
        different_folder = AgentStatus(
            provider="grok",
            agent_id="grok:session:other",
            display_name="other: What is here (019ffd99)",
            mode=AgentMode.COMPLETED,
            updated_at=now - timedelta(seconds=2),
            event_name="Stop",
            session_id="019ffd99-0000-0000-0000-000000000000",
            cwd="/Users/pero/temp/other",
            origin="Grok CLI",
        )
        snapshot = MonitorSnapshot(
            aggregate=AggregateStatus(AgentMode.COMPLETED, 0, 0, newer),
            statuses=(older, newer, different_folder),
            stale_statuses=(),
            sources=(),
            collected_at=now,
        )

        statuses = status_bar.recent_statuses(snapshot)

        self.assertEqual(
            [status.agent_id for status in statuses],
            [
                "grok:session:other",
                "grok:session:019ffd37-1458-7d92-b077-3d0f92aedde4",
                "grok:session:019ffd2e-2060-7ff2-842f-761cb458ccf4",
            ],
        )

    def test_status_bar_disambiguates_same_visible_session_titles(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        now = datetime.now(timezone.utc)
        older = AgentStatus(
            provider="grok",
            agent_id="grok:session:019ffd2e-2060-7ff2-842f-761cb458ccf4",
            display_name="msdosfs: What is here (019ffd2e)",
            mode=AgentMode.COMPLETED,
            updated_at=now - timedelta(seconds=20),
            event_name="Stop",
            session_id="019ffd2e-2060-7ff2-842f-761cb458ccf4",
            cwd="/Users/pero/temp/msdosfs",
            origin="Grok CLI",
        )
        newer = AgentStatus(
            provider="grok",
            agent_id="grok:session:019ffd37-1458-7d92-b077-3d0f92aedde4",
            display_name="msdosfs: What is here (019ffd37)",
            mode=AgentMode.COMPLETED,
            updated_at=now - timedelta(seconds=4),
            event_name="Stop",
            session_id="019ffd37-1458-7d92-b077-3d0f92aedde4",
            cwd="/Users/pero/temp/msdosfs",
            origin="Grok CLI",
        )
        snapshot = MonitorSnapshot(
            aggregate=AggregateStatus(AgentMode.COMPLETED, 0, 0, newer),
            statuses=(older, newer),
            stale_statuses=(),
            sources=(),
            collected_at=now,
        )
        target = SimpleNamespace(
            settings=AgentMonitorSettings(),
            closed_lid_awake=SimpleNamespace(last_error=None),
            status_bar_devices=lambda: [],
        )

        menu = status_bar.build_menu(snapshot, status_bar.STATE_DONE, target)
        titles = [
            menu.itemAtIndex_(index).title()
            for index in range(menu.numberOfItems())
            if menu.itemAtIndex_(index).title()
        ]

        self.assertIn("What is here (019ffd37)  msdosfs", titles)
        self.assertIn("What is here (019ffd2e)  msdosfs", titles)

    def test_status_bar_recent_statuses_coalesce_subagents_by_session(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        now = datetime.now(timezone.utc)
        session_id = "f9ccc24e-3dad-4607-95e6-4142428a93cc"
        main = AgentStatus(
            provider="claude",
            agent_id=f"claude:session:{session_id}",
            display_name=f"peterkuhar.com: so all good? ({session_id[:8]})",
            mode=AgentMode.COMPLETED,
            updated_at=now - timedelta(seconds=40),
            event_name="SessionEnd",
            session_id=session_id,
            cwd="/Users/pero/pgit/peterkuhar.com",
            origin="Claude Code CLI",
        )
        subagent = AgentStatus(
            provider="claude",
            agent_id="claude:agent:ac7f1721ec697b403",
            display_name="peterkuhar.com: what is this repo about? (agent ac7f1721)",
            mode=AgentMode.COMPLETED,
            updated_at=now - timedelta(seconds=4),
            event_name="SubagentStop",
            session_id=session_id,
            cwd="/Users/pero/pgit/peterkuhar.com",
            origin="Claude Code CLI",
        )
        snapshot = MonitorSnapshot(
            aggregate=AggregateStatus(AgentMode.COMPLETED, 0, 0, subagent),
            statuses=(main, subagent),
            stale_statuses=(),
            sources=(),
            collected_at=now,
        )

        statuses = status_bar.recent_statuses(snapshot)

        self.assertEqual([status.agent_id for status in statuses], [main.agent_id])

    def test_status_bar_recent_statuses_include_recent_done_while_active(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        now = datetime.now(timezone.utc)
        working = AgentStatus(
            provider="codex",
            agent_id="codex:session:working",
            display_name="project: Working session (working)",
            mode=AgentMode.TOOL_RUNNING,
            updated_at=now,
            event_name="PreToolUse",
            session_id="working",
            cwd="/Users/pero/pgit/project",
        )
        recent_done = AgentStatus(
            provider="grok",
            agent_id="grok:session:recent-done",
            display_name="other: Recent done (recent-d)",
            mode=AgentMode.COMPLETED,
            updated_at=now - timedelta(minutes=8),
            event_name="Stop",
            session_id="recent-done",
            cwd="/Users/pero/pgit/other",
            stale=True,
        )
        old_done = AgentStatus(
            provider="claude",
            agent_id="claude:session:old-done",
            display_name="old: Old done (old-done)",
            mode=AgentMode.COMPLETED,
            updated_at=now - timedelta(hours=49),
            event_name="Stop",
            session_id="old-done",
            cwd="/Users/pero/pgit/old",
            stale=True,
        )
        snapshot = MonitorSnapshot(
            aggregate=AggregateStatus(AgentMode.TOOL_RUNNING, 1, 2, working),
            statuses=(working,),
            stale_statuses=(recent_done, old_done),
            sources=(),
            collected_at=now,
        )

        statuses = status_bar.recent_statuses(snapshot)

        self.assertEqual(
            [status.session_id for status in statuses],
            ["working", "recent-done"],
        )

    def test_status_bar_recent_statuses_keeps_last_ten_within_retention(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        now = datetime.now(timezone.utc)
        stale_done = tuple(
            AgentStatus(
                provider="codex",
                agent_id=f"codex:session:done-{index}",
                display_name=f"project: Done {index} (done-{index})",
                mode=AgentMode.COMPLETED,
                updated_at=now - timedelta(hours=index),
                event_name="Stop",
                session_id=f"done-{index}",
                cwd="/Users/pero/pgit/project",
                stale=True,
            )
            for index in range(1, 13)
        )
        too_old = AgentStatus(
            provider="claude",
            agent_id="claude:session:too-old",
            display_name="old: Too old (too-old)",
            mode=AgentMode.COMPLETED,
            updated_at=now - timedelta(hours=50),
            event_name="Stop",
            session_id="too-old",
            cwd="/Users/pero/pgit/old",
            stale=True,
        )
        snapshot = MonitorSnapshot(
            aggregate=AggregateStatus(AgentMode.IDLE_READY, 0, 13, None),
            statuses=(),
            stale_statuses=(*stale_done, too_old),
            sources=(),
            collected_at=now,
        )

        statuses = status_bar.recent_statuses(
            snapshot,
            AgentMonitorSettings(recent_session_retention_seconds=48 * 60 * 60),
        )

        self.assertEqual(len(statuses), 10)
        self.assertEqual(statuses[0].session_id, "done-1")
        self.assertEqual(statuses[-1].session_id, "done-10")
        self.assertNotIn("too-old", [status.session_id for status in statuses])

        shorter = status_bar.recent_statuses(
            snapshot,
            AgentMonitorSettings(recent_session_retention_seconds=2.5 * 60 * 60),
        )
        self.assertEqual([status.session_id for status in shorter], ["done-1", "done-2"])

    def test_codex_session_options_are_codex_specific(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        now = datetime.now(timezone.utc)
        status = AgentStatus(
            provider="codex",
            agent_id="codex:session:abc",
            display_name="Codex abc",
            mode=AgentMode.WORKING,
            updated_at=now,
            event_name="PreToolUse",
            session_id="019ee395-2f64-7cc3-b566-afcc1d626160",
            cwd="/tmp/project with spaces",
        )
        target = SimpleNamespace(settings=AgentMonitorSettings())

        row = status_bar.build_session_menu_item(status, now, target)
        options = status_bar.build_session_options_menu(status, now, target)
        titles = [
            options.itemAtIndex_(index).title()
            for index in range(options.numberOfItems())
            if options.itemAtIndex_(index).title()
        ]

        self.assertEqual(row.title(), status_bar.native_session_menu_title(status))
        self.assertIsNotNone(row.image())
        self.assertIsNone(row.submenu())
        self.assertTrue(any(title.startswith("Working  Codex abc") for title in titles))
        self.assertIn("Open in Codex", titles)
        self.assertIn("Resume in Terminal", titles)
        self.assertNotIn("Open in VS Code", titles)
        self.assertNotIn("Open Claude App", titles)

    def test_grok_default_terminal_opener_uses_resume_terminal_setting(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        now = datetime.now(timezone.utc)
        status = AgentStatus(
            provider="grok",
            agent_id="grok:session:abc",
            display_name="Grok abc",
            mode=AgentMode.WORKING,
            updated_at=now,
            event_name="PreToolUse",
            session_id="grok-session",
            cwd="/Users/pero/pgit/sdstatus_bitbang",
        )
        settings = AgentMonitorSettings().with_session_terminal(TERMINAL_APP_GHOSTTY)
        target = SimpleNamespace(settings=settings)

        self.assertEqual(
            status_bar.provider_open_action_label("grok", SESSION_OPEN_TERMINAL, settings),
            "Resume in Terminal",
        )
        self.assertEqual(
            status_bar.session_open_action_title(status, SESSION_OPEN_TERMINAL, settings),
            "Resume in Ghostty",
        )
        options = status_bar.build_session_options_menu(status, now, target)
        titles = [
            options.itemAtIndex_(index).title()
            for index in range(options.numberOfItems())
            if options.itemAtIndex_(index).title()
        ]

        self.assertIn("Resume in Ghostty", titles)

        fake = SimpleNamespace(
            settings=settings,
            set_settings_message=lambda message: None,
        )
        with patch("sidepulse.status_bar.open_terminal_command") as open_terminal:
            status_bar.StatusBarController.open_session(
                fake,
                status,
                None,
                remember=False,
            )

        open_terminal.assert_called_once_with(
            "cd /Users/pero/pgit/sdstatus_bitbang && grok --resume grok-session",
            terminal_app=TERMINAL_APP_GHOSTTY,
            custom_terminal_path="",
            session_hints=status_bar.terminal_session_hints(status),
        )

    def test_status_bar_device_submenu_has_brightness_slider(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        device = status_bar.StatusBarDevice(
            device_id="/Volumes/SidePulseDot",
            name="SidePulse Dot",
            root=Path("/Volumes/SidePulseDot"),
            target=Path("/Volumes/SidePulseDot/LEDS.LED"),
            connected=True,
            display="agent",
            brightness=128,
        )

        item = status_bar.build_device_menu_item(device, SimpleNamespace())
        submenu = item.submenu()
        titles = [
            submenu.itemAtIndex_(index).title()
            for index in range(submenu.numberOfItems())
            if submenu.itemAtIndex_(index).title()
        ]
        custom_view_count = sum(
            1
            for index in range(submenu.numberOfItems())
            if submenu.itemAtIndex_(index).view() is not None
        )

        self.assertIn("Brightness 50%", titles)
        self.assertIn("Agent Status", titles)
        self.assertIn("Battery Level", titles)
        self.assertIn("Manual", titles)
        self.assertEqual(custom_view_count, 1)

        custom_device = status_bar.StatusBarDevice(
            device_id="/Volumes/SidePulseDot",
            name="SidePulse Dot",
            root=Path("/Volumes/SidePulseDot"),
            target=Path("/Volumes/SidePulseDot/LEDS.LED"),
            connected=True,
            display=LED_DISPLAY_CUSTOM,
            brightness=128,
        )
        custom_item = status_bar.build_device_menu_item(custom_device, SimpleNamespace())
        custom_submenu = custom_item.submenu()
        by_title = {
            custom_submenu.itemAtIndex_(index).title(): custom_submenu.itemAtIndex_(index)
            for index in range(custom_submenu.numberOfItems())
            if custom_submenu.itemAtIndex_(index).title()
        }

        self.assertEqual(by_title["Manual"].state(), 1)

    def test_status_bar_screen_bar_remove_lives_in_screen_bar_submenu(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        device = status_bar.StatusBarDevice(
            device_id=status_bar.VIRTUAL_DEVICE_ID,
            name="Screen Bar",
            root=Path(status_bar.VIRTUAL_DEVICE_ID),
            target=Path(status_bar.VIRTUAL_DEVICE_ID),
            connected=True,
            display="agent",
            brightness=255,
        )
        snapshot = SimpleNamespace(
            statuses=[],
            collected_at=datetime.now(timezone.utc),
        )
        target = SimpleNamespace(
            settings=AgentMonitorSettings(virtual_status_device_enabled=True),
            closed_lid_awake=SimpleNamespace(last_error=None),
            status_bar_devices=lambda: [device],
        )

        menu = status_bar.build_menu(snapshot, status_bar.STATE_IDLE, target)
        titles = [
            menu.itemAtIndex_(index).title()
            for index in range(menu.numberOfItems())
            if menu.itemAtIndex_(index).title()
        ]
        screen_bar_item = next(
            menu.itemAtIndex_(index)
            for index in range(menu.numberOfItems())
            if menu.itemAtIndex_(index).title() == "Screen Bar"
        )
        submenu = screen_bar_item.submenu()
        submenu_titles = [
            submenu.itemAtIndex_(index).title()
            for index in range(submenu.numberOfItems())
            if submenu.itemAtIndex_(index).title()
        ]
        submenu_view_count = sum(
            1
            for index in range(submenu.numberOfItems())
            if submenu.itemAtIndex_(index).view() is not None
        )

        self.assertNotIn("Remove Screen Bar", titles)
        self.assertIn("Remove Screen Bar", submenu_titles)
        self.assertNotIn("Brightness 100%", submenu_titles)
        self.assertEqual(submenu_view_count, 0)

        target.settings = AgentMonitorSettings(virtual_status_device_enabled=False)
        target.status_bar_devices = lambda: []
        menu = status_bar.build_menu(snapshot, status_bar.STATE_IDLE, target)
        titles = [
            menu.itemAtIndex_(index).title()
            for index in range(menu.numberOfItems())
            if menu.itemAtIndex_(index).title()
        ]

        self.assertIn("Add Screen Bar", titles)

    def test_status_bar_observe_connected_device_resets_on_new_mount(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            device = status_bar.StatusBarDevice(
                device_id="/Volumes/SidePulsePro",
                name="SidePulse Pro",
                root=root,
                target=root / "LEDS.LED",
                connected=True,
                display="agent",
                brightness=255,
            )
            reset_ids: list[str] = []
            devices: list[status_bar.StatusBarDevice] = []
            target = SimpleNamespace(
                last_connected_device_signature=None,
                status_bar_devices=lambda: devices,
                reset_led_controllers_for_device=lambda device_id: reset_ids.append(device_id),
            )

            self.assertFalse(status_bar.StatusBarController.observe_connected_devices(target))

            devices.append(device)
            self.assertTrue(status_bar.StatusBarController.observe_connected_devices(target))
            self.assertEqual(reset_ids, ["/Volumes/SidePulsePro"])

            self.assertFalse(status_bar.StatusBarController.observe_connected_devices(target))
            self.assertEqual(reset_ids, ["/Volumes/SidePulsePro"])

    def test_status_bar_poll_devices_refreshes_on_connection_change(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        calls: list[object] = []
        target = SimpleNamespace(
            observe_connected_devices=lambda: True,
            last_snapshot=object(),
            refresh_=lambda sender: calls.append(sender),
        )

        status_bar.StatusBarController.poll_devices_once(target)

        self.assertEqual(calls, [None])

    def test_status_bar_sync_skips_custom_device_display(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        device = status_bar.StatusBarDevice(
            device_id="/Volumes/SidePulseDot",
            name="SidePulse Dot",
            root=Path("/Volumes/SidePulseDot"),
            target=Path("/Volumes/SidePulseDot/LEDS.LED"),
            connected=True,
            display=LED_DISPLAY_CUSTOM,
            brightness=128,
        )
        fake = SimpleNamespace(
            status_bar_devices=lambda remember=True: [device],
            ensure_device_selection=lambda: None,
            last_led_error="old",
            device_errors={device.device_id: "old"},
            last_led_display_kind_by_device={},
            reset_led_controllers_for_device=lambda device_id: None,
            active_led_display_kind_for_device=lambda _device, _battery: LED_DISPLAY_CUSTOM,
            agent_controller_for_device=lambda _device: self.fail("agent LEDs should not sync"),
            battery_controller_for_device=lambda _device: self.fail("battery LEDs should not sync"),
        )

        status_bar.StatusBarController.sync_leds_now(
            fake,
            AgentMode.WORKING,
            None,
            LED_DISPLAY_CUSTOM,
        )

        self.assertIsNone(fake.last_led_error)
        self.assertNotIn(device.device_id, fake.device_errors)
        self.assertEqual(
            fake.last_led_display_kind_by_device[device.device_id],
            LED_DISPLAY_CUSTOM,
        )

    def test_status_bar_manual_device_display_clears_leds(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        device = status_bar.StatusBarDevice(
            device_id="/Volumes/SidePulseDot",
            name="SidePulse Dot",
            root=Path("/Volumes/SidePulseDot"),
            target=Path("/Volumes/SidePulseDot/LEDS.LED"),
            connected=True,
            display="agent",
            brightness=128,
        )
        messages: list[str] = []
        fake = SimpleNamespace(
            settings=AgentMonitorSettings(),
            status_bar_devices=lambda remember=False: [device],
            reset_led_controllers_for_device=lambda device_id: None,
            set_settings_message=messages.append,
            refresh_settings_window=lambda: None,
            refresh_=lambda sender: None,
            device_errors={},
            last_led_error=None,
        )
        fake.clear_manual_device_display = lambda item: (
            status_bar.StatusBarController.clear_manual_device_display(fake, item)
        )

        with (
            patch("sidepulse.status_bar.save_settings"),
            patch(
                "sidepulse.status_bar.write_led_program",
                return_value=device.target,
            ) as write,
        ):
            status_bar.StatusBarController.set_device_display(
                fake,
                device.device_id,
                LED_DISPLAY_CUSTOM,
            )

        write.assert_called_once_with("off", device_path=device.target)
        self.assertEqual(fake.settings.display_for_device(device.device_id), LED_DISPLAY_CUSTOM)
        self.assertEqual(messages[-1], "SidePulse Dot: Manual, LEDs cleared.")

    def test_status_bar_menu_has_sleep_prevention_title_and_policy_choices(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        snapshot = SimpleNamespace(
            statuses=[],
            collected_at=datetime.now(timezone.utc),
        )
        target = SimpleNamespace(
            settings=AgentMonitorSettings(
                sleep_prevention_policy=SLEEP_PREVENTION_AGENTS,
            ),
            keep_awake=SimpleNamespace(process_running=lambda: True),
            closed_lid_awake=SimpleNamespace(
                last_error=None,
                process_running=lambda: False,
            ),
            last_mac_sleep_snapshot=MacSleepSnapshot(
                sleep_disabled=False,
                prevent_user_idle_system_sleep=True,
            ),
            last_lid_closed=False,
            agent_awake_requested=True,
            battery_sleep_safeguard_active=False,
            battery_sleep_safeguard_reason="battery 87%, threshold 20%",
            status_bar_devices=lambda: [],
        )

        menu = status_bar.build_menu(snapshot, status_bar.STATE_IDLE, target)
        items = [menu.itemAtIndex_(index) for index in range(menu.numberOfItems())]
        titled_items = [item for item in items if item.title()]
        titles = [item.title() for item in titled_items]

        self.assertLess(titles.index("Agents"), titles.index("Devices"))
        self.assertIn("Closed-Lid Sleep Prevention", titles)
        self.assertNotIn(
            "Status: caffeinate active, closed-lid support off, disablesleep off, macOS prevented",
            titles,
        )
        self.assertNotIn("Lid: open; agent keep-awake window active", titles)
        self.assertNotIn("Battery safeguard: standby; battery 87%, threshold 20%", titles)
        policy_index = titles.index("Closed-Lid Sleep Prevention")
        policy_items = titled_items[policy_index + 1 : policy_index + 4]

        self.assertEqual(
            [item.title() for item in policy_items],
            ["Never", "When Agents Work", "Always"],
        )
        self.assertEqual([item.state() for item in policy_items], [0, 1, 0])
        self.assertNotIn("Keep Awake With Lid Open", titles)
        self.assertNotIn("Keep Awake With Lid Closed", titles)
        self.assertNotIn("Strong Sleep Override...", titles)
        self.assertNotIn("Sleep Helper Missing", titles)
        self.assertIn("Setup...", titles)

    def test_lid_animation_program_uses_device_brightness(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        animation = default_lid_animation(LID_ANIMATION_CLOSED)
        program = status_bar.program_for_lid_animation(animation, brightness=128)

        validate_led_text(program)
        self.assertTrue(program.startswith("brightness 128\n"))

    def test_lid_animation_restore_forces_led_resync(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        calls: list[tuple[str, object]] = []
        fake = SimpleNamespace(
            led_animation_token=42,
            led_animation_until_monotonic=100.0,
            last_snapshot=SimpleNamespace(
                aggregate=SimpleNamespace(mode=AgentMode.WORKING),
            ),
            last_battery_snapshot=object(),
            reset_led_controllers_for_display_change=lambda: calls.append(("reset", None)),
            active_led_display_kind=lambda snapshot: "agent",
            sync_leds=lambda mode, snapshot, display: calls.append(
                ("sync", (mode, snapshot, display))
            ),
            refresh_=lambda sender: calls.append(("refresh", sender)),
        )

        status_bar.restore_led_display(fake, "41")
        self.assertEqual(calls, [])
        self.assertEqual(fake.led_animation_until_monotonic, 100.0)

        status_bar.restore_led_display(fake, "42")
        self.assertEqual(fake.led_animation_until_monotonic, 0.0)
        self.assertEqual(calls[0], ("reset", None))
        self.assertEqual(calls[1][0], "sync")
        self.assertEqual(calls[1][1][0], AgentMode.WORKING)

    def test_status_bar_settings_window_has_lid_animation_controls(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        target = SimpleNamespace(settings_fields={}, settings_buttons={})

        window = status_bar.build_settings_window(target)

        self.assertEqual(window.title(), "SidePulse Settings")
        tab_views = [
            view
            for view in window.contentView().subviews()
            if hasattr(view, "numberOfTabViewItems")
        ]
        self.assertEqual(len(tab_views), 1)
        self.assertEqual(tab_views[0].numberOfTabViewItems(), 6)
        animations_tab = next(
            tab_views[0].tabViewItemAtIndex_(index)
            for index in range(tab_views[0].numberOfTabViewItems())
            if str(tab_views[0].tabViewItemAtIndex_(index).identifier()) == "animations"
        )
        agent_animations = animations_tab.view()
        self.assertFalse(
            any(
                hasattr(view, "numberOfTabViewItems")
                for view in agent_animations.subviews()
            )
        )
        previews = [
            view
            for view in agent_animations.subviews()
            if isinstance(view, status_bar.VirtualLedView)
        ]
        self.assertEqual(len(previews), len(status_bar.ANIMATION_UI_STATES))
        show_buttons = [
            view
            for view in agent_animations.subviews()
            if hasattr(view, "title") and str(view.title()) == "Show"
        ]
        self.assertEqual(len(show_buttons), len(status_bar.ANIMATION_UI_STATES))
        self.assertEqual(
            {str(button.representedObject()) for button in show_buttons},
            set(status_bar.ANIMATION_UI_STATES),
        )
        edit_buttons = [
            view
            for view in agent_animations.subviews()
            if hasattr(view, "title") and str(view.title()) == "Edit"
        ]
        self.assertEqual(len(edit_buttons), len(status_bar.ANIMATION_UI_STATES))
        self.assertEqual(
            {str(button.representedObject()) for button in edit_buttons},
            set(status_bar.ANIMATION_UI_STATES),
        )
        labels = {
            str(view.stringValue())
            for view in agent_animations.subviews()
            if hasattr(view, "stringValue")
        }
        self.assertIn("Lid Open", labels)
        self.assertIn("Lid Closed", labels)
        self.assertIn("Working / Tool / Long Task", labels)
        self.assertNotIn("Tool Running", labels)
        self.assertNotIn("Long Task Progress", labels)
        self.assertNotIn("agent_animation_tool_running", target.settings_fields)
        self.assertNotIn("agent_animation_long_task_progress", target.settings_fields)
        self.assertNotIn(
            "lid",
            {
                str(tab_views[0].tabViewItemAtIndex_(index).identifier())
                for index in range(tab_views[0].numberOfTabViewItems())
            },
        )
        self.assertIn("debug_log_status", target.settings_fields)
        self.assertIn("status_history_status", target.settings_fields)
        self.assertIn("status_history_chart", target.settings_fields)
        self.assertIn("session_terminal", target.settings_fields)
        self.assertIn("custom_terminal_path", target.settings_fields)
        self.assertIn("recent_session_retention_hours", target.settings_fields)
        self.assertIn("idle_timeout_minutes", target.settings_fields)
        self.assertIn("sleep_prevention_min_battery_percent", target.settings_fields)
        self.assertIn("status_history_timeframe", target.settings_fields)
        self.assertIn("agent_animation_lid_closed", target.settings_fields)
        self.assertIn("agent_animation_lid_open", target.settings_fields)
        for mode in status_bar.ANIMATION_UI_STATES:
            self.assertIn(f"agent_animation_{mode}", target.settings_fields)
            self.assertIn(
                f"agent_animation_preview_{mode}", target.settings_fields
            )
        self.assertNotIn("closed_lid_system_override", target.settings_buttons)

    def test_status_bar_validates_custom_agent_animation_program(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        status_bar.validate_agent_animation_program("off\n#FF0000 pulse\nrepeat")
        with self.assertRaises(DeviceWriteError):
            status_bar.validate_agent_animation_program("x" * 513)

    def test_animation_preview_timer_runs_while_menu_tracks_input(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        timer = object()
        modes = []
        run_loop = SimpleNamespace(
            addTimer_forMode_=lambda added_timer, mode: modes.append(
                (added_timer, mode)
            )
        )
        fake = SimpleNamespace(agent_animation_preview_timer=None)
        with (
            patch(
                "sidepulse.status_bar.NSTimer",
                SimpleNamespace(
                    timerWithTimeInterval_target_selector_userInfo_repeats_=(
                        lambda *args: timer
                    )
                ),
            ),
            patch(
                "sidepulse.status_bar.NSRunLoop",
                SimpleNamespace(mainRunLoop=lambda: run_loop),
            ),
        ):
            status_bar.StatusBarController.start_agent_animation_preview_timer(fake)

        self.assertEqual(fake.agent_animation_preview_timer, timer)
        self.assertEqual(
            modes,
            [
                (timer, status_bar.NSRunLoopCommonModes),
                (timer, status_bar.NSEventTrackingRunLoopMode),
            ],
        )

    def test_status_bar_show_agent_animation_on_device_writes_and_restores(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "SidePulsePro"
            root.mkdir()
            device = status_bar.StatusBarDevice(
                device_id=str(root),
                name="SidePulse Pro",
                root=root,
                target=root / "LEDS.LED",
                connected=True,
                display="agent",
            )
            restore_calls = []
            fake = SimpleNamespace(
                performSelectorOnMainThread_withObject_waitUntilDone_=(
                    lambda selector, value, wait: restore_calls.append(
                        (selector, value, wait)
                    )
                )
            )

            with patch("sidepulse.status_bar.time.sleep"):
                status_bar.StatusBarController.show_agent_animation_on_device_worker(
                    fake,
                    AgentMode.WORKING,
                    AGENT_ANIMATION_KITT,
                    "",
                    [device],
                    17,
                )

            program = (root / "LEDS.LED").read_text()
            self.assertIn("7:#00E5FF 320ms pulse 595ms", program)
            self.assertEqual(
                restore_calls,
                [("restoreLedDisplay:", "17", False)],
            )

    def test_status_history_status_text_is_compact(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        text = status_bar.status_history_status_text(
            [
                {
                    "recorded_at": "2026-07-20T12:30:00Z",
                    "battery_level": 76,
                    "charger_power_watts": 86,
                }
            ]
        )

        self.assertIn("History: Last 12h", text)
        self.assertIn("1 samples", text)
        self.assertIn("battery 76%", text)
        self.assertIn("charger 86W", text)
        self.assertNotIn("/Users/", text)

    def test_status_history_filters_to_selected_timeframe(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        records = [
            {"recorded_at": "2026-07-20T00:00:00Z", "display_status": "Idle"},
            {"recorded_at": "2026-07-20T11:30:00Z", "display_status": "Working"},
            {"recorded_at": "2026-07-20T12:00:00Z", "display_status": "Done"},
        ]

        filtered = status_bar.filter_status_history_records(records, 60 * 60)

        self.assertEqual(filtered, records[1:])
        self.assertEqual(status_bar.history_timeframe_label(60 * 60), "Last 1h")
        self.assertEqual(
            status_bar.history_timeframe_label(DEFAULT_HISTORY_TIMEFRAME_SECONDS),
            "Last 12h",
        )
        self.assertGreaterEqual(
            status_bar.history_record_limit_for_timeframe(HISTORY_TIMEFRAME_48H_SECONDS),
            2400,
        )

    def test_status_history_line_segments_scale_metric_values(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        points = [
            (0.0, {"battery_level": 0}),
            (30.0, {"battery_level": 50}),
            (60.0, {"battery_level": 100}),
        ]

        segments = status_bar.history_line_segments(
            points,
            "battery_level",
            100.0,
            0.0,
            60.0,
            10.0,
            90.0,
            20.0,
            40.0,
        )

        self.assertEqual(
            segments,
            [[(10.0, 20.0), (55.0, 40.0), (100.0, 60.0)]],
        )

    def test_status_history_legend_names_major_colors(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        text = " ".join(
            str(item)
            for row in status_bar.history_legend_rows()
            for item in row
        )

        self.assertIn("Ask", text)
        self.assertIn("Working", text)
        self.assertIn("Battery", text)
        self.assertIn("Charger", text)
        self.assertIn("Lid closed", text)

    def test_status_history_line_segments_split_missing_values(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        points = [
            (0.0, {"charger_power_watts": 0}),
            (30.0, {"charger_power_watts": None}),
            (60.0, {"charger_power_watts": 30}),
        ]

        segments = status_bar.history_line_segments(
            points,
            "charger_power_watts",
            30.0,
            0.0,
            60.0,
            10.0,
            90.0,
            20.0,
            40.0,
        )

        self.assertEqual(segments, [[(10.0, 20.0)], [(100.0, 60.0)]])
        self.assertEqual(
            status_bar.latest_numeric_history_value(points, "charger_power_watts"),
            30,
        )
        self.assertEqual(status_bar.nice_history_max([0, 12, 30]), 30.0)
        self.assertEqual(status_bar.nice_history_max([31]), 45.0)
        self.assertEqual(status_bar.nice_history_max([96]), 100.0)

    def test_status_bar_monitor_uses_idle_timeout_setting(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        with tempfile.TemporaryDirectory() as tmp:
            fake = SimpleNamespace(
                settings=AgentMonitorSettings(idle_timeout_seconds=1234)
            )
            with (
                patch(
                    "sidepulse.status_bar.default_event_socket_path",
                    return_value=Path(tmp) / "events.sock",
                ),
                patch(
                    "sidepulse.status_bar.default_latest_state_path",
                    return_value=Path(tmp) / "latest.json",
                ),
            ):
                monitor = status_bar.StatusBarController.build_monitor(fake)

        self.assertEqual(monitor.stale_after_seconds, 1234)

    def test_status_bar_setup_window_has_first_launch_controls(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        target = SimpleNamespace(setup_fields={}, setup_buttons={})

        window = status_bar.build_setup_window(target)

        self.assertEqual(window.title(), "SidePulse Setup")
        self.assertIn("launch", target.setup_buttons)
        self.assertIn("eject_guard", target.setup_buttons)
        self.assertIn("eject_guard_uninstall", target.setup_buttons)
        self.assertIn("sleep_helper", target.setup_buttons)
        self.assertIn("launch_status", target.setup_fields)
        self.assertIn("eject_status", target.setup_fields)
        self.assertIn("sleep_status", target.setup_fields)

    def test_first_launch_setup_window_only_shows_until_completed(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        self.assertTrue(status_bar.should_show_setup_window(AgentMonitorSettings()))
        self.assertFalse(
            status_bar.should_show_setup_window(
                AgentMonitorSettings(setup_screen_completed=True)
            )
        )

    def test_setup_terminal_installer_opens_command_file(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            with (
                patch("sidepulse.status_bar.default_state_dir", return_value=state_dir),
                patch("sidepulse.status_bar.subprocess.Popen") as popen,
            ):
                script = status_bar.open_terminal_setup_command("echo hello")

            self.assertEqual(script, state_dir / "install-sleep-helper.command")
            self.assertIn("echo hello", script.read_text())
            self.assertEqual(script.stat().st_mode & 0o777, 0o700)
            popen.assert_called_once()
            self.assertEqual(popen.call_args.args[0][0], "/usr/bin/open")

    def test_status_bar_open_terminal_command_uses_selected_terminal(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        with patch("sidepulse.status_bar.subprocess.Popen") as popen:
            status_bar.open_terminal_command("echo hello")
        args = popen.call_args.args[0]
        self.assertEqual(args[:2], ["/usr/bin/osascript", "-e"])
        self.assertIn('tell application "Terminal"', args[2])

        with (
            patch("sidepulse.status_bar.terminal_app_installed", return_value=True),
            patch("sidepulse.status_bar.subprocess.Popen") as popen,
        ):
            status_bar.open_terminal_command(
                "echo hello",
                terminal_app=TERMINAL_APP_ITERM,
            )
        args = popen.call_args.args[0]
        self.assertEqual(args[:2], ["/usr/bin/osascript", "-e"])
        self.assertIn('tell application "iTerm"', args[2])

        with (
            patch(
                "sidepulse.status_bar.installed_terminal_app_path",
                return_value=Path("/Applications/Ghostty.app"),
            ),
            patch("sidepulse.status_bar.open_ghostty_command") as open_ghostty,
        ):
            status_bar.open_terminal_command(
                "echo hello",
                terminal_app=TERMINAL_APP_GHOSTTY,
            )
        open_ghostty.assert_called_once_with("echo hello", None)

        with (
            patch("sidepulse.status_bar.terminal_app_installed", return_value=False),
            patch("sidepulse.status_bar.subprocess.Popen") as popen,
        ):
            status_bar.open_terminal_command(
                "echo hello",
                terminal_app=TERMINAL_APP_GHOSTTY,
            )
        args = popen.call_args.args[0]
        self.assertEqual(args[:2], ["/usr/bin/osascript", "-e"])
        self.assertIn('tell application "Terminal"', args[2])

        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            with (
                patch("sidepulse.status_bar.default_state_dir", return_value=state_dir),
                patch(
                    "sidepulse.status_bar.installed_terminal_app_path",
                    return_value=Path("/Applications/Warp.app"),
                ),
                patch("sidepulse.status_bar.subprocess.Popen") as popen,
            ):
                status_bar.open_terminal_command(
                    "echo hello",
                    terminal_app=TERMINAL_APP_WARP,
                )
            script = state_dir / "resume-session.command"
            self.assertIn("echo hello", script.read_text())
            self.assertEqual(
                popen.call_args.args[0],
                ["/usr/bin/open", "-a", "/Applications/Warp.app", str(script)],
            )

        with (
            patch(
                "sidepulse.status_bar.installed_terminal_app_path",
                return_value=Path("/Applications/Kitty.app"),
            ),
            patch("sidepulse.status_bar.subprocess.Popen") as popen,
        ):
            status_bar.open_terminal_command(
                "echo hello",
                terminal_app=TERMINAL_APP_KITTY,
            )
        self.assertEqual(
            popen.call_args.args[0],
            [
                "/usr/bin/open",
                "-n",
                "/Applications/Kitty.app",
                "--args",
                "-e",
                "/bin/zsh",
                "-lc",
                "echo hello",
            ],
        )

        with (
            patch(
                "sidepulse.status_bar.installed_terminal_app_path",
                return_value=Path("/Applications/WezTerm.app"),
            ),
            patch("sidepulse.status_bar.subprocess.Popen") as popen,
        ):
            status_bar.open_terminal_command(
                "echo hello",
                terminal_app=TERMINAL_APP_WEZTERM,
            )
        self.assertEqual(
            popen.call_args.args[0],
            [
                "/usr/bin/open",
                "-n",
                "/Applications/WezTerm.app",
                "--args",
                "start",
                "--new-tab",
                "--",
                "/bin/zsh",
                "-lc",
                "echo hello",
            ],
        )

        with (
            patch(
                "sidepulse.status_bar.installed_terminal_app_path",
                return_value=Path("/Applications/Alacritty.app"),
            ),
            patch("sidepulse.status_bar.subprocess.Popen") as popen,
        ):
            status_bar.open_terminal_command(
                "echo hello",
                terminal_app=TERMINAL_APP_ALACRITTY,
            )
        self.assertEqual(
            popen.call_args.args[0],
            [
                "/usr/bin/open",
                "-n",
                "/Applications/Alacritty.app",
                "--args",
                "-e",
                "/bin/zsh",
                "-lc",
                "echo hello",
            ],
        )

        with tempfile.TemporaryDirectory() as tmp:
            custom_app = Path(tmp) / "WezTerm.app"
            custom_app.mkdir()
            with patch("sidepulse.status_bar.subprocess.Popen") as popen:
                status_bar.open_terminal_command(
                    "echo hello",
                    terminal_app=TERMINAL_APP_CUSTOM,
                    custom_terminal_path=str(custom_app),
                )
            call_args = popen.call_args.args[0]
        self.assertEqual(
            call_args,
            [
                "/usr/bin/open",
                "-n",
                str(custom_app),
                "--args",
                "start",
                "--new-tab",
                "--",
                "/bin/zsh",
                "-lc",
                "echo hello",
            ],
        )

        with patch("sidepulse.status_bar.subprocess.Popen") as popen:
            status_bar.open_terminal_command(
                "echo hello",
                terminal_app=TERMINAL_APP_CUSTOM,
                custom_terminal_path="/Applications/Missing.app",
            )
        args = popen.call_args.args[0]
        self.assertEqual(args[:2], ["/usr/bin/osascript", "-e"])
        self.assertIn('tell application "Terminal"', args[2])

    def test_status_bar_resume_terminal_focuses_existing_terminal_session(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        hints = status_bar.TerminalSessionHints(
            provider="codex",
            session_id="019ee395-2f64-7cc3-b566-afcc1d626160",
            cwd="/Users/pero/pgit/pixiepulse-bridge",
            title="Codex Refine README agent status modes pixiepulse-bridge",
        )

        with (
            patch(
                "sidepulse.status_bar.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    ["/usr/bin/osascript"],
                    0,
                    stdout="1\n",
                ),
            ) as run,
            patch("sidepulse.status_bar.subprocess.Popen") as popen,
        ):
            status_bar.open_terminal_command(
                "cd /Users/pero/pgit/pixiepulse-bridge && codex resume 019ee395-2f64-7cc3-b566-afcc1d626160",
                session_hints=hints,
            )

        script = run.call_args.args[0][2]
        self.assertIn('tell application "Terminal"', script)
        self.assertIn("019ee395-2f64-7cc3-b566-afcc1d626160", script)
        self.assertIn("selected tab of windowRef", script)
        popen.assert_not_called()

    def test_status_bar_resume_terminal_falls_back_with_session_title(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        hints = status_bar.TerminalSessionHints(
            provider="grok",
            session_id="grok-session",
            cwd="/Users/pero/pgit/sdstatus_bitbang",
            title="Grok all good sdstatus_bitbang",
        )

        with (
            patch(
                "sidepulse.status_bar.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    ["/usr/bin/osascript"],
                    0,
                    stdout="0\n",
                ),
            ) as run,
            patch("sidepulse.status_bar.subprocess.Popen") as popen,
        ):
            status_bar.open_terminal_command(
                "cd /Users/pero/pgit/sdstatus_bitbang && grok --resume grok-session",
                session_hints=hints,
            )

        script = run.call_args.args[0][2]
        self.assertIn("grok-session", script)
        launch_script = popen.call_args.args[0][2]
        self.assertIn("SidePulse Grok all good sdstatus_bitbang (grok-ses)", launch_script)
        self.assertIn("grok --resume grok-session", launch_script)

    def test_status_bar_resume_iterm_focuses_existing_session(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        hints = status_bar.TerminalSessionHints(
            provider="claude",
            session_id="claude-session",
            cwd="/Users/pero/pgit/app",
            title="Claude app",
        )

        with (
            patch(
                "sidepulse.status_bar.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    ["/usr/bin/osascript"],
                    0,
                    stdout="1\n",
                ),
            ) as run,
            patch("sidepulse.status_bar.subprocess.Popen") as popen,
        ):
            status_bar.open_terminal_command(
                "cd /Users/pero/pgit/app && claude --resume claude-session",
                terminal_app=TERMINAL_APP_ITERM,
                session_hints=hints,
            )

        script = run.call_args.args[0][2]
        self.assertIn('tell application "iTerm"', script)
        self.assertIn("select tabRef", script)
        popen.assert_not_called()

    def test_status_bar_resume_ghostty_focuses_existing_surface(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        class FakeTerminal:
            def __init__(self) -> None:
                self.focused = False

            def name(self):
                return "SidePulse Grok all good sdstatus_bitbang (grok-ses)"

            def workingDirectory(self):
                return "/Users/pero/pgit/sdstatus_bitbang"

            def focus(self):
                self.focused = True

        class FakeTab:
            def __init__(self, terminal) -> None:
                self.terminal = terminal
                self.selected = False

            def name(self):
                return "grok"

            def terminals(self):
                return [self.terminal]

            def selectTab(self):
                self.selected = True

        class FakeWindow:
            def __init__(self, tab) -> None:
                self.tab = tab
                self.activated = False

            def name(self):
                return "Ghostty"

            def tabs(self):
                return [self.tab]

            def activateWindow(self):
                self.activated = True

        class FakeGhostty:
            def __init__(self, window) -> None:
                self.window = window
                self.activated = False

            def isRunning(self):
                return True

            def windows(self):
                return [self.window]

            def activate(self):
                self.activated = True

        terminal = FakeTerminal()
        tab = FakeTab(terminal)
        window = FakeWindow(tab)
        app = FakeGhostty(window)
        hints = status_bar.TerminalSessionHints(
            provider="grok",
            session_id="grok-session",
            cwd="/Users/pero/pgit/sdstatus_bitbang",
            title="Grok all good sdstatus_bitbang",
        )

        with patch("sidepulse.status_bar.ghostty_application", return_value=app):
            self.assertTrue(status_bar.focus_ghostty_session(hints))

        self.assertTrue(tab.selected)
        self.assertTrue(terminal.focused)
        self.assertTrue(window.activated)
        self.assertTrue(app.activated)

    def test_status_bar_resume_ghostty_focuses_unique_bare_grok_title(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        class FakeTerminal:
            def __init__(self, name: str) -> None:
                self._name = name
                self.focused = False

            def name(self):
                return self._name

            def workingDirectory(self):
                return "/Users/pero/temp/msdosfs"

            def focus(self):
                self.focused = True

        class FakeTab:
            def __init__(self, terminal) -> None:
                self.terminal = terminal
                self.selected = False

            def name(self):
                return self.terminal.name()

            def terminals(self):
                return [self.terminal]

            def selectTab(self):
                self.selected = True

        class FakeWindow:
            def __init__(self, tab) -> None:
                self.tab = tab
                self.activated = False

            def name(self):
                return "Ghostty"

            def tabs(self):
                return [self.tab]

            def activateWindow(self):
                self.activated = True

        class FakeGhostty:
            def __init__(self, windows) -> None:
                self._windows = windows
                self.activated = False

            def isRunning(self):
                return True

            def windows(self):
                return self._windows

            def activate(self):
                self.activated = True

        wrong_terminal = FakeTerminal("plain shell")
        right_terminal = FakeTerminal("✳ Identify unknown code or concept")
        wrong_window = FakeWindow(FakeTab(wrong_terminal))
        right_window = FakeWindow(FakeTab(right_terminal))
        app = FakeGhostty([wrong_window, right_window])
        hints = status_bar.TerminalSessionHints(
            provider="grok",
            session_id="grok-session",
            cwd="/Users/pero/temp/msdosfs",
            title="Grok Identify unknown code or concept msdosfs",
            match_title="Identify unknown code or concept",
        )

        with patch("sidepulse.status_bar.ghostty_application", return_value=app):
            self.assertTrue(status_bar.focus_ghostty_session(hints))

        self.assertFalse(wrong_terminal.focused)
        self.assertFalse(wrong_window.activated)
        self.assertTrue(right_terminal.focused)
        self.assertTrue(right_window.activated)

    def test_status_bar_resume_ghostty_ignores_same_cwd_without_session_marker(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        class FakeTerminal:
            def __init__(self, name: str) -> None:
                self._name = name
                self.focused = False

            def name(self):
                return self._name

            def workingDirectory(self):
                return "/Users/pero/pgit/sdstatus_bitbang"

            def focus(self):
                self.focused = True

        class FakeTab:
            def __init__(self, terminal) -> None:
                self.terminal = terminal
                self.selected = False

            def name(self):
                return self.terminal.name()

            def terminals(self):
                return [self.terminal]

            def selectTab(self):
                self.selected = True

        class FakeWindow:
            def __init__(self, tab) -> None:
                self.tab = tab
                self.activated = False

            def name(self):
                return "Ghostty"

            def tabs(self):
                return [self.tab]

            def activateWindow(self):
                self.activated = True

        class FakeGhostty:
            def __init__(self, windows) -> None:
                self._windows = windows
                self.activated = False

            def isRunning(self):
                return True

            def windows(self):
                return self._windows

            def activate(self):
                self.activated = True

        wrong_terminal = FakeTerminal("plain shell")
        right_terminal = FakeTerminal("SidePulse Grok all good sdstatus_bitbang (grok-ses)")
        wrong_window = FakeWindow(FakeTab(wrong_terminal))
        right_window = FakeWindow(FakeTab(right_terminal))
        app = FakeGhostty([wrong_window, right_window])
        hints = status_bar.TerminalSessionHints(
            provider="grok",
            session_id="grok-session",
            cwd="/Users/pero/pgit/sdstatus_bitbang",
            title="Grok all good sdstatus_bitbang",
        )

        with patch("sidepulse.status_bar.ghostty_application", return_value=app):
            self.assertTrue(status_bar.focus_ghostty_session(hints))

        self.assertFalse(wrong_terminal.focused)
        self.assertFalse(wrong_window.activated)
        self.assertTrue(right_terminal.focused)
        self.assertTrue(right_window.activated)

    def test_status_bar_resume_ghostty_falls_back_to_new_window(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        class FakeWindow:
            pass

        class FakeGhostty:
            def __init__(self) -> None:
                self.window = FakeWindow()
                self.config = None
                self.new_tab_window = None
                self.new_window_config = None
                self.activated = False

            def isRunning(self):
                return True

            def frontWindow(self):
                return self.window

            def newSurfaceConfigurationFrom_(self, config):
                self.config = config
                return config

            def newTabIn_withConfiguration_(self, window, config):
                self.new_tab_window = window
                self.config = config

            def newWindowWithConfiguration_(self, config):
                self.new_window_config = config
                self.config = config

            def activate(self):
                self.activated = True

        app = FakeGhostty()
        hints = status_bar.TerminalSessionHints(
            provider="grok",
            session_id="grok-session",
            cwd="/Users/pero/pgit/sdstatus_bitbang",
            title="Grok all good sdstatus_bitbang",
        )

        with patch("sidepulse.status_bar.ghostty_application", return_value=app):
            self.assertTrue(
                status_bar.open_ghostty_command_with_scripting_bridge(
                    "cd /Users/pero/pgit/sdstatus_bitbang && grok --resume grok-session",
                    hints,
                )
            )

        self.assertIsNone(app.new_tab_window)
        self.assertIs(app.new_window_config, app.config)
        self.assertTrue(app.activated)
        self.assertEqual(app.config["workingDirectory"], "/Users/pero/pgit/sdstatus_bitbang")
        self.assertIn("grok --resume grok-session", app.config["command"])

    def test_status_bar_resume_ghostty_uses_focus_before_new_tab(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        hints = status_bar.TerminalSessionHints(
            provider="grok",
            session_id="grok-session",
            cwd="/Users/pero/pgit/sdstatus_bitbang",
            title="Grok all good sdstatus_bitbang",
        )

        with (
            patch("sidepulse.status_bar.focus_ghostty_session", return_value=True),
            patch("sidepulse.status_bar.open_ghostty_command") as open_ghostty,
        ):
            status_bar.open_terminal_command(
                "cd /Users/pero/pgit/sdstatus_bitbang && grok --resume grok-session",
                terminal_app=TERMINAL_APP_GHOSTTY,
                session_hints=hints,
            )

        open_ghostty.assert_not_called()

    def test_status_bar_detects_installed_terminal_apps(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        with tempfile.TemporaryDirectory() as tmp:
            app_dir = Path(tmp)
            (app_dir / "Warp.app").mkdir()
            (app_dir / "iTerm.app").mkdir()

            self.assertEqual(
                status_bar.installed_terminal_app_path(
                    TERMINAL_APP_WARP,
                    app_dirs=(app_dir,),
                ),
                app_dir / "Warp.app",
            )
            self.assertTrue(
                status_bar.terminal_app_installed(
                    TERMINAL_APP_ITERM,
                    app_dirs=(app_dir,),
                )
            )
            self.assertIsNone(
                status_bar.installed_terminal_app_path(
                    TERMINAL_APP_KITTY,
                    app_dirs=(app_dir,),
                )
            )

    def test_status_bar_open_session_remembers_action_by_origin(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        status = AgentStatus(
            provider="claude",
            agent_id="claude:session:abc",
            display_name="Claude abc",
            mode=AgentMode.WAITING_FOR_INPUT,
            updated_at=datetime.now(timezone.utc),
            event_name="Notification",
            session_id="1ca4348e-2aec-4147-9e81-d7d56364d257",
            cwd="/Users/pero/pgit/sdstatus_bitbang",
            origin="Claude in VS Code",
        )
        fake = SimpleNamespace(
            settings=AgentMonitorSettings().with_session_terminal(TERMINAL_APP_ITERM),
            messages=[],
            set_settings_message=lambda message: None,
        )

        with (
            patch("sidepulse.status_bar.open_terminal_command") as open_terminal,
            patch("sidepulse.status_bar.save_settings") as save,
        ):
            status_bar.StatusBarController.open_session(
                fake,
                status,
                SESSION_OPEN_TERMINAL,
                remember=True,
            )

        open_terminal.assert_called_once_with(
            "cd /Users/pero/pgit/sdstatus_bitbang && claude --resume 1ca4348e-2aec-4147-9e81-d7d56364d257",
            terminal_app=TERMINAL_APP_ITERM,
            custom_terminal_path="",
            session_hints=status_bar.terminal_session_hints(status),
        )
        self.assertEqual(
            fake.settings.session_open_action("claude", "Claude in VS Code"),
            SESSION_OPEN_TERMINAL,
        )
        self.assertIsNone(fake.settings.session_open_action("claude"))
        save.assert_called_once_with(fake.settings)

    def test_status_bar_primary_session_click_uses_saved_origin_preference(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        status = AgentStatus(
            provider="claude",
            agent_id="claude:session:abc",
            display_name="Claude abc",
            mode=AgentMode.WAITING_FOR_INPUT,
            updated_at=datetime.now(timezone.utc),
            event_name="Notification",
            session_id="1ca4348e-2aec-4147-9e81-d7d56364d257",
            cwd="/Users/pero/pgit/sdstatus_bitbang",
            origin="Claude in VS Code",
        )
        controller = status_bar.StatusBarController.alloc().init()
        sender = SimpleNamespace(representedObject=lambda: status)

        with (
            patch.object(status_bar.StatusBarController, "open_session", autospec=True) as open_session,
            patch.object(status_bar.StatusBarController, "close_status_menu", autospec=True) as close_menu,
        ):
            controller.openSessionPrimary_(sender)

        open_session.assert_called_once_with(controller, status, None, remember=False)
        close_menu.assert_called_once_with(controller)

    def test_codex_installer_replaces_monitor_hook_and_preserves_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = base / "config.toml"
            log = base / "codex.jsonl"
            config.write_text(
                "\n".join(
                    [
                        '[features]',
                        'js_repl = false',
                        '',
                        '[[hooks.PreToolUse]]',
                        '[[hooks.PreToolUse.hooks]]',
                        'type = "command"',
                        f"command = '''echo old >> {log}'''",
                        '',
                        '[hooks.state]',
                        'source = "keep-me"',
                        '',
                    ]
                )
            )

            result = install_codex_hooks(
                log_path=log,
                config_path=config,
                python_executable="python3",
            )

            self.assertTrue(result.changed)
            text = config.read_text()
            self.assertIn("hooks = true", text)
            self.assertIn('[hooks.state]', text)
            self.assertIn('source = "keep-me"', text)
            self.assertIn("hook_entry.py", text)
            self.assertIn("--provider codex", text)
            self.assertIn(str(log), text)
            self.assertNotIn("echo old", text)

    def test_codex_installer_refreshes_managed_hook_trust_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = base / "config.toml"
            log = base / "codex.jsonl"
            key = f"{config}:pre_tool_use:0:0"
            config.write_text("[features]\nhooks = true\n")

            with patch("sidepulse.install.should_refresh_codex_hook_trust", return_value=True):
                with patch(
                    "sidepulse.install.resolve_codex_hook_hashes",
                    return_value={key: "sha256:new-current-hash"},
                ):
                    result = install_codex_hooks(
                        log_path=log,
                        config_path=config,
                        python_executable="python3",
                    )

            self.assertTrue(result.changed)
            text = config.read_text()
            self.assertIn(f'[hooks.state."{key}"]', text)
            self.assertIn('trusted_hash = "sha256:new-current-hash"', text)

    def test_update_codex_trusted_hashes_preserves_other_state(self) -> None:
        text = "\n".join(
            [
                "[hooks.state]",
                'source = "keep-me"',
                "",
                '[hooks.state."/tmp/config.toml:pre_tool_use:0:0"]',
                'trusted_hash = "sha256:old"',
                "",
            ]
        )

        updated = update_codex_trusted_hashes(
            text,
            {
                "/tmp/config.toml:pre_tool_use:0:0": "sha256:new",
                "/tmp/config.toml:stop:0:0": "sha256:stop",
            },
        )

        self.assertIn('source = "keep-me"', updated)
        self.assertIn('trusted_hash = "sha256:new"', updated)
        self.assertIn('[hooks.state."/tmp/config.toml:stop:0:0"]', updated)
        self.assertIn('trusted_hash = "sha256:stop"', updated)
        self.assertNotIn("sha256:old", updated)

    def test_claude_installer_replaces_target_hook_and_preserves_other_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = base / "settings.json"
            log = base / "claude.jsonl"
            config.write_text(
                json.dumps(
                    {
                        "permissions": {"allow": ["Bash(date)"]},
                        "hooks": {
                            "PreToolUse": [
                                {
                                    "matcher": "*",
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": f"jq -c . >> {log}",
                                        },
                                        {
                                            "type": "command",
                                            "command": "echo keep >> /tmp/other.log",
                                        },
                                    ],
                                }
                            ]
                        },
                    }
                )
            )

            result = install_claude_hooks(
                log_path=log,
                config_path=config,
                python_executable="python3",
            )

            self.assertTrue(result.changed)
            data = json.loads(config.read_text())
            commands = [
                hook["command"]
                for entry in data["hooks"]["PreToolUse"]
                for hook in entry["hooks"]
            ]
            self.assertIn("echo keep >> /tmp/other.log", commands)
            self.assertTrue(any("hook_entry.py" in command for command in commands))
            self.assertFalse(any(command.startswith("jq -c") for command in commands))
            self.assertEqual(data["permissions"]["allow"], ["Bash(date)"])

    def test_grok_installer_writes_global_hook_file_without_lifecycle_matchers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = base / "hooks" / "sidepulse.json"
            log = base / "grok.jsonl"
            config.parent.mkdir()
            config.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "PreToolUse": [
                                {
                                    "matcher": "run_terminal_command",
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": "echo keep >> /tmp/other.log",
                                        },
                                        {
                                            "type": "command",
                                            "command": f"jq -c . >> {log}",
                                        },
                                    ],
                                }
                            ]
                        }
                    }
                )
            )

            result = install_grok_hooks(
                log_path=log,
                config_path=config,
                python_executable="python3",
            )

            self.assertTrue(result.changed)
            data = json.loads(config.read_text())
            pre_tool_commands = [
                hook["command"]
                for entry in data["hooks"]["PreToolUse"]
                for hook in entry["hooks"]
            ]
            self.assertIn("echo keep >> /tmp/other.log", pre_tool_commands)
            self.assertTrue(any("--provider grok" in command for command in pre_tool_commands))
            self.assertFalse(any(command.startswith("jq -c") for command in pre_tool_commands))
            self.assertIn("matcher", data["hooks"]["PreToolUse"][-1])
            self.assertNotIn("matcher", data["hooks"]["SessionStart"][-1])

    def test_grok_installer_removes_legacy_sidepulse_hook_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = base / "hooks" / "sidepulse.json"
            log = base / "grok.jsonl"
            config.parent.mkdir()
            old_backup = config.parent / "sidepulse.json.bak.20260813T214318Z"
            old_backup.write_text('{"hooks": {"Stop": [{"hooks": []}]}}\n')
            stale = config.parent / "sidepulse-cli.json"
            stale.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "PreToolUse": [
                                {
                                    "matcher": "*",
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": (
                                                "python3 /missing/sidepulse_cli/hook_entry.py "
                                                f"--provider grok --log {log}"
                                            ),
                                        }
                                    ],
                                }
                            ]
                        }
                    }
                )
            )
            mixed = config.parent / "sidepulse-agent-monitor.json"
            mixed.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "Notification": [
                                {
                                    "matcher": "*",
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": (
                                                "python3 /missing/agent_monitor/hook_entry.py "
                                                f"--provider grok --log {log}"
                                            ),
                                        },
                                        {
                                            "type": "command",
                                            "command": "echo keep >> /tmp/other.log",
                                        },
                                    ],
                                }
                            ]
                        }
                    }
                )
            )

            result = install_grok_hooks(
                log_path=log,
                config_path=config,
                python_executable="python3",
            )

            self.assertTrue(result.changed)
            self.assertFalse(stale.exists())
            self.assertFalse(old_backup.exists())
            self.assertTrue((base / "sidepulse-hook-backups" / old_backup.name).exists())
            data = json.loads(mixed.read_text())
            commands = [
                hook["command"]
                for entry in data["hooks"]["Notification"]
                for hook in entry["hooks"]
            ]
            self.assertEqual(commands, ["echo keep >> /tmp/other.log"])

    def test_codex_uninstaller_removes_monitor_hooks_and_preserves_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = base / "config.toml"
            log = base / "codex.jsonl"
            config.write_text(
                "\n".join(
                    [
                        "[features]",
                        "js_repl = false",
                        "",
                        "[hooks.state]",
                        'source = "keep-me"',
                        "",
                    ]
                )
            )
            install_codex_hooks(log_path=log, config_path=config, python_executable="python3")

            result = uninstall_codex_hooks(log_path=log, config_path=config)

            self.assertTrue(result.changed)
            text = config.read_text()
            self.assertIn("[features]", text)
            self.assertIn("js_repl = false", text)
            self.assertIn("[hooks.state]", text)
            self.assertIn('source = "keep-me"', text)
            self.assertNotIn("agent-monitor hooks", text)
            self.assertNotIn(str(log), text)

    def test_claude_uninstaller_removes_monitor_hooks_and_preserves_other_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = base / "settings.json"
            log = base / "claude.jsonl"
            config.write_text(
                json.dumps(
                    {
                        "permissions": {"allow": ["Bash(date)"]},
                        "hooks": {
                            "PreToolUse": [
                                {
                                    "matcher": "*",
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": "echo keep >> /tmp/other.log",
                                        }
                                    ],
                                }
                            ]
                        },
                    }
                )
            )
            install_claude_hooks(log_path=log, config_path=config, python_executable="python3")

            result = uninstall_claude_hooks(log_path=log, config_path=config)

            self.assertTrue(result.changed)
            data = json.loads(config.read_text())
            commands = [
                hook["command"]
                for entry in data["hooks"]["PreToolUse"]
                for hook in entry["hooks"]
            ]
            self.assertEqual(commands, ["echo keep >> /tmp/other.log"])
            self.assertEqual(data["permissions"]["allow"], ["Bash(date)"])

    def test_grok_uninstaller_removes_monitor_hooks_and_preserves_other_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = base / "hooks" / "sidepulse.json"
            log = base / "grok.jsonl"
            config.parent.mkdir()
            config.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "PreToolUse": [
                                {
                                    "matcher": "run_terminal_command",
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": "echo keep >> /tmp/other.log",
                                        }
                                    ],
                                }
                            ]
                        }
                    }
                )
            )
            install_grok_hooks(log_path=log, config_path=config, python_executable="python3")

            result = uninstall_grok_hooks(log_path=log, config_path=config)

            self.assertTrue(result.changed)
            data = json.loads(config.read_text())
            commands = [
                hook["command"]
                for entry in data["hooks"]["PreToolUse"]
                for hook in entry["hooks"]
            ]
            self.assertEqual(commands, ["echo keep >> /tmp/other.log"])

    def test_grok_uninstaller_removes_legacy_sidepulse_hook_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = base / "hooks" / "sidepulse.json"
            log = base / "grok.jsonl"
            config.parent.mkdir()
            stale = config.parent / "sidepulse-cli.json"
            stale.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "Stop": [
                                {
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": (
                                                "python3 /missing/sidepulse_cli/hook_entry.py "
                                                f"--provider grok --log {log}"
                                            ),
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                )
            )

            result = uninstall_grok_hooks(log_path=log, config_path=config)

            self.assertTrue(result.changed)
            self.assertFalse(stale.exists())

    def test_detect_grok_config_reads_managed_hook_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            config = home / ".grok" / "hooks" / "sidepulse.json"
            log = home / "state" / "grok.jsonl"
            install_grok_hooks(log_path=log, config_path=config, python_executable="python3")

            detected = detect_grok_config(home)

            self.assertEqual(detected.provider, "grok")
            self.assertTrue(detected.exists)
            self.assertTrue(detected.hooks_enabled)
            self.assertIn("PreToolUse", detected.hook_events)
            self.assertIn(log, detected.log_paths)

    def test_sidepulse_sidepulse_command_shape(self) -> None:
        parser = build_parser(prog="sidepulse agent-monitor")

        install = parser.parse_args(["install"])
        live = parser.parse_args(["live", "--recent-seconds", "120"])
        leds = parser.parse_args(["leds", "--once", "--dry-run"])
        uninstall = parser.parse_args(["uninstall"])
        status_bar = parser.parse_args(["status-bar"])
        status_bar_foreground = parser.parse_args(["status-bar", "--foreground"])
        grok_install = parser.parse_args(["install", "grok"])
        grok_hook_log = parser.parse_args(["hook-log", "--provider", "grok", "--log", "/tmp/grok.jsonl"])

        self.assertEqual(install.provider, "all")
        self.assertEqual(grok_install.provider, "grok")
        self.assertEqual(live.command, "live")
        self.assertEqual(live.recent_seconds, 120)
        self.assertEqual(leds.command, "leds")
        self.assertTrue(leds.once)
        self.assertTrue(leds.dry_run)
        self.assertEqual(uninstall.provider, "all")
        self.assertEqual(status_bar.command, "status-bar")
        self.assertFalse(status_bar.foreground)
        self.assertFalse(status_bar.uninstall)
        self.assertTrue(status_bar_foreground.foreground)
        self.assertEqual(grok_hook_log.provider, "grok")
        self.assertIn("sidepulse agent-monitor", parser.format_usage())

    def test_sidepulse_entrypoint_dispatches_to_sidepulse(self) -> None:
        with patch.object(cli_module, "main", return_value=17) as main:
            result = cli_module.sidepulse_main(["agent-monitor", "live"])

        self.assertEqual(result, 17)
        main.assert_called_once_with(["live"], prog="sidepulse agent-monitor")

    def test_sidepulse_battery_command_shape(self) -> None:
        parser = cli_module.build_sidepulse_parser()

        status = parser.parse_args(["battery", "status", "--json"])
        leds = parser.parse_args(["battery", "leds", "--once", "--dry-run", "--full-watts", "140"])
        configure = parser.parse_args(["battery", "configure", "--display", "battery"])

        self.assertEqual(status.command, "battery")
        self.assertEqual(status.battery_command, "status")
        self.assertTrue(status.json)
        self.assertEqual(leds.battery_command, "leds")
        self.assertTrue(leds.once)
        self.assertTrue(leds.dry_run)
        self.assertEqual(leds.full_watts, "140")
        self.assertEqual(configure.battery_command, "configure")
        self.assertEqual(configure.display, "battery")

    def test_sidepulse_status_bar_root_command_shape(self) -> None:
        parser = cli_module.build_sidepulse_parser()

        default = parser.parse_args(["status-bar"])
        start = parser.parse_args(["status-bar", "start", "--foreground"])
        stop = parser.parse_args(["status-bar", "stop"])
        helper = parser.parse_args(["status-bar", "install-sleep-helper", "--dry-run"])

        self.assertEqual(default.command, "status-bar")
        self.assertEqual(default.status_bar_command, "start")
        self.assertFalse(default.foreground)
        self.assertEqual(start.status_bar_command, "start")
        self.assertTrue(start.foreground)
        self.assertEqual(stop.status_bar_command, "stop")
        self.assertEqual(helper.status_bar_command, "install-sleep-helper")
        self.assertTrue(helper.dry_run)

    def test_python_module_entrypoint_uses_sidepulse_cli(self) -> None:
        root = Path(__file__).resolve().parents[1]
        env = {
            **os.environ,
            "PYTHONPATH": str(root / "src"),
        }
        result = subprocess.run(
            [sys.executable, "-m", "sidepulse", "status-bar", "--help"],
            cwd=root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Helo World", result.stdout)
        self.assertIn("Start/stop the menu-bar app", result.stdout)

    def test_sidepulse_sdejectguard_command_shape(self) -> None:
        parser = cli_module.build_sidepulse_parser()

        start = parser.parse_args(["sdejectguard", "start"])
        interactive = parser.parse_args(["sdejectguard", "start", "-it", "--scope", "user"])
        stop = parser.parse_args(["sdejectguard", "stop", "--scope", "system"])
        uninstall = parser.parse_args(["sdejectguard", "uninstall", "--scope", "user", "--dry-run"])
        logs = parser.parse_args(["sdejectguard", "logs", "--lines", "12", "--follow"])

        self.assertEqual(start.command, "sdejectguard")
        self.assertEqual(start.sdejectguard_command, "start")
        self.assertEqual(start.scope, "auto")
        self.assertFalse(start.interactive)
        self.assertTrue(interactive.interactive)
        self.assertEqual(interactive.scope, "user")
        self.assertEqual(stop.sdejectguard_command, "stop")
        self.assertEqual(stop.scope, "system")
        self.assertEqual(uninstall.sdejectguard_command, "uninstall")
        self.assertEqual(uninstall.scope, "user")
        self.assertTrue(uninstall.dry_run)
        self.assertEqual(logs.sdejectguard_command, "logs")
        self.assertEqual(logs.lines, 12)
        self.assertTrue(logs.follow)

    def test_sidepulse_sdejectguard_start_uses_launchd_installer(self) -> None:
        parser = cli_module.build_sidepulse_parser()
        args = parser.parse_args(["sdejectguard", "start", "--scope", "user"])
        guard_result = SimpleNamespace(
            dry_run=False,
            changed=True,
            started=True,
            scope="user",
            plist_path=Path("/tmp/io.sidepulse.sdejectguard.plist"),
            binary_path=Path("/tmp/sd_eject_guard"),
            cleanup_removed=None,
            cleanup_skipped=None,
        )

        with patch(
            "sidepulse.sd_eject_guard_launch.install_sd_eject_guard",
            return_value=guard_result,
        ) as install:
            result = cli_module.cmd_sidepulse_sdejectguard_start(args)

        self.assertEqual(result, 0)
        install.assert_called_once_with(scope="user", dry_run=False)

    def test_sidepulse_sdejectguard_start_interactive_runs_foreground(self) -> None:
        parser = cli_module.build_sidepulse_parser()
        args = parser.parse_args(["sdejectguard", "start", "-it", "--scope", "user"])

        with patch(
            "sidepulse.sd_eject_guard_launch.run_sd_eject_guard_interactive",
            return_value=0,
        ) as run:
            result = cli_module.cmd_sidepulse_sdejectguard_start(args)

        self.assertEqual(result, 0)
        run.assert_called_once_with(scope="user")

    def test_sidepulse_sdejectguard_stop_calls_guard_stop(self) -> None:
        parser = cli_module.build_sidepulse_parser()
        args = parser.parse_args(["sdejectguard", "stop", "--scope", "user", "--dry-run"])
        stop_result = SimpleNamespace(
            scope="user",
            plist_path=Path("/tmp/io.sidepulse.sdejectguard.plist"),
            stopped=True,
            skipped=None,
        )

        with patch(
            "sidepulse.sd_eject_guard_launch.stop_sd_eject_guard",
            return_value=(stop_result,),
        ) as stop:
            result = cli_module.cmd_sidepulse_sdejectguard_stop(args)

        self.assertEqual(result, 0)
        stop.assert_called_once_with(scope="user", dry_run=True)

    def test_sidepulse_sdejectguard_uninstall_calls_guard_uninstall(self) -> None:
        parser = cli_module.build_sidepulse_parser()
        args = parser.parse_args(["sdejectguard", "uninstall", "--scope", "user", "--dry-run"])
        uninstall_result = SimpleNamespace(
            scope="user",
            plist_path=Path("/tmp/io.sidepulse.sdejectguard.plist"),
            removed_paths=(Path("/tmp/io.sidepulse.sdejectguard.plist"),),
            skipped=None,
            dry_run=True,
        )

        with patch(
            "sidepulse.sd_eject_guard_launch.uninstall_sd_eject_guard",
            return_value=(uninstall_result,),
        ) as uninstall:
            result = cli_module.cmd_sidepulse_sdejectguard_uninstall(args)

        self.assertEqual(result, 0)
        uninstall.assert_called_once_with(scope="user", dry_run=True)

    def test_sidepulse_setup_command_shape(self) -> None:
        parser = cli_module.build_sidepulse_parser()

        default = parser.parse_args(["setup"])
        codex_only = parser.parse_args(
            [
                "setup",
                "codex",
                "--no-status-bar",
                "--dry-run",
                "--sd-eject-guard-scope",
                "user",
            ]
        )

        self.assertEqual(default.command, "setup")
        self.assertEqual(default.provider, "all")
        self.assertEqual(default.sd_eject_guard_scope, "auto")
        self.assertFalse(default.no_status_bar)
        self.assertFalse(default.dry_run)
        self.assertEqual(codex_only.provider, "codex")
        self.assertEqual(codex_only.sd_eject_guard_scope, "user")
        self.assertTrue(codex_only.no_status_bar)
        self.assertTrue(codex_only.dry_run)

    def test_sidepulse_setup_installs_hooks_guard_and_status_bar(self) -> None:
        parser = cli_module.build_sidepulse_parser()
        args = parser.parse_args(["setup"])
        codex_result = SimpleNamespace(
            provider="codex",
            config_path=Path("/tmp/codex.toml"),
            log_path=Path("/tmp/codex.jsonl"),
            changed=True,
            backup_path=None,
        )
        claude_result = SimpleNamespace(
            provider="claude",
            config_path=Path("/tmp/settings.json"),
            log_path=Path("/tmp/claude.jsonl"),
            changed=False,
            backup_path=None,
        )
        grok_result = SimpleNamespace(
            provider="grok",
            config_path=Path("/tmp/grok-hook.json"),
            log_path=Path("/tmp/grok.jsonl"),
            changed=True,
            backup_path=None,
        )
        launch_result = SimpleNamespace(
            plist_path=Path("/tmp/io.sidepulse.agentstatus.plist"),
            changed=True,
            started=True,
        )
        guard_result = SimpleNamespace(
            dry_run=False,
            changed=True,
            started=True,
            scope="user",
            plist_path=Path("/tmp/io.sidepulse.sdejectguard.plist"),
            binary_path=Path("/tmp/sd_eject_guard"),
            cleanup_removed=None,
            cleanup_skipped=None,
        )

        with (
            patch.object(cli_module, "install_codex_hooks", return_value=codex_result) as codex,
            patch.object(cli_module, "install_claude_hooks", return_value=claude_result) as claude,
            patch.object(cli_module, "install_grok_hooks", return_value=grok_result) as grok,
            patch(
                "sidepulse.sd_eject_guard_launch.install_sd_eject_guard",
                return_value=guard_result,
            ) as guard,
            patch(
                "sidepulse.status_bar_launch.install_launch_agent",
                return_value=launch_result,
            ) as launch,
        ):
            result = cli_module.cmd_sidepulse_setup(args)

        self.assertEqual(result, 0)
        codex.assert_called_once()
        claude.assert_called_once()
        grok.assert_called_once()
        guard.assert_called_once_with(scope="auto", dry_run=False)
        launch.assert_called_once_with(start=True)

    def test_sidepulse_setup_no_status_bar_still_installs_guard(self) -> None:
        parser = cli_module.build_sidepulse_parser()
        args = parser.parse_args(["setup", "--no-status-bar", "--sd-eject-guard-scope", "user"])
        hook_result = SimpleNamespace(
            provider="codex",
            config_path=Path("/tmp/codex.toml"),
            log_path=Path("/tmp/codex.jsonl"),
            changed=False,
            backup_path=None,
        )
        guard_result = SimpleNamespace(
            dry_run=False,
            changed=False,
            started=True,
            scope="user",
            plist_path=Path("/tmp/io.sidepulse.sdejectguard.plist"),
            binary_path=Path("/tmp/sd_eject_guard"),
            cleanup_removed=None,
            cleanup_skipped=None,
        )

        with (
            patch.object(cli_module, "install_hook_results", return_value=[hook_result]),
            patch(
                "sidepulse.sd_eject_guard_launch.install_sd_eject_guard",
                return_value=guard_result,
            ) as guard,
            patch("sidepulse.status_bar_launch.install_launch_agent") as launch,
        ):
            result = cli_module.cmd_sidepulse_setup(args)

        self.assertEqual(result, 0)
        guard.assert_called_once_with(scope="user", dry_run=False)
        launch.assert_not_called()

    def test_sidepulse_write_decodes_escaped_newlines_and_writes_leds_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            device = Path(tmp) / "SidePulsePro"
            device.mkdir()

            target = write_led_program(
                r"off\n#FF00FF pulse",
                device_path=device,
            )

            self.assertEqual(target, device / "LEDS.LED")
            self.assertEqual(target.read_text(), "off\n#FF00FF pulse")

    def test_sidepulse_write_uses_leds_led_even_when_old_file_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            device = Path(tmp) / "SidePulseDot"
            device.mkdir()
            (device / "LEDS.TXT").write_text("off")

            target = write_led_program(
                r"off\n#FF00FF pulse",
                device_path=device,
            )

            self.assertEqual(target, device / "LEDS.LED")
            self.assertEqual(target.read_text(), "off\n#FF00FF pulse")
            self.assertEqual((device / "LEDS.TXT").read_text(), "off")

    def test_sidepulse_write_discovers_sidepulse_dot_volume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mount_root = Path(tmp)
            device = mount_root / "SidePulseDot"
            device.mkdir()

            candidates = discover_devices(mount_root=mount_root)

            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0].root, device)
            self.assertEqual(candidates[0].target, device / "LEDS.LED")

    def test_sidepulse_write_prefers_leds_led_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mount_root = Path(tmp)
            device = mount_root / "SidePulsePro"
            device.mkdir()
            (device / "LEDS.LED").write_text("off")

            candidates = discover_devices(mount_root=mount_root)

            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0].target, device / "LEDS.LED")

    def test_device_discovery_ignores_old_leds_txt_on_unnamed_volume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mount_root = Path(tmp)
            device = mount_root / "USB Drive"
            device.mkdir()
            (device / "LEDS.TXT").write_text("off")

            candidates = discover_devices(mount_root=mount_root)

            self.assertEqual(candidates, [])

    def test_device_discovery_skips_mount_io_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mount_root = Path(tmp)
            good = mount_root / "SidePulseDot"
            bad = mount_root / "SidePulsePro"
            good.mkdir()
            bad.mkdir()
            (good / "LEDS.LED").write_text("off")
            original_is_dir = Path.is_dir

            def flaky_is_dir(path: Path) -> bool:
                if path == bad:
                    raise OSError("offline")
                return original_is_dir(path)

            with patch.object(Path, "is_dir", flaky_is_dir):
                candidates = discover_devices(mount_root=mount_root)

            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0].root, good)

    def test_sidepulse_write_validates_device_limits(self) -> None:
        self.assertEqual(normalize_led_text(r"off\n#FF00FF pulse"), "off\n#FF00FF pulse")
        with self.assertRaises(DeviceWriteError):
            write_led_program("x" * 513, device_path=Path("/tmp/device"), dry_run=True)
        write_led_program("\n".join(["off"] * 20), device_path=Path("/tmp/device"), dry_run=True)
        with self.assertRaises(DeviceWriteError):
            write_led_program("\n".join(["off"] * 21), device_path=Path("/tmp/device"), dry_run=True)

    def test_sidepulse_write_entrypoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            device = Path(tmp) / "SidePulseDot"
            device.mkdir()
            result = cli_module.sidepulse_main(
                ["write", r"off\n#FF00FF pulse", "--device", str(device)]
            )

            self.assertEqual(result, 0)
            self.assertEqual((device / "LEDS.LED").read_text(), "off\n#FF00FF pulse")

    def test_led_status_maps_agent_modes_to_programs(self) -> None:
        self.assertEqual(
            display_state_for_mode(AgentMode.WAITING_FOR_INPUT),
            LedDisplayState.ASK,
        )
        self.assertEqual(
            display_state_for_mode(AgentMode.TOOL_RUNNING),
            LedDisplayState.WORKING,
        )
        self.assertEqual(
            display_state_for_mode(AgentMode.COMPLETED),
            LedDisplayState.DONE,
        )
        self.assertEqual(
            display_state_for_mode(AgentMode.IDLE_READY),
            LedDisplayState.IDLE,
        )

        self.assertEqual(
            program_for_display_state(LedDisplayState.IDLE),
            "off 2s\n3:#006060 4:#006060 2s ease\nrepeat",
        )
        self.assertEqual(
            program_for_display_state(LedDisplayState.DONE),
            "#00FF66 320ms cosine",
        )
        self.assertIn("#FF3A00 1.6s pulse", program_for_display_state(LedDisplayState.ASK))
        self.assertEqual(
            program_for_display_state(LedDisplayState.WORKING, led_count=2).splitlines(),
            [
                "off 320ms cosine",
                "0:#00E5FF 760ms pulse 0ms; 1:#00E5FF 760ms pulse 260ms",
                "repeat",
            ],
        )
        self.assertEqual(
            len(program_for_display_state(LedDisplayState.WORKING, led_count=8).splitlines()),
            3,
        )
        kitt_program = program_for_agent_mode(
            AgentMode.WORKING,
            led_count=8,
            brightness=128,
            animation_style=AGENT_ANIMATION_KITT,
        )
        validate_led_text(kitt_program)
        self.assertLessEqual(len(kitt_program.encode("utf-8")), 512)
        self.assertIn("7:#00E5FF 320ms pulse 595ms", kitt_program)
        self.assertIn("6:#00E5FF 320ms pulse 0ms", kitt_program)
        red_kitt_program = program_for_agent_mode(
            AgentMode.WORKING,
            led_count=8,
            animation_style=AGENT_ANIMATION_KITT_RED,
        )
        validate_led_text(red_kitt_program)
        self.assertIn("7:#FF1800 320ms pulse 595ms", red_kitt_program)
        for mode in AgentMode:
            with self.subTest(kitt_mode=mode.value):
                validate_led_text(
                    program_for_agent_mode(
                        mode,
                        animation_style=AGENT_ANIMATION_KITT,
                    )
                )
        custom_program = program_for_agent_mode(
            AgentMode.COMPLETED,
            brightness=64,
            animation_style=AGENT_ANIMATION_CUSTOM,
            custom_program="off\\n#FF0000 pulse\\nrepeat",
        )
        self.assertEqual(
            custom_program,
            "brightness 64\noff\n#FF0000 pulse\nrepeat",
        )
        self.assertEqual(
            program_for_display_state(LedDisplayState.DONE, brightness=128),
            "brightness 128\n#00FF66 320ms cosine",
        )

    def test_builtin_animation_programs_are_loaded_from_led_files(self) -> None:
        self.assertEqual(
            builtin_animation_program(AGENT_ANIMATION_SLOW_OFF, 2),
            "off 1s",
        )
        self.assertEqual(
            builtin_animation_program(AGENT_ANIMATION_SLOW_OFF, 8),
            "off 1s",
        )
        self.assertEqual(
            builtin_animation_program(AGENT_ANIMATION_IMMEDIATE_OFF, 2),
            "off",
        )
        self.assertEqual(
            builtin_animation_program(AGENT_ANIMATION_IMMEDIATE_OFF, 8),
            "off",
        )
        self.assertEqual(builtin_animation_file_name("idle-pulse", 2), "idle-pulse-2.LED")
        self.assertEqual(builtin_animation_file_name("idle-pulse", 8), "idle-pulse-8.LED")
        self.assertEqual(
            builtin_animation_program("idle-pulse", 8),
            "off 2s\n3:#006060 4:#006060 2s ease\nrepeat",
        )
        self.assertEqual(
            builtin_animation_program("idle-pulse", 2),
            "off 2s\n0:#006060 1:#006060 2s ease\nrepeat",
        )
        self.assertEqual(builtin_animation_file_name("cyan-roll", 2), "cyan-roll-2.LED")
        self.assertEqual(builtin_animation_file_name("cyan-roll", 8), "cyan-roll-8.LED")
        self.assertEqual(builtin_animation_file_name("cyan-roll", 5), "cyan-roll-8.LED")
        self.assertIn("1:#00E5FF", builtin_animation_program("cyan-roll", 2))
        self.assertNotIn("2:#00E5FF", builtin_animation_program("cyan-roll", 2))
        self.assertIn("7:#FF1800", builtin_animation_program("kitt-red", 8))
        self.assertEqual(
            builtin_animation_file_name("ember-tide", 2),
            "ember-tide-2.LED",
        )
        self.assertIn(
            "1:#F23819 760ms pulse 260ms",
            builtin_animation_program("ember-tide", 2),
        )
        self.assertIn(
            "7:#F23819 760ms pulse 665ms",
            builtin_animation_program("ember-tide", 8),
        )
        self.assertIn(
            "7:#FF1200 360ms pulse 630ms",
            builtin_animation_program("night-rider", 8),
        )
        self.assertEqual(
            builtin_animation_file_name(AGENT_ANIMATION_PURPLE_IDLE, 2),
            "purple-idle-2.LED",
        )
        self.assertIn(
            "2:#9F00FF 3:#FF00FF 4:#FF00FF 5:#9F009F 2s ease",
            builtin_animation_program(AGENT_ANIMATION_PURPLE_IDLE, 8),
        )
        self.assertIn(
            "1:#FF00FF 760ms pulse 260ms",
            builtin_animation_program(AGENT_ANIMATION_PURPLE_TIDE, 2),
        )
        self.assertIn(
            "#FF00FF 1.6s pulse",
            builtin_animation_program(AGENT_ANIMATION_PURPLE_ATTENTION, 8),
        )
        self.assertEqual(
            builtin_animation_program(AGENT_ANIMATION_PURPLE_COMPLETE, 8),
            "#FF00FF 320ms cosine",
        )
        self.assertIn(
            "#FF00FF 220ms ease",
            builtin_animation_program(AGENT_ANIMATION_PURPLE_LID_OPEN, 8),
        )

    def test_color_profiles_use_the_exact_cyan_animation_shapes(self) -> None:
        def without_colors(program: str) -> str:
            return re.sub(r"#[0-9A-Fa-f]{6}", "#COLOR", program)

        for led_count in (2, 8):
            cyan_idle_shape = without_colors(
                builtin_animation_program(AGENT_ANIMATION_IDLE_PULSE, led_count)
            )
            self.assertEqual(
                without_colors(
                    builtin_animation_program(AGENT_ANIMATION_EMBER_IDLE, led_count)
                ),
                cyan_idle_shape,
            )
            if led_count == 2:
                self.assertEqual(
                    without_colors(
                        builtin_animation_program(
                            AGENT_ANIMATION_PURPLE_IDLE,
                            led_count,
                        )
                    ),
                    cyan_idle_shape,
                )

        corresponding_animations = (
            (
                AGENT_ANIMATION_CYAN_ROLL,
                AGENT_ANIMATION_EMBER_TIDE,
                AGENT_ANIMATION_PURPLE_TIDE,
            ),
            (
                AGENT_ANIMATION_AMBER_PULSE,
                AGENT_ANIMATION_EMBER_ATTENTION,
                AGENT_ANIMATION_PURPLE_ATTENTION,
            ),
            (
                AGENT_ANIMATION_CYAN_COMPLETE,
                AGENT_ANIMATION_EMBER_COMPLETE,
                AGENT_ANIMATION_PURPLE_COMPLETE,
            ),
            (
                AGENT_ANIMATION_LID_OPEN,
                AGENT_ANIMATION_EMBER_LID_OPEN,
                AGENT_ANIMATION_PURPLE_LID_OPEN,
            ),
        )
        for led_count in (2, 8):
            for cyan_id, ember_id, purple_id in corresponding_animations:
                cyan_shape = without_colors(
                    builtin_animation_program(cyan_id, led_count)
                )
                with self.subTest(profile="Ember", animation=cyan_id, leds=led_count):
                    self.assertEqual(
                        without_colors(
                            builtin_animation_program(ember_id, led_count)
                        ),
                        cyan_shape,
                    )
                with self.subTest(profile="Purple", animation=cyan_id, leds=led_count):
                    self.assertEqual(
                        without_colors(
                            builtin_animation_program(purple_id, led_count)
                        ),
                        cyan_shape,
                    )

    def test_purple_profile_uses_ff00ff_for_every_colored_animation(self) -> None:
        purple_animations = (
            AGENT_ANIMATION_PURPLE_TIDE,
            AGENT_ANIMATION_PURPLE_ATTENTION,
            AGENT_ANIMATION_PURPLE_COMPLETE,
            AGENT_ANIMATION_PURPLE_LID_OPEN,
        )
        for led_count in (2, 8):
            for animation_id in purple_animations:
                with self.subTest(animation=animation_id, leds=led_count):
                    self.assertEqual(
                        set(
                            re.findall(
                                r"#[0-9A-Fa-f]{6}",
                                builtin_animation_program(animation_id, led_count),
                            )
                        ),
                        {"#FF00FF"},
                    )
        self.assertEqual(
            builtin_animation_program(AGENT_ANIMATION_PURPLE_IDLE, 2),
            "off 2s\n0:#FF00FF 1:#FF00FF 2s ease\nrepeat",
        )
        self.assertEqual(
            builtin_animation_program(AGENT_ANIMATION_PURPLE_IDLE, 8),
            "off 2s\n"
            "2:#9F00FF 3:#FF00FF 4:#FF00FF 5:#9F009F 2s ease\n"
            "repeat",
        )

    def test_ember_profile_uses_f23819_for_every_colored_animation(self) -> None:
        ember_animations = (
            AGENT_ANIMATION_EMBER_IDLE,
            AGENT_ANIMATION_EMBER_TIDE,
            AGENT_ANIMATION_EMBER_ATTENTION,
            AGENT_ANIMATION_EMBER_COMPLETE,
            AGENT_ANIMATION_EMBER_LID_OPEN,
        )
        for led_count in (2, 8):
            for animation_id in ember_animations:
                with self.subTest(animation=animation_id, leds=led_count):
                    self.assertEqual(
                        set(
                            re.findall(
                                r"#[0-9A-Fa-f]{6}",
                                builtin_animation_program(animation_id, led_count),
                            )
                        ),
                        {"#F23819"},
                    )

    def test_core_state_transitions_and_lid_close_outside_in_program(self) -> None:
        completed = builtin_animation_program("solid-green", 8)
        working = builtin_animation_program("cyan-roll", 8)
        lid_closed = builtin_animation_program("lid-closed", 8)

        self.assertEqual(completed, "#00FF66 320ms cosine")
        self.assertTrue(working.startswith("off 320ms cosine\n"))
        self.assertEqual(
            lid_closed.splitlines(),
            [
                "0:#000000 75ms ease; 7:#000000 75ms ease",
                "1:#000000 75ms ease; 6:#000000 75ms ease",
                "2:#000000 75ms ease; 5:#000000 75ms ease",
                "3:#000000 75ms ease; 4:#000000 75ms ease",
                "#000000 1s",
            ],
        )
        self.assertEqual(
            builtin_animation_program("lid-closed", 2),
            "0:#000000 300ms ease; 1:#000000 300ms ease\n#000000 1s",
        )
        self.assertEqual(
            default_lid_animation(LID_ANIMATION_CLOSED).duration_seconds,
            1.3,
        )

    def test_device_brightness_multiplies_authored_file_brightness(self) -> None:
        self.assertEqual(
            apply_brightness("brightness 128\n#FF0000", 128),
            "brightness 64\n#FF0000",
        )
        self.assertEqual(
            apply_brightness(
                "brightness 200\n#FF0000\nbrightness 100\n#00FF00",
                64,
            ),
            "brightness 50\n#FF0000\nbrightness 25\n#00FF00",
        )

    def test_write_mode_to_leds_uses_device_specific_program(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            device = Path(tmp) / "SidePulseDot"
            device.mkdir()

            result = write_mode_to_leds(AgentMode.WORKING, device_path=device)

            self.assertEqual(result.state, LedDisplayState.WORKING)
            self.assertEqual(result.target, device / "LEDS.LED")
            self.assertEqual(
                (device / "LEDS.LED").read_text(),
                "off 320ms cosine\n"
                "0:#00E5FF 760ms pulse 0ms; 1:#00E5FF 760ms pulse 260ms\n"
                "repeat",
            )

            write_mode_to_leds(AgentMode.IDLE_READY, device_path=device)

            self.assertEqual(
                (device / "LEDS.LED").read_text(),
                "off 2s\n0:#006060 1:#006060 2s ease\nrepeat",
            )

            write_mode_to_leds(AgentMode.COMPLETED, device_path=device, brightness=64)

            self.assertEqual(
                (device / "LEDS.LED").read_text(),
                "brightness 64\n#00FF66 320ms cosine",
            )

    def test_led_count_uses_product_name(self) -> None:
        self.assertEqual(led_count_for_target(Path("/Volumes/SidePulseDot/LEDS.LED")), 2)
        self.assertEqual(led_count_for_target(Path("/Volumes/PulseDot/LEDS.LED")), 2)
        self.assertEqual(led_count_for_target(Path("/Volumes/SidePulsePro/LEDS.LED")), 8)

    def test_sidepulse_working_program_uses_eight_leds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            device = Path(tmp) / "SidePulsePro"
            device.mkdir()

            write_mode_to_leds(AgentMode.WORKING, device_path=device)

            lines = (device / "LEDS.LED").read_text().splitlines()
            self.assertEqual(len(lines), 3)
            self.assertEqual(lines[0], "off 320ms cosine")
            self.assertIn("0:#00E5FF 760ms pulse 0ms", lines[1])
            self.assertIn("5:#00E5FF 760ms pulse 475ms", lines[1])
            self.assertIn("7:#00E5FF 760ms pulse 665ms", lines[1])
            self.assertEqual(lines[-1], "repeat")

    def test_agent_led_controller_skips_unchanged_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            device = Path(tmp) / "SidePulsePro"
            device.mkdir()
            controller = AgentLedController(device_path=device)

            first = controller.sync_mode(AgentMode.COMPLETED)
            second = controller.sync_mode(AgentMode.COMPLETED)
            third = controller.sync_mode(AgentMode.WAITING_FOR_INPUT)

            self.assertTrue(first.changed)
            self.assertFalse(second.changed)
            self.assertTrue(third.changed)
            self.assertIn("#FF3A00 1.6s pulse", (device / "LEDS.LED").read_text())

    def test_agent_led_controller_repairs_externally_changed_program(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            device = Path(tmp) / "SidePulsePro"
            device.mkdir()
            controller = AgentLedController(device_path=device, brightness=211)

            first = controller.sync_mode(
                AgentMode.WORKING,
                animation_style=AGENT_ANIMATION_PURPLE_TIDE,
            )
            target = device / "LEDS.LED"
            target.write_text("off 2s\n3:#006060 4:#006060 2s ease\nrepeat")

            repaired = controller.sync_mode(
                AgentMode.WORKING,
                animation_style=AGENT_ANIMATION_PURPLE_TIDE,
            )

            self.assertTrue(repaired.changed)
            self.assertEqual(target.read_text(), first.program)

    def test_agent_led_controller_tracks_mode_and_animation_choice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            device = Path(tmp) / "SidePulsePro"
            device.mkdir()
            controller = AgentLedController(device_path=device)

            default = controller.sync_mode(AgentMode.WORKING)
            same_default = controller.sync_mode(AgentMode.TOOL_RUNNING)
            kitt = controller.sync_mode(
                AgentMode.WORKING,
                animation_style=AGENT_ANIMATION_KITT,
            )
            custom_tool = controller.sync_mode(
                AgentMode.TOOL_RUNNING,
                animation_style=AGENT_ANIMATION_CUSTOM,
                custom_program="#123456",
            )
            unchanged = controller.sync_mode(
                AgentMode.TOOL_RUNNING,
                animation_style=AGENT_ANIMATION_CUSTOM,
                custom_program="#123456",
            )

            self.assertTrue(default.changed)
            self.assertFalse(same_default.changed)
            self.assertTrue(kitt.changed)
            self.assertTrue(custom_tool.changed)
            self.assertFalse(unchanged.changed)
            self.assertEqual((device / "LEDS.LED").read_text(), "#123456")

    def test_battery_parser_uses_adapter_watts_and_raw_capacity(self) -> None:
        payload = plistlib.dumps(
            [
                {
                    "CurrentCapacity": 50,
                    "ExternalConnected": True,
                    "IsCharging": True,
                    "FullyCharged": False,
                    "Voltage": 12000,
                    "Amperage": 1000,
                    "AppleRawCurrentCapacity": 4000,
                    "AppleRawMaxCapacity": 8000,
                    "DesignCapacity": 10000,
                    "CycleCount": 12,
                    "AdapterDetails": {
                        "Watts": 96,
                        "AdapterVoltage": 20000,
                        "Current": 4800,
                        "UsbHvcMenu": [
                            {"MaxVoltage": 5000, "MaxCurrent": 3000},
                            {"MaxVoltage": 20000, "MaxCurrent": 4800},
                        ],
                    },
                }
            ]
        )

        snapshot = parse_ioreg_battery_plist(payload)

        self.assertEqual(snapshot.percent, 50)
        self.assertTrue(snapshot.is_plugged)
        self.assertTrue(snapshot.is_charging)
        self.assertEqual(snapshot.adapter_power, 96)
        self.assertEqual(snapshot.health_percent, 80)
        self.assertEqual(snapshot.current_capacity_mah, 4000)
        self.assertEqual(len(snapshot.pd_profiles), 2)

    def test_battery_program_matches_simulator_frontier_pulse(self) -> None:
        snapshot = BatterySnapshot(
            percent=50,
            is_plugged=True,
            is_charging=True,
            adapter_watts=70,
            full_charge_watts=140,
        )

        program = program_for_battery(snapshot, led_count=8)

        validate_led_text(program)
        lines = program.splitlines()
        self.assertIn(f"0:{BATTERY_CHARGING_MINT} 360ms ease", lines[0])
        self.assertIn(f"3:{BATTERY_CHARGING_MINT} 360ms ease", lines[0])
        self.assertIn("4:#000000 360ms ease", lines[0])
        self.assertEqual(lines[1], f"4:{BATTERY_CHARGING_MINT} 790ms pulse")
        self.assertEqual(len(lines), 2)
        self.assertNotIn("repeat", program)
        self.assertNotIn("\noff", program)

    def test_unplugged_battery_program_eases_to_static_level(self) -> None:
        snapshot = BatterySnapshot(percent=50, is_plugged=False)

        program = program_for_battery(snapshot, led_count=8)

        validate_led_text(program)
        self.assertEqual(len(program.splitlines()), 1)
        self.assertIn("0:#FFB000 360ms ease", program)
        self.assertIn("3:#FFB000 360ms ease", program)
        self.assertIn("4:#000000 360ms ease", program)
        self.assertNotIn("repeat", program)

    def test_battery_program_uses_partial_next_led(self) -> None:
        snapshot = BatterySnapshot(percent=57, is_plugged=False)

        program = program_for_battery(snapshot, led_count=8)

        validate_led_text(program)
        segments = program.split(";")
        self.assertEqual(segments[0], "0:#00FF66 360ms ease")
        self.assertEqual(segments[3], "3:#00FF66 360ms ease")
        self.assertEqual(segments[4], "4:#008F39 360ms ease")
        self.assertEqual(segments[5], "5:#000000 360ms ease")

    def test_battery_program_uses_brightness_command(self) -> None:
        snapshot = BatterySnapshot(percent=57, is_plugged=False)

        program = program_for_battery(snapshot, led_count=8, brightness=128)

        validate_led_text(program)
        self.assertTrue(program.startswith("brightness 128\n"))

    def test_battery_program_uses_full_speed_steady_pulse(self) -> None:
        snapshot = BatterySnapshot(
            percent=80,
            is_plugged=True,
            is_charging=True,
            adapter_watts=140,
            full_charge_watts=140,
        )

        program = program_for_battery(snapshot, led_count=8)

        validate_led_text(program)
        self.assertIn(f"6:{BATTERY_CHARGING_MINT} 1400ms pulse", program)
        self.assertNotIn("repeat", program)
        self.assertNotIn("none", program)

    def test_battery_led_controller_animates_charging_on_cadence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            device = Path(tmp) / "SidePulsePro"
            device.mkdir()
            controller = BatteryLedController(device_path=device)
            snapshot = BatterySnapshot(
                percent=50,
                is_plugged=True,
                is_charging=True,
                adapter_watts=70,
                full_charge_watts=140,
            )

            with patch(
                "sidepulse.battery.time.monotonic",
                side_effect=[0.0, 0.5, 2.0],
            ):
                first = controller.sync_snapshot(snapshot)
                second = controller.sync_snapshot(snapshot)
                third = controller.sync_snapshot(snapshot)

            self.assertTrue(first.changed)
            self.assertFalse(second.changed)
            self.assertTrue(third.changed)

    def test_battery_led_controller_skips_unchanged_static_level(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            device = Path(tmp) / "SidePulsePro"
            device.mkdir()
            controller = BatteryLedController(device_path=device)
            snapshot = BatterySnapshot(percent=50, is_plugged=False)

            with patch(
                "sidepulse.battery.time.monotonic",
                side_effect=[0.0, 10.0],
            ):
                first = controller.sync_snapshot(snapshot)
                second = controller.sync_snapshot(snapshot)

            self.assertTrue(first.changed)
            self.assertFalse(second.changed)

    def test_keep_awake_holds_working_then_graces_done(self) -> None:
        processes: list[FakeProcess] = []

        def factory(*_args, **_kwargs):
            process = FakeProcess()
            processes.append(process)
            return process

        controller = KeepAwakeController(
            grace_seconds=300,
            process_factory=factory,
        )

        self.assertTrue(controller.update(AgentMode.WORKING, now=100))
        self.assertEqual(len(processes), 1)
        self.assertTrue(controller.process_running())

        self.assertTrue(controller.update(AgentMode.COMPLETED, now=110))
        self.assertIn("grace", controller.detail(now=110))

        self.assertTrue(controller.update(AgentMode.IDLE_READY, now=200))
        self.assertTrue(controller.process_running())

        self.assertFalse(controller.update(AgentMode.IDLE_READY, now=411))
        self.assertFalse(controller.process_running())
        self.assertTrue(processes[0].terminated)

    def test_keep_awake_ask_grace_expires_without_refresh_extension(self) -> None:
        processes: list[FakeProcess] = []

        def factory(*_args, **_kwargs):
            process = FakeProcess()
            processes.append(process)
            return process

        controller = KeepAwakeController(
            grace_seconds=300,
            process_factory=factory,
        )

        self.assertTrue(controller.update(AgentMode.WAITING_FOR_INPUT, now=100))
        self.assertTrue(controller.update(AgentMode.WAITING_FOR_INPUT, now=350))
        self.assertFalse(controller.update(AgentMode.WAITING_FOR_INPUT, now=401))
        self.assertEqual(len(processes), 1)
        self.assertTrue(processes[0].terminated)

    def test_keep_awake_touches_keepalive_file_once_per_interval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            device = Path(tmp) / "SidePulsePro"
            device.mkdir()
            status_path = device / "keepalive"
            reads: list[Path] = []

            controller = KeepAwakeController(
                status_read_seconds=60,
                status_reader=lambda path: reads.append(path),
                status_read_async=False,
            )

            self.assertEqual(status_file_for_target(device / "LEDS.LED"), status_path)
            self.assertEqual(status_file_for_target(device / "STATUS.TXT"), status_path)
            self.assertEqual(
                controller.poke_status_file(device / "LEDS.LED", now=0),
                status_path,
            )
            self.assertIsNone(controller.poke_status_file(device / "LEDS.LED", now=30))
            self.assertEqual(
                controller.poke_status_file(device / "LEDS.LED", now=61),
                status_path,
            )
            self.assertEqual(reads, [status_path, status_path])

    def test_closed_lid_awake_policy_decisions(self) -> None:
        self.assertFalse(
            closed_lid_awake_should_hold(CLOSED_LID_AWAKE_NEVER, agents_active=True)
        )
        self.assertFalse(
            closed_lid_awake_should_hold(CLOSED_LID_AWAKE_AGENTS, agents_active=False)
        )
        self.assertTrue(
            closed_lid_awake_should_hold(CLOSED_LID_AWAKE_AGENTS, agents_active=True)
        )
        self.assertTrue(
            closed_lid_awake_should_hold(CLOSED_LID_AWAKE_ALWAYS, agents_active=False)
        )

    def test_lid_close_holds_final_led_state_only_when_sleep_is_allowed(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        self.assertTrue(
            status_bar.lid_closed_led_state_should_hold(
                lid_closed=True,
                sleep_prevention_policy=SLEEP_PREVENTION_AGENTS,
                agents_active=False,
            )
        )
        self.assertFalse(
            status_bar.lid_closed_led_state_should_hold(
                lid_closed=True,
                sleep_prevention_policy=SLEEP_PREVENTION_AGENTS,
                agents_active=True,
            )
        )
        self.assertFalse(
            status_bar.lid_closed_led_state_should_hold(
                lid_closed=False,
                sleep_prevention_policy=SLEEP_PREVENTION_AGENTS,
                agents_active=False,
            )
        )
        self.assertFalse(
            status_bar.lid_closed_led_state_should_hold(
                lid_closed=True,
                sleep_prevention_policy=SLEEP_PREVENTION_ALWAYS,
                agents_active=False,
            )
        )
        self.assertTrue(
            status_bar.lid_closed_led_state_should_hold(
                lid_closed=True,
                sleep_prevention_policy=SLEEP_PREVENTION_ALWAYS,
                agents_active=False,
                battery_safeguard_active=True,
            )
        )
        self.assertTrue(
            status_bar.lid_closed_led_state_should_hold(
                lid_closed=True,
                sleep_prevention_policy=SLEEP_PREVENTION_NEVER,
                agents_active=True,
            )
        )
        self.assertFalse(
            status_bar.lid_closed_led_state_should_hold(
                lid_closed=True,
                sleep_prevention_policy=SLEEP_PREVENTION_AGENTS,
                agents_active=None,
            )
        )

    def test_lid_close_animation_runs_only_when_sleep_prevention_is_inactive(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        self.assertFalse(
            status_bar.lid_close_animation_should_run(
                sleep_prevention_policy=SLEEP_PREVENTION_ALWAYS,
                agents_active=False,
            )
        )
        self.assertFalse(
            status_bar.lid_close_animation_should_run(
                sleep_prevention_policy=SLEEP_PREVENTION_AGENTS,
                agents_active=True,
            )
        )
        self.assertFalse(
            status_bar.lid_close_animation_should_run(
                sleep_prevention_policy=SLEEP_PREVENTION_AGENTS,
                agents_active=None,
            )
        )
        self.assertTrue(
            status_bar.lid_close_animation_should_run(
                sleep_prevention_policy=SLEEP_PREVENTION_AGENTS,
                agents_active=False,
            )
        )
        self.assertTrue(
            status_bar.lid_close_animation_should_run(
                sleep_prevention_policy=SLEEP_PREVENTION_NEVER,
                agents_active=True,
            )
        )

    def test_lid_poll_enables_hold_on_close_and_clears_it_on_open(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        animations = []
        controller = status_bar.StatusBarController.alloc().init()
        controller.lid_poll_in_flight = True
        controller.pending_lid_error = None
        controller.pending_lid_closed = True
        controller.last_lid_error = None
        controller.lid_poll_backoff_until_monotonic = 0.0
        controller.last_lid_closed = False
        controller.lid_closed_led_hold_active = False

        with (
            patch("sidepulse.status_bar.log_status_bar"),
            patch.object(
                status_bar.StatusBarController,
                "should_run_lid_close_animation",
                return_value=True,
            ),
            patch.object(
                status_bar.StatusBarController,
                "should_hold_lid_closed_led_state",
                return_value=True,
            ),
            patch.object(
                status_bar.StatusBarController,
                "play_lid_animation",
                side_effect=lambda kind: animations.append(kind),
            ),
        ):
            controller.handleLidPollResult_(None)
            self.assertTrue(controller.lid_closed_led_hold_active)
            self.assertEqual(animations, [LID_ANIMATION_CLOSED])

            controller.pending_lid_closed = False
            controller.handleLidPollResult_(None)

        self.assertFalse(controller.lid_closed_led_hold_active)
        self.assertEqual(animations, [LID_ANIMATION_CLOSED, LID_ANIMATION_OPEN])

    def test_lid_poll_skips_close_animation_while_sleep_prevention_is_active(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        controller = status_bar.StatusBarController.alloc().init()
        controller.lid_poll_in_flight = True
        controller.pending_lid_error = None
        controller.pending_lid_closed = True
        controller.last_lid_error = None
        controller.lid_poll_backoff_until_monotonic = 0.0
        controller.last_lid_closed = False
        controller.lid_closed_led_hold_active = True

        with (
            patch("sidepulse.status_bar.log_status_bar"),
            patch.object(
                status_bar.StatusBarController,
                "should_run_lid_close_animation",
                return_value=False,
            ),
            patch.object(
                status_bar.StatusBarController,
                "play_lid_animation",
            ) as play_lid_animation,
        ):
            controller.handleLidPollResult_(None)

        self.assertTrue(controller.last_lid_closed)
        self.assertFalse(controller.lid_closed_led_hold_active)
        play_lid_animation.assert_not_called()

    def test_lid_close_hold_suppresses_refresh_until_conditions_change(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        controller = status_bar.StatusBarController.alloc().init()
        controller.leds_enabled = True
        controller.lid_closed_led_hold_active = True
        controller.led_animation_until_monotonic = 0.0
        controller.led_sync_in_flight = False

        with (
            patch.object(controller, "sync_virtual_status_device"),
            patch.object(
                status_bar.StatusBarController,
                "should_hold_lid_closed_led_state",
                side_effect=[True, False],
            ),
            patch("sidepulse.status_bar.threading.Thread") as thread,
            patch("sidepulse.status_bar.log_status_bar"),
        ):
            controller.sync_leds(AgentMode.IDLE_READY, None, "agent")
            thread.assert_not_called()
            self.assertTrue(controller.lid_closed_led_hold_active)

            controller.sync_leds(AgentMode.WORKING, None, "agent")
            thread.assert_called_once()
            thread.return_value.start.assert_called_once()

        self.assertFalse(controller.lid_closed_led_hold_active)

    def test_status_bar_sleep_prevention_never_releases_all_sleep_prevention(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        fake = SimpleNamespace(
            settings=AgentMonitorSettings(
                sleep_prevention_policy=SLEEP_PREVENTION_NEVER,
            ),
            keep_awake=KeepAwakeController(process_factory=lambda *_args, **_kwargs: FakeProcess()),
            closed_lid_awake=ClosedLidAwakeController(
                process_factory=lambda *_args, **_kwargs: FakeProcess()
            ),
            last_keep_awake_error=None,
            last_closed_lid_awake_error=None,
            last_status_read_error=None,
            leds_enabled=False,
            agent_awake_last_mode=None,
            agent_awake_grace_until_monotonic=None,
            agent_awake_requested=False,
            battery_sleep_safeguard_active=False,
            battery_sleep_safeguard_reason="",
        )
        fake.update_agent_awake_request = (
            lambda mode: status_bar.StatusBarController.update_agent_awake_request(fake, mode)
        )
        fake.sync_closed_lid_awake = (
            lambda *, agents_active=None: status_bar.StatusBarController.sync_closed_lid_awake(
                fake,
                agents_active=agents_active,
            )
        )

        with patch("sidepulse.status_bar.sleep_helper_installed", return_value=False):
            status_bar.StatusBarController.sync_keep_awake(fake, AgentMode.WORKING)

        self.assertFalse(fake.keep_awake.process_running())
        self.assertFalse(fake.closed_lid_awake.process_running())
        self.assertTrue(fake.agent_awake_requested)

    def test_status_bar_sleep_prevention_always_holds_caffeinate_while_agents_idle(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        fake = SimpleNamespace(
            settings=AgentMonitorSettings(
                sleep_prevention_policy=SLEEP_PREVENTION_ALWAYS,
            ),
            keep_awake=KeepAwakeController(process_factory=lambda *_args, **_kwargs: FakeProcess()),
            closed_lid_awake=ClosedLidAwakeController(
                process_factory=lambda *_args, **_kwargs: FakeProcess()
            ),
            last_keep_awake_error=None,
            last_closed_lid_awake_error=None,
            last_status_read_error=None,
            leds_enabled=False,
            agent_awake_last_mode=None,
            agent_awake_grace_until_monotonic=None,
            agent_awake_requested=False,
            battery_sleep_safeguard_active=False,
            battery_sleep_safeguard_reason="",
        )
        fake.update_agent_awake_request = (
            lambda mode: status_bar.StatusBarController.update_agent_awake_request(fake, mode)
        )
        fake.sync_closed_lid_awake = (
            lambda *, agents_active=None: status_bar.StatusBarController.sync_closed_lid_awake(
                fake,
                agents_active=agents_active,
            )
        )

        with patch("sidepulse.status_bar.sleep_helper_installed", return_value=False):
            status_bar.StatusBarController.sync_keep_awake(fake, AgentMode.IDLE_READY)

        self.assertTrue(fake.keep_awake.process_running())
        self.assertTrue(fake.closed_lid_awake.process_running())
        self.assertFalse(fake.agent_awake_requested)

    def test_status_bar_sleep_prevention_agents_drives_both_paths(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        fake = SimpleNamespace(
            settings=AgentMonitorSettings(
                sleep_prevention_policy=SLEEP_PREVENTION_AGENTS,
            ),
            keep_awake=KeepAwakeController(process_factory=lambda *_args, **_kwargs: FakeProcess()),
            closed_lid_awake=ClosedLidAwakeController(
                process_factory=lambda *_args, **_kwargs: FakeProcess()
            ),
            last_keep_awake_error=None,
            last_closed_lid_awake_error=None,
            last_status_read_error=None,
            leds_enabled=False,
            agent_awake_last_mode=None,
            agent_awake_grace_until_monotonic=None,
            agent_awake_requested=False,
            battery_sleep_safeguard_active=False,
            battery_sleep_safeguard_reason="",
        )
        fake.update_agent_awake_request = (
            lambda mode: status_bar.StatusBarController.update_agent_awake_request(fake, mode)
        )
        fake.sync_closed_lid_awake = (
            lambda *, agents_active=None: status_bar.StatusBarController.sync_closed_lid_awake(
                fake,
                agents_active=agents_active,
            )
        )

        with patch("sidepulse.status_bar.sleep_helper_installed", return_value=False):
            status_bar.StatusBarController.sync_keep_awake(fake, AgentMode.WORKING)

        self.assertTrue(fake.keep_awake.process_running())
        self.assertTrue(fake.closed_lid_awake.process_running())

        fake.agent_awake_grace_until_monotonic = None
        with patch("sidepulse.status_bar.sleep_helper_installed", return_value=False):
            status_bar.StatusBarController.sync_keep_awake(fake, AgentMode.IDLE_READY)

        self.assertFalse(fake.keep_awake.process_running())
        self.assertFalse(fake.closed_lid_awake.process_running())

    def test_sleep_prevention_battery_safeguard_activates_only_on_battery(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        active, reason = status_bar.sleep_prevention_battery_safeguard(
            BatterySnapshot(percent=19, is_plugged=False),
            20,
        )
        plugged_active, plugged_reason = status_bar.sleep_prevention_battery_safeguard(
            BatterySnapshot(percent=19, is_plugged=True),
            20,
        )
        disabled_active, disabled_reason = status_bar.sleep_prevention_battery_safeguard(
            BatterySnapshot(percent=5, is_plugged=False),
            0,
        )

        self.assertTrue(active)
        self.assertEqual(reason, "battery 19%, threshold 20%")
        self.assertFalse(plugged_active)
        self.assertEqual(plugged_reason, "battery 19%, plugged in, threshold 20%")
        self.assertFalse(disabled_active)
        self.assertEqual(disabled_reason, "disabled")

    def test_status_bar_battery_safeguard_releases_all_sleep_prevention(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        fake = SimpleNamespace(
            settings=AgentMonitorSettings(
                sleep_prevention_policy=SLEEP_PREVENTION_ALWAYS,
                sleep_prevention_min_battery_percent=20,
            ),
            keep_awake=KeepAwakeController(process_factory=lambda *_args, **_kwargs: FakeProcess()),
            closed_lid_awake=ClosedLidAwakeController(
                process_factory=lambda *_args, **_kwargs: FakeProcess()
            ),
            last_keep_awake_error=None,
            last_closed_lid_awake_error=None,
            last_status_read_error=None,
            leds_enabled=False,
            agent_awake_last_mode=None,
            agent_awake_grace_until_monotonic=None,
            agent_awake_requested=False,
            battery_sleep_safeguard_active=False,
            battery_sleep_safeguard_reason="",
        )
        fake.update_agent_awake_request = (
            lambda mode: status_bar.StatusBarController.update_agent_awake_request(fake, mode)
        )
        fake.sync_closed_lid_awake = (
            lambda *, agents_active=None: status_bar.StatusBarController.sync_closed_lid_awake(
                fake,
                agents_active=agents_active,
            )
        )

        with patch("sidepulse.status_bar.sleep_helper_installed", return_value=False):
            status_bar.StatusBarController.sync_keep_awake(
                fake,
                AgentMode.WORKING,
                BatterySnapshot(percent=19, is_plugged=False),
            )

        self.assertFalse(fake.keep_awake.process_running())
        self.assertFalse(fake.closed_lid_awake.process_running())
        self.assertTrue(fake.battery_sleep_safeguard_active)
        self.assertEqual(
            fake.battery_sleep_safeguard_reason,
            "battery 19%, threshold 20%",
        )

    def test_closed_lid_awake_controller_sets_and_restores_system_disable(self) -> None:
        processes: list[FakeProcess] = []
        disabled_calls: list[bool] = []

        def factory(*_args, **_kwargs):
            process = FakeProcess()
            processes.append(process)
            return process

        controller = ClosedLidAwakeController(
            process_factory=factory,
            sleep_disabled_reader=lambda: False,
            sleep_disabled_setter=disabled_calls.append,
            use_system_disable=True,
        )

        self.assertTrue(
            controller.update(CLOSED_LID_AWAKE_ALWAYS, agents_active=False)
        )
        self.assertEqual(disabled_calls, [True])
        self.assertTrue(controller.changed_system_disable)
        self.assertEqual(len(processes), 1)

        controller.update(CLOSED_LID_AWAKE_ALWAYS, agents_active=False)
        self.assertEqual(disabled_calls, [True])

        self.assertFalse(
            controller.update(CLOSED_LID_AWAKE_NEVER, agents_active=False)
        )
        self.assertEqual(disabled_calls, [True, False])
        self.assertTrue(processes[0].terminated)

    def test_closed_lid_awake_controller_defaults_to_user_mode_only(self) -> None:
        disabled_calls: list[bool] = []
        controller = ClosedLidAwakeController(
            process_factory=lambda *_args, **_kwargs: FakeProcess(),
            sleep_disabled_reader=lambda: False,
            sleep_disabled_setter=disabled_calls.append,
        )

        self.assertTrue(
            controller.update(CLOSED_LID_AWAKE_ALWAYS, agents_active=False)
        )

        self.assertEqual(disabled_calls, [])
        self.assertTrue(controller.process_running())
        self.assertFalse(controller.changed_system_disable)

    def test_closed_lid_awake_controller_drives_existing_system_disable(self) -> None:
        disabled_calls: list[bool] = []
        controller = ClosedLidAwakeController(
            process_factory=lambda *_args, **_kwargs: FakeProcess(),
            sleep_disabled_reader=lambda: True,
            sleep_disabled_setter=disabled_calls.append,
            use_system_disable=True,
        )

        controller.update(CLOSED_LID_AWAKE_ALWAYS, agents_active=False)
        controller.update(CLOSED_LID_AWAKE_NEVER, agents_active=False)

        self.assertEqual(disabled_calls, [True, False])

    def test_closed_lid_awake_controller_clears_system_disable_once_when_idle(self) -> None:
        disabled_calls: list[bool] = []
        controller = ClosedLidAwakeController(
            process_factory=lambda *_args, **_kwargs: FakeProcess(),
            sleep_disabled_reader=lambda: True,
            sleep_disabled_setter=disabled_calls.append,
            use_system_disable=True,
        )

        self.assertFalse(
            controller.update(CLOSED_LID_AWAKE_NEVER, agents_active=False)
        )
        self.assertFalse(
            controller.update(CLOSED_LID_AWAKE_NEVER, agents_active=False)
        )

        self.assertEqual(disabled_calls, [False])

    def test_sleep_override_uses_noninteractive_sudo(self) -> None:
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, "", "")

        run_sudo_pmset_disablesleep(True, runner=runner)

        self.assertEqual(
            calls[0][0],
            ["/usr/bin/sudo", "-n", "/usr/bin/pmset", "-a", "disablesleep", "1"],
        )
        self.assertEqual(calls[0][1]["check"], False)

    def test_sleep_override_reports_missing_helper_without_prompting(self) -> None:
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(
                command,
                1,
                "",
                "sudo: a password is required",
            )

        with self.assertRaises(SleepHelperRequiredError) as ctx:
            run_sudo_pmset_disablesleep(False, runner=runner)

        self.assertIn("install-sleep-helper", str(ctx.exception))
        self.assertNotIn("/usr/bin/osascript", calls[0])

    def test_sleep_helper_sudoers_rule_is_narrow(self) -> None:
        self.assertEqual(
            sleep_helper_sudoers_rule("pero"),
            "pero ALL=(root) NOPASSWD: "
            "/usr/bin/pmset -a disablesleep 0, "
            "/usr/bin/pmset -a disablesleep 1\n",
        )
        with self.assertRaises(ValueError):
            sleep_helper_sudoers_rule("bad user")

    def test_lid_state_parser_reads_ioreg_booleans(self) -> None:
        self.assertTrue(
            parse_bool_ioreg_property('"AppleClamshellState" = Yes', "AppleClamshellState")
        )
        self.assertFalse(
            parse_bool_ioreg_property('"AppleClamshellState" = No', "AppleClamshellState")
        )
        self.assertTrue(parse_bool_ioreg_property('"SleepDisabled" = true', "SleepDisabled"))
        self.assertIsNone(parse_bool_ioreg_property('"Other" = Yes', "SleepDisabled"))

    def test_pmset_assertion_parser_reads_sleep_prevention(self) -> None:
        assertions = parse_pmset_assertions(
            """
            Assertion status system-wide:
               PreventUserIdleDisplaySleep    1
               PreventSystemSleep             0
               PreventUserIdleSystemSleep     1
               UserIsActive                   1
            """
        )

        self.assertTrue(assertions["PreventUserIdleDisplaySleep"])
        self.assertFalse(assertions["PreventSystemSleep"])
        self.assertTrue(assertions["PreventUserIdleSystemSleep"])
        self.assertTrue(assertions["UserIsActive"])

    def test_read_mac_sleep_snapshot_reads_sleep_disabled_from_ioreg(self) -> None:
        commands: list[tuple[str, ...]] = []

        def runner(command, **_kwargs):
            commands.append(tuple(command))
            if "ioreg" in command[0]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    '"SleepDisabled" = No\n',
                    "",
                )
            return subprocess.CompletedProcess(
                command,
                0,
                """
                   PreventSystemSleep             0
                   PreventUserIdleSystemSleep     1
                   PreventUserIdleDisplaySleep    0
                   UserIsActive                   1
                """,
                "",
            )

        snapshot = read_mac_sleep_snapshot(runner=runner)

        self.assertFalse(snapshot.sleep_disabled)
        self.assertFalse(snapshot.prevent_system_sleep)
        self.assertTrue(snapshot.prevent_user_idle_system_sleep)
        self.assertFalse(snapshot.prevent_user_idle_display_sleep)
        self.assertTrue(snapshot.user_is_active)
        self.assertTrue(snapshot.sleep_prevented)
        self.assertIsNone(snapshot.error)
        self.assertEqual(commands[0], IOREG_SLEEP_DISABLED_COMMAND)
        self.assertEqual(commands[1], PMSET_ASSERTIONS_COMMAND)
        self.assertNotIn(("/usr/bin/pmset", "-g"), commands)
        self.assertNotIn(("/usr/bin/pmset", "-g", "custom"), commands)

    def test_default_logs_use_sidepulse_xdg_state_dir(self) -> None:
        home = Path("/Users/example")

        self.assertEqual(
            default_state_dir(home),
            home / ".local" / "state" / "sidepulse" / "agent-monitor",
        )
        self.assertEqual(
            default_log_path("codex", home),
            home / ".local" / "state" / "sidepulse" / "agent-monitor" / "codex.jsonl",
        )

        with patch.dict(os.environ, {"XDG_STATE_HOME": "/tmp/xdg-state"}):
            self.assertEqual(
                default_state_dir(),
                Path("/tmp/xdg-state") / "sidepulse" / "agent-monitor",
            )

    def test_install_defaults_to_standard_state_log_path(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["install", "codex"])

        with patch.object(
            cli_module,
            "default_log_path",
            return_value=Path("/tmp/state/sidepulse/agent-monitor/codex.jsonl"),
        ):
            self.assertEqual(
                cli_module.install_log_path("codex", args),
                Path("/tmp/state/sidepulse/agent-monitor/codex.jsonl"),
            )

    def test_settings_use_xdg_config_dir_and_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_home = Path(tmp) / "xdg-config"
            settings_path = config_home / "sidepulse" / "agent-monitor" / "settings.json"

            with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(config_home)}):
                self.assertEqual(default_config_dir(), settings_path.parent)
                self.assertEqual(default_settings_path(), settings_path)

                saved = AgentMonitorSettings(
                    codex_transcripts_enabled=False,
                    claude_transcripts_enabled=True,
                )
                self.assertEqual(save_settings(saved), settings_path)
                self.assertEqual(load_settings(), saved)

    def test_settings_round_trip_agent_list_timing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings_path = Path(tmp) / "settings.json"
            settings = AgentMonitorSettings().with_agent_list_timing(
                recent_session_retention_seconds=36 * 60 * 60,
                idle_timeout_seconds=15 * 60,
            )

            save_settings(settings, settings_path)
            loaded = load_settings(settings_path)

            self.assertEqual(
                AgentMonitorSettings().recent_session_retention_seconds,
                DEFAULT_RECENT_SESSION_RETENTION_SECONDS,
            )
            self.assertEqual(
                AgentMonitorSettings().idle_timeout_seconds,
                DEFAULT_IDLE_TIMEOUT_SECONDS,
            )
            self.assertEqual(loaded.recent_session_retention_seconds, 36 * 60 * 60)
            self.assertEqual(loaded.idle_timeout_seconds, 15 * 60)

    def test_settings_round_trip_agent_animations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings_path = Path(tmp) / "settings.json"
            settings = AgentMonitorSettings()
            settings = settings.with_agent_animation(
                AgentMode.WORKING,
                AGENT_ANIMATION_KITT,
            )
            settings = settings.with_agent_animation(
                AgentMode.BLOCKED_ERROR,
                AGENT_ANIMATION_CUSTOM,
                custom_program="off\n#FF0000 pulse\nrepeat",
            )

            save_settings(settings, settings_path)
            loaded = load_settings(settings_path)

            saved_json = json.loads(settings_path.read_text())
            custom_metadata = next(
                iter(saved_json["custom_agent_animations"].values())
            )
            self.assertNotIn("program", custom_metadata)
            custom_path = settings_path.parent / "animations" / custom_metadata["file"]
            self.assertEqual(
                custom_path.read_text(),
                "off\n#FF0000 pulse\nrepeat\n",
            )

            self.assertEqual(
                loaded.agent_animation(AgentMode.WORKING).style,
                AGENT_ANIMATION_KITT,
            )
            self.assertEqual(
                loaded.agent_animation(AgentMode.BLOCKED_ERROR).custom_program,
                "off\n#FF0000 pulse\nrepeat",
            )
            self.assertEqual(
                loaded.agent_animation(AgentMode.COMPLETED).style,
                AGENT_ANIMATION_CYAN_COMPLETE,
            )

            custom_path.write_text("brightness 128\n#123456\n")
            reloaded = load_settings(settings_path)
            self.assertEqual(
                reloaded.agent_animation(AgentMode.BLOCKED_ERROR).custom_program,
                "brightness 128\n#123456",
            )

    def test_agent_animation_catalog_is_shared_by_every_state(self) -> None:
        settings = AgentMonitorSettings()
        animation_id = custom_agent_animation_id(
            "Purple Sweep",
            settings.custom_agent_animations,
        )
        settings = settings.with_custom_agent_animation(
            animation_id,
            name="Purple Sweep",
            program="off\n#AA44FF 700ms pulse\nrepeat",
        )

        expected = (
            AGENT_ANIMATION_SLOW_OFF,
            AGENT_ANIMATION_IMMEDIATE_OFF,
            AGENT_ANIMATION_IDLE_PULSE,
            AGENT_ANIMATION_CYAN_ROLL,
            AGENT_ANIMATION_CYAN_COMPLETE,
            AGENT_ANIMATION_AMBER_PULSE,
            AGENT_ANIMATION_SOLID_GREEN,
            AGENT_ANIMATION_KITT,
            AGENT_ANIMATION_KITT_RED,
            AGENT_ANIMATION_EMBER_IDLE,
            AGENT_ANIMATION_EMBER_TIDE,
            AGENT_ANIMATION_EMBER_ATTENTION,
            AGENT_ANIMATION_EMBER_COMPLETE,
            AGENT_ANIMATION_EMBER_LID_OPEN,
            AGENT_ANIMATION_PURPLE_IDLE,
            AGENT_ANIMATION_PURPLE_TIDE,
            AGENT_ANIMATION_PURPLE_ATTENTION,
            AGENT_ANIMATION_PURPLE_COMPLETE,
            AGENT_ANIMATION_PURPLE_LID_OPEN,
            AGENT_ANIMATION_NIGHT_RIDER,
            AGENT_ANIMATION_LID_OPEN,
            AGENT_ANIMATION_LID_CLOSED,
            animation_id,
        )
        self.assertEqual(settings.agent_animation_choices(), expected)
        for mode in ANIMATION_STATES:
            with self.subTest(mode=mode):
                selected = settings.with_agent_animation(mode, animation_id)
                self.assertEqual(selected.agent_animation_id(mode), animation_id)
                self.assertEqual(
                    selected.agent_animation(mode).custom_program,
                    "off\n#AA44FF 700ms pulse\nrepeat",
                )

    def test_working_tool_and_long_task_share_one_animation_setting(self) -> None:
        settings = AgentMonitorSettings().with_agent_animation(
            AgentMode.TOOL_RUNNING,
            AGENT_ANIMATION_KITT,
        )

        self.assertEqual(
            {
                settings.agent_animation_id(mode)
                for mode in AGENT_ANIMATION_WORKING_MODES
            },
            {AGENT_ANIMATION_KITT},
        )

    def test_animation_profile_round_trip_includes_every_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings_path = Path(tmp) / "settings.json"
            settings = AgentMonitorSettings()
            custom_id = custom_agent_animation_id(
                "Purple Sweep",
                settings.custom_agent_animations,
            )
            settings = settings.with_custom_agent_animation(
                custom_id,
                name="Purple Sweep",
                program="#AA44FF 700ms pulse\nrepeat",
            )
            for index, mode in enumerate(AgentMode):
                selection = custom_id if index % 2 else AGENT_ANIMATION_KITT
                settings = settings.with_agent_animation(mode, selection)
            profile_id = agent_animation_profile_id(
                "Focus",
                settings.agent_animation_profiles,
            )
            settings = settings.with_agent_animation_profile(
                profile_id,
                name="Focus",
            )
            save_settings(settings, settings_path)

            loaded = load_settings(settings_path)
            profile = loaded.agent_animation_profiles[profile_id]
            self.assertEqual(set(profile.animations), set(ANIMATION_STATES))
            reset = loaded
            for mode in AgentMode:
                reset = reset.with_agent_animation(mode, AGENT_ANIMATION_DEFAULT)
            applied = reset.with_applied_agent_animation_profile(profile_id)
            self.assertEqual(
                [applied.agent_animation_id(mode) for mode in AgentMode],
                [loaded.agent_animation_id(mode) for mode in AgentMode],
            )

    def test_cyan_is_the_default_builtin_profile(self) -> None:
        settings = AgentMonitorSettings()

        self.assertEqual(
            settings.matching_agent_animation_profile_id(),
            AGENT_ANIMATION_PROFILE_CYAN,
        )
        self.assertEqual(
            [settings.agent_animation_id(mode) for mode in ANIMATION_STATES],
            [default_agent_animation_id(mode) for mode in ANIMATION_STATES],
        )

        purple = settings.with_applied_agent_animation_profile(
            AGENT_ANIMATION_PROFILE_PURPLE
        )
        self.assertEqual(
            purple.matching_agent_animation_profile_id(),
            AGENT_ANIMATION_PROFILE_PURPLE,
        )
        self.assertEqual(
            purple.agent_animation_id(AgentMode.WORKING),
            AGENT_ANIMATION_PURPLE_TIDE,
        )
        self.assertEqual(
            purple.agent_animation_id(ANIMATION_STATE_LID_OPEN),
            AGENT_ANIMATION_PURPLE_LID_OPEN,
        )
        self.assertEqual(
            purple.agent_animation_id(ANIMATION_STATE_LID_CLOSED),
            AGENT_ANIMATION_LID_CLOSED,
        )

    def test_shipped_profile_json_matches_builtin_profiles(self) -> None:
        profiles = builtin_agent_animation_profiles()
        expected = {
            "cyan": AGENT_ANIMATION_PROFILE_CYAN,
            "ember": AGENT_ANIMATION_PROFILE_EMBER,
            "purple": AGENT_ANIMATION_PROFILE_PURPLE,
        }
        for file_stem, profile_id in expected.items():
            with self.subTest(profile=file_stem):
                document = json.loads(
                    (
                        Path(__file__).parent.parent
                        / "profiles"
                        / f"{file_stem}.json"
                    ).read_text()
                )
                profile = profiles[profile_id]
                self.assertEqual(document["name"], profile.name)
                self.assertEqual(document["animations"], profile.animations)
                self.assertEqual(set(profile.animations), set(ANIMATION_STATES))

    def test_builtin_profiles_keep_saved_animation_selections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings_path = Path(tmp) / "settings.json"
            settings_path.write_text(
                json.dumps(
                    {
                        "agent_animations": {
                            AgentMode.IDLE_READY.value: {
                                "style": AGENT_ANIMATION_KITT,
                                "custom_program": "",
                            }
                        },
                        "agent_animation_profiles": {
                            "profile:default": {
                                "name": "Default",
                                "animations": {
                                    mode: default_agent_animation_id(mode)
                                    for mode in ANIMATION_STATES
                                },
                            }
                        },
                    }
                )
            )

            loaded = load_settings(settings_path)

        self.assertEqual(
            loaded.agent_animation_id(AgentMode.IDLE_READY),
            AGENT_ANIMATION_KITT,
        )
        self.assertEqual(
            set(loaded.agent_animation_profiles),
            {
                AGENT_ANIMATION_PROFILE_CYAN,
                AGENT_ANIMATION_PROFILE_EMBER,
                AGENT_ANIMATION_PROFILE_PURPLE,
            },
        )

    def test_animation_profile_json_is_self_contained_and_importable(self) -> None:
        settings = AgentMonitorSettings()
        custom_id = custom_agent_animation_id(
            "Purple Sweep",
            settings.custom_agent_animations,
        )
        settings = settings.with_custom_agent_animation(
            custom_id,
            name="Purple Sweep",
            program="#AA44FF 700ms pulse\nrepeat",
        )
        for mode in AgentMode:
            settings = settings.with_agent_animation(mode, custom_id)

        document = agent_animation_profile_document(settings)

        self.assertEqual(document["format"], "sidepulse-animation-profile")
        self.assertEqual(document["version"], 1)
        self.assertEqual(document["name"], "Current")
        self.assertEqual(
            set(document["animations"]),
            set(ANIMATION_STATES),
        )
        self.assertEqual(
            document["custom_animations"][custom_id]["program"],
            "#AA44FF 700ms pulse\nrepeat",
        )

        imported, profile_id = with_imported_agent_animation_profile(
            AgentMonitorSettings(),
            json.loads(json.dumps(document)),
        )
        self.assertIn(profile_id, imported.agent_animation_profiles)
        for mode in AgentMode:
            self.assertEqual(imported.agent_animation_id(mode), custom_id)
            self.assertEqual(
                imported.agent_animation(mode).custom_program,
                "#AA44FF 700ms pulse\nrepeat",
            )

    def test_animation_profile_json_requires_all_agent_states(self) -> None:
        document = agent_animation_profile_document(AgentMonitorSettings())
        del document["animations"][AgentMode.UNKNOWN.value]

        with self.assertRaisesRegex(ValueError, "every agent state"):
            with_imported_agent_animation_profile(
                AgentMonitorSettings(),
                document,
            )

    def test_settings_do_not_migrate_pr_kitt_toggle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings_path = Path(tmp) / "settings.json"
            settings_path.write_text(json.dumps({"kitt_mode_enabled": True}))

            loaded = load_settings(settings_path)

            self.assertEqual(
                loaded.agent_animation(AgentMode.WORKING).style,
                AGENT_ANIMATION_CYAN_ROLL,
            )

    def test_settings_migrates_missing_agent_list_timing_to_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings_path = Path(tmp) / "settings.json"
            settings_path.write_text(json.dumps({"led_display": "agent"}))

            loaded = load_settings(settings_path)

            self.assertEqual(
                loaded.recent_session_retention_seconds,
                DEFAULT_RECENT_SESSION_RETENTION_SECONDS,
            )
            self.assertEqual(loaded.idle_timeout_seconds, DEFAULT_IDLE_TIMEOUT_SECONDS)

    def test_default_sources_respect_transcript_settings(self) -> None:
        settings = AgentMonitorSettings(
            codex_transcripts_enabled=False,
            claude_transcripts_enabled=True,
        )

        providers = [source.provider for source in default_sources(settings)]

        self.assertNotIn("codex-transcripts", providers)
        self.assertIn("claude-transcripts", providers)

    def test_default_sources_are_hook_only_by_default(self) -> None:
        providers = [source.provider for source in default_sources(AgentMonitorSettings())]

        self.assertIn("codex", providers)
        self.assertIn("claude", providers)
        self.assertIn("grok", providers)
        self.assertNotIn("codex-transcripts", providers)
        self.assertNotIn("claude-transcripts", providers)

    def test_settings_round_trip_remembered_device_display_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings_path = Path(tmp) / "settings.json"
            settings = AgentMonitorSettings(
                devices=(
                    DeviceDisplaySetting(
                        device_id="/Volumes/SidePulsePro",
                        name="SidePulse Pro",
                        path="/Volumes/SidePulsePro",
                        led_display="agent",
                    ),
                    DeviceDisplaySetting(
                        device_id="/Volumes/SidePulseDot",
                        name="SidePulse Dot",
                        path="/Volumes/SidePulseDot",
                        led_display="battery",
                        brightness=128,
                    ),
                    DeviceDisplaySetting(
                        device_id="/Volumes/SidePulseCustom",
                        name="SidePulse Custom",
                        path="/Volumes/SidePulseCustom",
                        led_display=LED_DISPLAY_CUSTOM,
                    ),
                )
            )

            save_settings(settings, settings_path)
            loaded = load_settings(settings_path)

            self.assertEqual(loaded.devices, settings.devices)
            self.assertEqual(loaded.display_for_device("/Volumes/SidePulsePro"), "agent")
            self.assertEqual(loaded.display_for_device("/Volumes/SidePulseDot"), "battery")
            self.assertEqual(
                loaded.display_for_device("/Volumes/SidePulseCustom"),
                LED_DISPLAY_CUSTOM,
            )
            self.assertEqual(loaded.brightness_for_device("/Volumes/SidePulseDot"), 128)

    def test_settings_round_trip_remembered_device_brightness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings_path = Path(tmp) / "settings.json"
            settings = AgentMonitorSettings().with_device_brightness(
                "/Volumes/SidePulseDot",
                96,
                name="SidePulse Dot",
                path="/Volumes/SidePulseDot",
            )

            save_settings(settings, settings_path)
            loaded = load_settings(settings_path)

            self.assertEqual(loaded.brightness_for_device("/Volumes/SidePulseDot"), 96)

    def test_settings_round_trip_session_open_preferences(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings_path = Path(tmp) / "settings.json"
            settings = AgentMonitorSettings().with_session_open_action(
                "Claude",
                SESSION_OPEN_TERMINAL,
                "Claude in VS Code",
            )

            save_settings(settings, settings_path)
            loaded = load_settings(settings_path)

            self.assertEqual(
                loaded.session_open_action("claude", "Claude in VS Code"),
                SESSION_OPEN_TERMINAL,
            )
            self.assertIsNone(loaded.session_open_action("claude"))

    def test_settings_round_trip_session_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings_path = Path(tmp) / "settings.json"
            settings = AgentMonitorSettings().with_session_terminal(
                TERMINAL_APP_CUSTOM,
                "/Applications/WezTerm.app",
            )

            save_settings(settings, settings_path)
            loaded = load_settings(settings_path)

            self.assertEqual(loaded.session_terminal_app, TERMINAL_APP_CUSTOM)
            self.assertEqual(loaded.custom_terminal_path, "/Applications/WezTerm.app")

            switched = loaded.with_session_terminal(TERMINAL_APP_ITERM)
            save_settings(switched, settings_path)
            reloaded = load_settings(settings_path)

            self.assertEqual(reloaded.session_terminal_app, TERMINAL_APP_ITERM)
            self.assertEqual(reloaded.custom_terminal_path, "/Applications/WezTerm.app")

    def test_settings_round_trip_grok_session_opening_is_split_from_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings_path = Path(tmp) / "settings.json"
            settings = (
                AgentMonitorSettings()
                .with_provider_session_open_action("grok", SESSION_OPEN_TERMINAL)
                .with_session_terminal(TERMINAL_APP_GHOSTTY)
            )

            save_settings(settings, settings_path)
            loaded = load_settings(settings_path)

            self.assertEqual(loaded.grok_session_open_action, SESSION_OPEN_TERMINAL)
            self.assertEqual(loaded.session_open_action("grok"), SESSION_OPEN_TERMINAL)
            self.assertEqual(loaded.session_terminal_app, TERMINAL_APP_GHOSTTY)
            self.assertNotIn("grok", loaded.session_open_preferences)

    def test_settings_migrates_legacy_grok_session_open_preference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings_path = Path(tmp) / "settings.json"
            settings_path.write_text(
                json.dumps(
                    {
                        "session_open_preferences": {"grok": SESSION_OPEN_TERMINAL},
                        "session_terminal": {"app": TERMINAL_APP_ITERM},
                    }
                )
            )

            loaded = load_settings(settings_path)

            self.assertEqual(loaded.grok_session_open_action, SESSION_OPEN_TERMINAL)
            self.assertEqual(loaded.session_terminal_app, TERMINAL_APP_ITERM)
            self.assertNotIn("grok", loaded.session_open_preferences)

    def test_settings_round_trip_sleep_prevention_policy_and_animations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings_path = Path(tmp) / "settings.json"
            settings = AgentMonitorSettings().with_sleep_prevention_policy(
                SLEEP_PREVENTION_ALWAYS
            )
            settings = settings.with_sleep_prevention_battery_safeguard(25)
            settings = settings.with_history_timeframe(HISTORY_TIMEFRAME_24H_SECONDS)
            settings = settings.with_lid_animation(
                LID_ANIMATION_CLOSED,
                program="off\n#FF3A00 200ms ease",
                duration_seconds=1.4,
            )
            settings = settings.with_lid_animation(
                LID_ANIMATION_OPEN,
                program="off\n#00FF66 200ms ease",
                duration_seconds=1.6,
            )

            save_settings(settings, settings_path)
            loaded = load_settings(settings_path)

            self.assertEqual(loaded.sleep_prevention_policy, SLEEP_PREVENTION_ALWAYS)
            self.assertEqual(loaded.sleep_prevention_min_battery_percent, 25)
            self.assertEqual(loaded.history_timeframe_seconds, HISTORY_TIMEFRAME_24H_SECONDS)
            self.assertEqual(
                loaded.lid_animation(LID_ANIMATION_CLOSED).program,
                "off\n#FF3A00 200ms ease",
            )
            self.assertEqual(
                loaded.lid_animation(LID_ANIMATION_OPEN).duration_seconds,
                1.6,
            )

            enabled = settings.with_closed_lid_system_override(True)
            save_settings(enabled, settings_path)
            loaded_enabled = load_settings(settings_path)
            self.assertTrue(loaded_enabled.closed_lid_system_override_enabled)

            completed = settings.with_setup_screen_completed(True)
            save_settings(completed, settings_path)
            loaded_completed = load_settings(settings_path)
            self.assertTrue(loaded_completed.setup_screen_completed)

    def test_settings_migrate_missing_lid_fields_to_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings_path = Path(tmp) / "settings.json"
            settings_path.write_text(json.dumps({"led_display": "agent"}))

            loaded = load_settings(settings_path)

            self.assertEqual(loaded.sleep_prevention_policy, SLEEP_PREVENTION_AGENTS)
            self.assertEqual(loaded.sleep_prevention_min_battery_percent, 20)
            self.assertEqual(loaded.history_timeframe_seconds, DEFAULT_HISTORY_TIMEFRAME_SECONDS)
            self.assertFalse(loaded.closed_lid_system_override_enabled)
            self.assertFalse(loaded.setup_screen_completed)
            self.assertEqual(
                loaded.lid_animation(LID_ANIMATION_CLOSED),
                default_lid_animation(LID_ANIMATION_CLOSED),
            )

    def test_settings_remember_device_preserves_existing_display_choice(self) -> None:
        settings = AgentMonitorSettings().with_device_display(
            "/Volumes/SidePulseDot",
            "battery",
            name="SidePulse Dot",
            path="/Volumes/SidePulseDot",
        )

        remembered = settings.with_remembered_device(
            device_id="/Volumes/SidePulseDot",
            name="SidePulse Dot",
            path="/Volumes/SidePulseDot",
        )

        self.assertEqual(remembered.display_for_device("/Volumes/SidePulseDot"), "battery")
        self.assertEqual(remembered.brightness_for_device("/Volumes/SidePulseDot"), 255)

    def test_settings_remove_remembered_device(self) -> None:
        settings = AgentMonitorSettings(
            devices=(
                DeviceDisplaySetting(
                    device_id="/Volumes/SidePulsePro",
                    name="SidePulse Pro",
                    path="/Volumes/SidePulsePro",
                    led_display="agent",
                ),
                DeviceDisplaySetting(
                    device_id="/Volumes/SidePulseDot",
                    name="SidePulse Dot",
                    path="/Volumes/SidePulseDot",
                    led_display="battery",
                ),
            )
        )

        updated = settings.without_device("/Volumes/SidePulseDot")

        self.assertEqual([device.device_id for device in updated.devices], ["/Volumes/SidePulsePro"])
        self.assertEqual(updated.display_for_device("/Volumes/SidePulseDot"), "agent")

    def test_disconnected_device_menu_has_remove_option(self) -> None:
        try:
            from sidepulse import status_bar
        except SystemExit as exc:
            self.skipTest(str(exc))

        device = status_bar.StatusBarDevice(
            device_id="/Volumes/SidePulsePro",
            name="SidePulse Pro",
            root=Path("/Volumes/SidePulsePro"),
            target=Path("/Volumes/SidePulsePro/LEDS.LED"),
            connected=False,
            display="agent",
        )
        item = status_bar.build_device_menu_item(device, None)
        submenu = item.submenu()
        titles = [
            submenu.itemAtIndex_(index).title()
            for index in range(submenu.numberOfItems())
            if submenu.itemAtIndex_(index).title()
        ]

        self.assertIn("Not connected", titles)
        self.assertIn("Remove", titles)

    def test_status_bar_launch_agent_plist_runs_foreground_command(self) -> None:
        launcher = Path("/tmp/SidePulse Status Bar")
        plist = build_launch_agent_plist(
            python_executable="/usr/bin/python3",
            launcher_path=launcher,
            stdout_path=Path("/tmp/sidepulse.out.log"),
            stderr_path=Path("/tmp/sidepulse.err.log"),
        )

        self.assertEqual(plist["Label"], LAUNCH_AGENT_LABEL)
        self.assertEqual(
            plist["ProgramArguments"],
            [str(launcher)],
        )
        self.assertTrue(plist["RunAtLoad"])
        self.assertEqual(plist["StandardOutPath"], "/tmp/sidepulse.out.log")
        self.assertEqual(plist["StandardErrorPath"], "/tmp/sidepulse.err.log")
        self.assertNotIn("KeepAlive", plist)

    def test_status_bar_launcher_uses_background_item_name(self) -> None:
        script = build_status_bar_launcher_script(python_executable="/usr/bin/python3")

        self.assertEqual(STATUS_BAR_DISPLAY_NAME, "SidePulse Status Bar")
        self.assertIn("exec /usr/bin/python3 -m sidepulse status-bar --foreground", script)

    def test_status_bar_launch_agent_installed_checks_plist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plist = Path(tmp) / "io.sidepulse.agentstatus.plist"

            self.assertFalse(launch_agent_installed(plist))
            plist.write_bytes(b"plist")
            self.assertTrue(launch_agent_installed(plist))

    def test_frozen_status_bar_launch_agent_uses_sidepulse_executable(self) -> None:
        with patch("sidepulse.status_bar_launch.sys.frozen", True, create=True):
            script = build_status_bar_launcher_script()

        self.assertIn("status-bar start --foreground", script)

    def test_frozen_hook_command_uses_internal_cli(self) -> None:
        with patch("sidepulse.install.sys.frozen", True, create=True):
            command = hook_command("codex", Path("/tmp/codex events.jsonl"))

        self.assertEqual(
            command,
            f"{sys.executable} agent-monitor hook-log --provider codex "
            "--log '/tmp/codex events.jsonl' ; true",
        )

    def test_hook_command_is_fail_open(self) -> None:
        command = hook_command("grok", Path("/tmp/grok.jsonl"), python_executable="python3")

        self.assertTrue(command.endswith("; true"))

    def test_legacy_hook_entry_points_still_log_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            log = base / "grok.jsonl"
            payload = b'{"hookEventName":"pre_tool_use","sessionId":"legacy"}'
            root = Path(__file__).resolve().parents[1]
            for script in (
                root / "src" / "agent_monitor" / "hook_entry.py",
                root / "src" / "sidepulse_cli" / "hook_entry.py",
            ):
                result = subprocess.run(
                    [
                        sys.executable,
                        str(script),
                        "--provider",
                        "grok",
                        "--log",
                        str(log),
                    ],
                    input=payload,
                    env={**os.environ, "SIDEPULSE_DISABLE_EVENT_SOCKET": "1"},
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr.decode())

            lines = log.read_text().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertTrue(all(json.loads(line)["sessionId"] == "legacy" for line in lines))

    def test_status_bar_install_removes_legacy_com_sidepulse_plist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            target = base / "io.sidepulse.agentstatus.plist"
            legacy = base / "com.sidepulse.agentstatus.plist"
            pixiepulse_legacy = base / "com.pixiepulse.agentstatus.plist"
            launcher = base / "SidePulse Status Bar"
            legacy.write_bytes(b"old")
            pixiepulse_legacy.write_bytes(b"old")

            with (
                patch("sidepulse.status_bar_launch.default_state_dir", return_value=base / "state"),
                patch("sidepulse.status_bar_launch.subprocess.run") as run,
            ):
                result = install_launch_agent(
                    start=False,
                    plist_path=target,
                    legacy_plist_path=legacy,
                    pixiepulse_legacy_plist_path=pixiepulse_legacy,
                    launcher_path=launcher,
                    python_executable="/usr/bin/python3",
                )

            self.assertTrue(result.changed)
            self.assertTrue(target.exists())
            self.assertTrue(launcher.exists())
            self.assertEqual(plistlib.loads(target.read_bytes())["ProgramArguments"], [str(launcher)])
            self.assertFalse(legacy.exists())
            self.assertFalse(pixiepulse_legacy.exists())
            self.assertEqual(run.call_count, 2)
            self.assertEqual(run.call_args_list[0].args[0][0:2], ["launchctl", "bootout"])
            self.assertEqual(run.call_args_list[1].args[0][0:2], ["launchctl", "bootout"])

    def test_sd_eject_guard_plist_shapes_for_user_and_system_scopes(self) -> None:
        for scope in ("user", "system"):
            paths = SdEjectGuardPaths(
                scope=scope,
                plist_path=Path(f"/tmp/{scope}/io.sidepulse.sdejectguard.plist"),
                binary_path=Path(f"/tmp/{scope}/sd_eject_guard"),
                stdout_path=Path(f"/tmp/{scope}/sd-eject-guard.out.log"),
                stderr_path=Path(f"/tmp/{scope}/sd-eject-guard.err.log"),
            )

            plist = build_sd_eject_guard_plist(paths)

            self.assertEqual(plist["Label"], SD_EJECT_GUARD_LABEL)
            self.assertEqual(plist["ProgramArguments"], [str(paths.binary_path)])
            self.assertTrue(plist["RunAtLoad"])
            self.assertTrue(plist["KeepAlive"])
            self.assertEqual(plist["StandardOutPath"], str(paths.stdout_path))
            self.assertEqual(plist["StandardErrorPath"], str(paths.stderr_path))

    def test_sd_eject_guard_default_binary_uses_background_item_name(self) -> None:
        self.assertEqual(SD_EJECT_GUARD_BINARY_NAME, SD_EJECT_GUARD_DISPLAY_NAME)

    def test_sd_eject_guard_log_cleaner_truncates_files_over_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "sd-eject-guard.out.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("wb") as handle:
                handle.seek(SD_EJECT_GUARD_LOG_MAX_BYTES)
                handle.write(b"x")

            self.assertTrue(truncate_log_if_large(log))
            self.assertEqual(log.stat().st_size, 0)

            log.write_text("prevented eject of disk4s1\n", encoding="utf-8")
            self.assertFalse(truncate_log_if_large(log))
            self.assertEqual(read_log_tail(log, 5), "prevented eject of disk4s1")

    def test_sd_eject_guard_installed_checks_user_and_system_plists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            user_paths = SdEjectGuardPaths(
                scope="user",
                plist_path=base / "user" / "io.sidepulse.sdejectguard.plist",
                binary_path=base / "user" / "sd_eject_guard",
                stdout_path=base / "user" / "out.log",
                stderr_path=base / "user" / "err.log",
            )
            system_paths = SdEjectGuardPaths(
                scope="system",
                plist_path=base / "system" / "io.sidepulse.sdejectguard.plist",
                binary_path=base / "system" / "sd_eject_guard",
                stdout_path=base / "system" / "out.log",
                stderr_path=base / "system" / "err.log",
            )

            self.assertFalse(
                sd_eject_guard_installed(
                    user_paths=user_paths,
                    system_paths=system_paths,
                )
            )
            user_paths.plist_path.parent.mkdir(parents=True)
            user_paths.plist_path.write_bytes(b"plist")

            self.assertTrue(
                sd_eject_guard_installed(
                    user_paths=user_paths,
                    system_paths=system_paths,
                )
            )
            self.assertTrue(
                sd_eject_guard_installed(
                    "user",
                    user_paths=user_paths,
                    system_paths=system_paths,
                )
            )
            self.assertFalse(
                sd_eject_guard_installed(
                    "system",
                    user_paths=user_paths,
                    system_paths=system_paths,
                )
            )

    def test_sd_eject_guard_auto_falls_back_to_user_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "sd_eject_guard.c"
            source.write_text("int main(void) { return 0; }\n")
            user_paths = SdEjectGuardPaths(
                scope="user",
                plist_path=base / "user" / "io.sidepulse.sdejectguard.plist",
                binary_path=base / "user" / "sd_eject_guard",
                stdout_path=base / "user" / "out.log",
                stderr_path=base / "user" / "err.log",
            )
            system_paths = SdEjectGuardPaths(
                scope="system",
                plist_path=base / "system" / "io.sidepulse.sdejectguard.plist",
                binary_path=base / "system" / "sd_eject_guard",
                stdout_path=base / "system" / "out.log",
                stderr_path=base / "system" / "err.log",
            )
            calls = []

            def fake_run(command, *args, **kwargs):
                calls.append(command)
                if command[0] == "clang":
                    Path(command[3]).write_bytes(b"binary")
                return subprocess.CompletedProcess(command, 0)

            with (
                patch("sidepulse.sd_eject_guard_launch.os.geteuid", return_value=501),
                patch("sidepulse.sd_eject_guard_launch.os.getuid", return_value=501),
                patch("sidepulse.sd_eject_guard_launch.subprocess.run", side_effect=fake_run),
            ):
                result = install_sd_eject_guard(
                    scope="auto",
                    source_path=source,
                    user_paths=user_paths,
                    system_paths=system_paths,
                )

            self.assertEqual(result.scope, "user")
            self.assertTrue(result.compiled)
            self.assertTrue(result.started)
            self.assertTrue(user_paths.binary_path.exists())
            self.assertTrue(user_paths.plist_path.exists())
            self.assertEqual(calls[0][0:4], ["clang", "-O2", "-o", str(user_paths.binary_path.with_name("sd_eject_guard.tmp"))])
            self.assertIn("-framework", calls[0])
            self.assertIn(["launchctl", "bootstrap", "gui/501", str(user_paths.plist_path)], calls)

    def test_sd_eject_guard_install_removes_legacy_binary_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "sd_eject_guard.c"
            source.write_text("int main(void) { return 0; }\n")
            user_paths = SdEjectGuardPaths(
                scope="user",
                plist_path=base / "user" / "io.sidepulse.sdejectguard.plist",
                binary_path=base / "user" / SD_EJECT_GUARD_BINARY_NAME,
                stdout_path=base / "user" / "out.log",
                stderr_path=base / "user" / "err.log",
            )
            system_paths = SdEjectGuardPaths(
                scope="system",
                plist_path=base / "system" / "io.sidepulse.sdejectguard.plist",
                binary_path=base / "system" / SD_EJECT_GUARD_BINARY_NAME,
                stdout_path=base / "system" / "out.log",
                stderr_path=base / "system" / "err.log",
            )
            legacy = user_paths.binary_path.with_name("sd_eject_guard")
            legacy.parent.mkdir(parents=True)
            legacy.write_bytes(b"legacy")

            def fake_run(command, *args, **kwargs):
                if command[0] == "clang":
                    Path(command[3]).write_bytes(b"binary")
                return subprocess.CompletedProcess(command, 0)

            with (
                patch("sidepulse.sd_eject_guard_launch.os.geteuid", return_value=501),
                patch("sidepulse.sd_eject_guard_launch.os.getuid", return_value=501),
                patch("sidepulse.sd_eject_guard_launch.subprocess.run", side_effect=fake_run),
            ):
                result = install_sd_eject_guard(
                    scope="user",
                    source_path=source,
                    user_paths=user_paths,
                    system_paths=system_paths,
                )

            self.assertTrue(user_paths.binary_path.exists())
            self.assertFalse(legacy.exists())
            self.assertEqual(result.legacy_removed, (legacy,))

    def test_sd_eject_guard_user_install_reports_skipped_system_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "sd_eject_guard.c"
            source.write_text("int main(void) { return 0; }\n")
            user_paths = SdEjectGuardPaths(
                scope="user",
                plist_path=base / "user" / "io.sidepulse.sdejectguard.plist",
                binary_path=base / "user" / "sd_eject_guard",
                stdout_path=base / "user" / "out.log",
                stderr_path=base / "user" / "err.log",
            )
            system_paths = SdEjectGuardPaths(
                scope="system",
                plist_path=base / "system" / "io.sidepulse.sdejectguard.plist",
                binary_path=base / "system" / "sd_eject_guard",
                stdout_path=base / "system" / "out.log",
                stderr_path=base / "system" / "err.log",
            )
            system_paths.plist_path.parent.mkdir(parents=True)
            system_paths.plist_path.write_bytes(b"old")

            def fake_run(command, *args, **kwargs):
                if command[0] == "clang":
                    Path(command[3]).write_bytes(b"binary")
                return subprocess.CompletedProcess(command, 0)

            with (
                patch("sidepulse.sd_eject_guard_launch.os.geteuid", return_value=501),
                patch("sidepulse.sd_eject_guard_launch.os.getuid", return_value=501),
                patch("sidepulse.sd_eject_guard_launch.subprocess.run", side_effect=fake_run),
            ):
                result = install_sd_eject_guard(
                    scope="user",
                    source_path=source,
                    user_paths=user_paths,
                    system_paths=system_paths,
                )

            self.assertIsNone(result.cleanup_removed)
            self.assertIn(str(system_paths.plist_path), result.cleanup_skipped or "")
            self.assertTrue(system_paths.plist_path.exists())

    def test_sd_eject_guard_stop_auto_stops_user_and_skips_system_without_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            user_paths = SdEjectGuardPaths(
                scope="user",
                plist_path=base / "user" / "io.sidepulse.sdejectguard.plist",
                binary_path=base / "user" / "sd_eject_guard",
                stdout_path=base / "user" / "out.log",
                stderr_path=base / "user" / "err.log",
            )
            system_paths = SdEjectGuardPaths(
                scope="system",
                plist_path=base / "system" / "io.sidepulse.sdejectguard.plist",
                binary_path=base / "system" / "sd_eject_guard",
                stdout_path=base / "system" / "out.log",
                stderr_path=base / "system" / "err.log",
            )
            user_paths.plist_path.parent.mkdir(parents=True)
            system_paths.plist_path.parent.mkdir(parents=True)
            user_paths.plist_path.write_bytes(b"user")
            system_paths.plist_path.write_bytes(b"system")

            with (
                patch("sidepulse.sd_eject_guard_launch.os.geteuid", return_value=501),
                patch("sidepulse.sd_eject_guard_launch.os.getuid", return_value=501),
                patch("sidepulse.sd_eject_guard_launch.subprocess.run") as run,
            ):
                results = stop_sd_eject_guard(
                    scope="auto",
                    user_paths=user_paths,
                    system_paths=system_paths,
                )

            self.assertEqual(len(results), 2)
            self.assertTrue(results[0].stopped)
            self.assertFalse(results[1].stopped)
            self.assertIn("missing permissions", results[1].skipped or "")
            run.assert_called_once()
            self.assertEqual(run.call_args.args[0][0:3], ["launchctl", "bootout", "gui/501"])

    def test_sd_eject_guard_uninstall_removes_plist_binary_and_legacy_binary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            paths = SdEjectGuardPaths(
                scope="user",
                plist_path=base / "user" / "io.sidepulse.sdejectguard.plist",
                binary_path=base / "user" / SD_EJECT_GUARD_BINARY_NAME,
                stdout_path=base / "user" / "out.log",
                stderr_path=base / "user" / "err.log",
            )
            system_paths = SdEjectGuardPaths(
                scope="system",
                plist_path=base / "system" / "io.sidepulse.sdejectguard.plist",
                binary_path=base / "system" / SD_EJECT_GUARD_BINARY_NAME,
                stdout_path=base / "system" / "out.log",
                stderr_path=base / "system" / "err.log",
            )
            legacy = paths.binary_path.with_name("sd_eject_guard")
            paths.plist_path.parent.mkdir(parents=True)
            paths.plist_path.write_bytes(b"plist")
            paths.binary_path.write_bytes(b"binary")
            legacy.write_bytes(b"legacy")

            with (
                patch("sidepulse.sd_eject_guard_launch.os.geteuid", return_value=501),
                patch("sidepulse.sd_eject_guard_launch.os.getuid", return_value=501),
                patch("sidepulse.sd_eject_guard_launch.subprocess.run") as run,
            ):
                results = uninstall_sd_eject_guard(
                    scope="user",
                    user_paths=paths,
                    system_paths=system_paths,
                )

            self.assertEqual(len(results), 1)
            self.assertTrue(results[0].stopped)
            self.assertEqual(
                set(results[0].removed_paths),
                {paths.plist_path, paths.binary_path, legacy},
            )
            self.assertFalse(paths.plist_path.exists())
            self.assertFalse(paths.binary_path.exists())
            self.assertFalse(legacy.exists())
            run.assert_called_once()
            self.assertEqual(run.call_args.args[0][0:3], ["launchctl", "bootout", "gui/501"])

    def test_sd_eject_guard_system_scope_requires_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "sd_eject_guard.c"
            source.write_text("int main(void) { return 0; }\n")
            system_paths = SdEjectGuardPaths(
                scope="system",
                plist_path=base / "system" / "io.sidepulse.sdejectguard.plist",
                binary_path=base / "system" / "sd_eject_guard",
                stdout_path=base / "system" / "out.log",
                stderr_path=base / "system" / "err.log",
            )

            with patch("sidepulse.sd_eject_guard_launch.os.geteuid", return_value=501):
                with self.assertRaisesRegex(SdEjectGuardInstallError, "requires root"):
                    install_sd_eject_guard(
                        scope="system",
                        source_path=source,
                        system_paths=system_paths,
                    )

    def test_sd_eject_guard_system_install_cleans_user_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "sd_eject_guard.c"
            source.write_text("int main(void) { return 0; }\n")
            user_paths = SdEjectGuardPaths(
                scope="user",
                plist_path=base / "user" / "io.sidepulse.sdejectguard.plist",
                binary_path=base / "user" / "sd_eject_guard",
                stdout_path=base / "user" / "out.log",
                stderr_path=base / "user" / "err.log",
            )
            system_paths = SdEjectGuardPaths(
                scope="system",
                plist_path=base / "system" / "io.sidepulse.sdejectguard.plist",
                binary_path=base / "system" / "sd_eject_guard",
                stdout_path=base / "system" / "out.log",
                stderr_path=base / "system" / "err.log",
            )
            user_paths.plist_path.parent.mkdir(parents=True)
            user_paths.plist_path.write_bytes(b"old")
            calls = []

            def fake_run(command, *args, **kwargs):
                calls.append(command)
                if command[0] == "clang":
                    Path(command[3]).write_bytes(b"binary")
                return subprocess.CompletedProcess(command, 0)

            with (
                patch("sidepulse.sd_eject_guard_launch.os.geteuid", return_value=0),
                patch("sidepulse.sd_eject_guard_launch.os.getuid", return_value=501),
                patch("sidepulse.sd_eject_guard_launch.os.chown") as chown,
                patch("sidepulse.sd_eject_guard_launch.subprocess.run", side_effect=fake_run),
            ):
                result = install_sd_eject_guard(
                    scope="system",
                    source_path=source,
                    user_paths=user_paths,
                    system_paths=system_paths,
                )

            self.assertEqual(result.scope, "system")
            self.assertEqual(result.cleanup_removed, user_paths.plist_path)
            self.assertFalse(user_paths.plist_path.exists())
            self.assertTrue(system_paths.plist_path.exists())
            self.assertIn(["launchctl", "bootstrap", "system", str(system_paths.plist_path)], calls)
            chown.assert_any_call(system_paths.binary_path, 0, 0)
            chown.assert_any_call(system_paths.plist_path, 0, 0)

    def test_watch_filters_to_recent_statuses(self) -> None:
        now = datetime.now(timezone.utc)
        recent = AgentStatus(
            provider="codex",
            agent_id="recent",
            display_name="Recent",
            mode=AgentMode.WORKING,
            updated_at=now - timedelta(seconds=20),
            event_name="PostToolUse",
        )
        older = AgentStatus(
            provider="claude",
            agent_id="older",
            display_name="Older",
            mode=AgentMode.COMPLETED,
            updated_at=now - timedelta(seconds=600),
            event_name="Stop",
        )
        snapshot = MonitorSnapshot(
            aggregate=AggregateStatus(AgentMode.WORKING, 2, 0, recent),
            statuses=(recent, older),
            stale_statuses=(),
            sources=(),
            collected_at=now,
        )

        visible = visible_watch_statuses(snapshot, recent_seconds=120, include_stale=False)

        self.assertEqual([status.agent_id for status in visible], ["recent"])

    def test_orphaned_tool_running_expires_before_session_stale_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "codex.jsonl"
            old = datetime.now(timezone.utc) - timedelta(seconds=180)
            log.write_text(
                json.dumps(
                    {
                        "logged_at": old.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "event": {
                            "hook_event_name": "PreToolUse",
                            "session_id": "codex-session",
                            "tool_name": "Bash",
                        },
                    }
                )
                + "\n"
            )

            monitor = AgentMonitor(
                sources=(SourceSpec("codex", log),),
                stale_after_seconds=300,
                tool_running_timeout_seconds=120,
            )
            snapshot = monitor.snapshot()

            self.assertEqual(snapshot.aggregate.mode, AgentMode.IDLE_READY)
            self.assertEqual(snapshot.statuses, ())
            self.assertEqual(len(snapshot.stale_statuses), 1)
            self.assertEqual(snapshot.stale_statuses[0].mode, AgentMode.TOOL_RUNNING)

    def test_completed_status_expires_before_session_stale_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "codex.jsonl"
            old = datetime.now(timezone.utc) - timedelta(seconds=60)
            log.write_text(
                json.dumps(
                    {
                        "logged_at": old.isoformat(),
                        "event": {
                            "hook_event_name": "Stop",
                            "session_id": "codex-session",
                            "last_assistant_message": "Done.",
                        },
                    }
                )
                + "\n"
            )

            monitor = AgentMonitor(
                sources=(SourceSpec("codex", log),),
                stale_after_seconds=3600,
                completed_visible_seconds=15,
            )
            snapshot = monitor.snapshot()

            self.assertEqual(snapshot.aggregate.mode, AgentMode.IDLE_READY)
            self.assertEqual(snapshot.statuses, ())
            self.assertEqual(len(snapshot.stale_statuses), 1)
            self.assertEqual(snapshot.stale_statuses[0].mode, AgentMode.COMPLETED)

    def test_completed_status_stays_visible_for_twenty_minutes_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "codex.jsonl"
            recent_done = datetime.now(timezone.utc) - timedelta(minutes=19)
            log.write_text(
                json.dumps(
                    {
                        "logged_at": recent_done.isoformat(),
                        "event": {
                            "hook_event_name": "Stop",
                            "session_id": "codex-session",
                            "last_assistant_message": "Done.",
                        },
                    }
                )
                + "\n"
            )

            monitor = AgentMonitor(
                sources=(SourceSpec("codex", log),),
                stale_after_seconds=3600,
            )
            snapshot = monitor.snapshot()

            self.assertEqual(snapshot.aggregate.mode, AgentMode.COMPLETED)
            self.assertEqual(len(snapshot.statuses), 1)

    def test_completed_status_is_hidden_when_active_work_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "codex.jsonl"
            now = datetime.now(timezone.utc).isoformat()
            log.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "logged_at": now,
                                "event": {
                                    "hook_event_name": "Stop",
                                    "session_id": "done-session",
                                    "last_assistant_message": "Done.",
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "logged_at": now,
                                "event": {
                                    "hook_event_name": "PreToolUse",
                                    "session_id": "working-session",
                                    "tool_name": "Bash",
                                },
                            }
                        ),
                    ]
                )
                + "\n"
            )

            monitor = AgentMonitor(
                sources=(SourceSpec("codex", log),),
                stale_after_seconds=3600,
                completed_visible_seconds=15,
            )
            snapshot = monitor.snapshot()

            self.assertEqual(snapshot.aggregate.active_count, 1)
            self.assertEqual([status.session_id for status in snapshot.statuses], ["working-session"])
            self.assertEqual(snapshot.stale_statuses[0].session_id, "done-session")

    def test_idle_notification_does_not_resurrect_completed_claude_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "claude.jsonl"
            old = datetime.now(timezone.utc) - timedelta(minutes=25)
            log.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "logged_at": old.isoformat(),
                                "hook_event_name": "Stop",
                                "session_id": "claude-session",
                                "cwd": "/tmp/project",
                                "last_assistant_message": "Done and verified.",
                                "background_tasks": [],
                                "session_crons": [],
                            }
                        ),
                        json.dumps(
                            {
                                "logged_at": (old + timedelta(seconds=60)).isoformat(),
                                "hook_event_name": "Notification",
                                "session_id": "claude-session",
                                "cwd": "/tmp/project",
                                "notification_type": "idle_prompt",
                                "message": "Claude is waiting for your input",
                            }
                        ),
                    ]
                )
                + "\n"
            )

            monitor = AgentMonitor(
                sources=(SourceSpec("claude", log),),
                stale_after_seconds=3600,
            )
            snapshot = monitor.snapshot()

            self.assertEqual(snapshot.aggregate.mode, AgentMode.IDLE_READY)
            self.assertEqual(snapshot.statuses, ())
            self.assertEqual(snapshot.stale_statuses[0].mode, AgentMode.COMPLETED)

    def test_codex_permission_request_stays_ask_during_unrelated_tool_activity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "codex.jsonl"
            now = datetime.now(timezone.utc)
            session_id = "019f179b-7fdc-7eb0-a3af-1ca3eb128eee"
            server_command = ".venv/bin/bambucuts server --host 127.0.0.1 --port 5425"
            curl_command = "curl -s http://127.0.0.1:5425/api/status | head -c 1000"
            events = [
                {
                    "logged_at": now.isoformat(),
                    "event": {
                        "hook_event_name": "PreToolUse",
                        "session_id": session_id,
                        "cwd": "/Users/pero/pgit/a1plotter",
                        "tool_name": "Bash",
                        "tool_input": {"command": server_command},
                    },
                },
                {
                    "logged_at": (now + timedelta(seconds=1)).isoformat(),
                    "event": {
                        "hook_event_name": "PermissionRequest",
                        "session_id": session_id,
                        "cwd": "/Users/pero/pgit/a1plotter",
                        "tool_name": "Bash",
                        "tool_input": {"command": server_command},
                    },
                },
                {
                    "logged_at": (now + timedelta(seconds=2)).isoformat(),
                    "event": {
                        "hook_event_name": "PreToolUse",
                        "session_id": session_id,
                        "cwd": "/Users/pero/pgit/a1plotter",
                        "tool_name": "Bash",
                        "tool_input": {"command": curl_command},
                    },
                },
                {
                    "logged_at": (now + timedelta(seconds=3)).isoformat(),
                    "event": {
                        "hook_event_name": "PostToolUse",
                        "session_id": session_id,
                        "cwd": "/Users/pero/pgit/a1plotter",
                        "tool_name": "Bash",
                        "tool_input": {"command": curl_command},
                        "tool_response": "{}",
                    },
                },
            ]
            log.write_text("\n".join(json.dumps(event) for event in events) + "\n")

            monitor = AgentMonitor(
                sources=(SourceSpec("codex", log),),
                stale_after_seconds=3600,
            )
            snapshot = monitor.snapshot()

            self.assertEqual(snapshot.aggregate.mode, AgentMode.WAITING_FOR_INPUT)
            self.assertEqual(snapshot.statuses[0].event_name, "PermissionRequest")

    def test_codex_permission_request_clears_when_matching_tool_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "codex.jsonl"
            now = datetime.now(timezone.utc)
            session_id = "019f179b-7fdc-7eb0-a3af-1ca3eb128eee"
            command = "curl -s http://127.0.0.1:5425/api/status | head -c 1000"
            events = [
                {
                    "logged_at": now.isoformat(),
                    "event": {
                        "hook_event_name": "PreToolUse",
                        "session_id": session_id,
                        "cwd": "/Users/pero/pgit/a1plotter",
                        "tool_name": "Bash",
                        "tool_input": {"command": command},
                    },
                },
                {
                    "logged_at": (now + timedelta(seconds=1)).isoformat(),
                    "event": {
                        "hook_event_name": "PermissionRequest",
                        "session_id": session_id,
                        "cwd": "/Users/pero/pgit/a1plotter",
                        "tool_name": "Bash",
                        "tool_input": {"command": command},
                    },
                },
                {
                    "logged_at": (now + timedelta(seconds=2)).isoformat(),
                    "event": {
                        "hook_event_name": "PostToolUse",
                        "session_id": session_id,
                        "cwd": "/Users/pero/pgit/a1plotter",
                        "tool_name": "Bash",
                        "tool_input": {"command": command},
                        "tool_response": "{}",
                    },
                },
            ]
            log.write_text("\n".join(json.dumps(event) for event in events) + "\n")

            monitor = AgentMonitor(
                sources=(SourceSpec("codex", log),),
                stale_after_seconds=3600,
            )
            snapshot = monitor.snapshot()

            self.assertEqual(snapshot.aggregate.mode, AgentMode.WORKING)
            self.assertEqual(snapshot.statuses[0].event_name, "PostToolUse")

    def test_post_tool_use_does_not_stay_working_indefinitely(self) -> None:
        now = datetime.now(timezone.utc)
        status = AgentStatus(
            provider="codex",
            agent_id="codex:session:tool-session",
            display_name="tool-session",
            mode=AgentMode.WORKING,
            updated_at=now
            - timedelta(seconds=collector_module.POST_TOOL_WORKING_VISIBLE_SECONDS + 1),
            event_name="PostToolUse",
            session_id="tool-session",
            cwd="/tmp/project",
            tool_name="webrun",
        )

        snapshot = collector_module.snapshot_from_statuses(
            (status,),
            sources=(),
            collected_at=now,
            stale_after_seconds=3600,
            tool_running_timeout_seconds=0,
            completed_visible_seconds=20 * 60,
            idle_visible_seconds=0,
        )

        self.assertEqual(snapshot.aggregate.mode, AgentMode.COMPLETED)
        self.assertEqual(snapshot.aggregate.active_count, 0)
        self.assertEqual(snapshot.statuses[0].event_name, "PostToolUse")

    def test_internal_codex_helper_sessions_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "codex.jsonl"
            now = datetime.now(timezone.utc).isoformat()
            log.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "logged_at": now,
                                "event": {
                                    "hook_event_name": "UserPromptSubmit",
                                    "session_id": "codex-helper",
                                    "cwd": "/Users/example/pgit/sidepulse",
                                    "prompt": "Overview\nGenerate 0 to 3 hyperpersonalized suggestions for what this user might do.",
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "logged_at": now,
                                "event": {
                                    "hook_event_name": "PostToolUse",
                                    "session_id": "codex-helper",
                                    "cwd": "/Users/example/pgit/sidepulse",
                                    "tool_name": "mcp__codex_apps__gmail__batch_read_email",
                                },
                            }
                        ),
                    ]
                )
                + "\n"
            )

            monitor = AgentMonitor(
                sources=(SourceSpec("codex", log),),
                stale_after_seconds=3600,
            )
            snapshot = monitor.snapshot()

            self.assertEqual(snapshot.aggregate.mode, AgentMode.IDLE_READY)
            self.assertEqual(snapshot.statuses, ())

    def test_codex_transcript_fallback_marks_recent_user_turn_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_id = "aaaaaaaa-bbbb-7ccc-8ddd-eeeeeeeeeeee"
            path = root / "2026" / "06" / "29" / f"rollout-2026-06-29T08-27-42-{session_id}.jsonl"
            path.parent.mkdir(parents=True)
            now = datetime.now(timezone.utc).isoformat()
            path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "timestamp": now,
                                "type": "turn_context",
                                "payload": {
                                    "turn_id": "turn-1",
                                    "cwd": "/Users/pero/pgit/sidepulse",
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": now,
                                "type": "response_item",
                                "payload": {
                                    "type": "message",
                                    "role": "user",
                                    "content": [
                                        {
                                            "type": "input_text",
                                            "text": "it didnt catch this conversation",
                                        }
                                    ],
                                },
                            }
                        ),
                    ]
                )
                + "\n"
            )

            monitor = AgentMonitor(
                sources=(SourceSpec("codex-transcripts", root),),
                stale_after_seconds=3600,
            )
            snapshot = monitor.snapshot()

            self.assertEqual(snapshot.aggregate.mode, AgentMode.WORKING)
            self.assertEqual(snapshot.statuses[0].session_id, session_id)
            self.assertIn("sidepulse", snapshot.statuses[0].display_name)
            self.assertIn("it didnt catch", snapshot.statuses[0].display_name)

    def test_transcript_records_are_cached_until_file_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_id = "019ee395-2f64-7cc3-b566-afcc1d626160"
            path = root / f"rollout-2026-06-29T08-27-42-{session_id}.jsonl"
            now = datetime.now(timezone.utc).isoformat()
            path.write_text(
                json.dumps(
                    {
                        "timestamp": now,
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "name": "exec_command",
                            "call_id": "call-1",
                            "arguments": "{}",
                        },
                    }
                )
                + "\n"
            )
            monitor = AgentMonitor(
                sources=(SourceSpec("codex-transcripts", root),),
                stale_after_seconds=3600,
            )
            calls: list[Path] = []
            original_read_recent_lines = collector_module.read_recent_lines

            def counting_read_recent_lines(read_path: Path, max_lines: int) -> list[str]:
                if read_path == path:
                    calls.append(read_path)
                return original_read_recent_lines(read_path, max_lines)

            with patch(
                "sidepulse.collector.read_recent_lines",
                side_effect=counting_read_recent_lines,
            ):
                self.assertEqual(monitor.snapshot().aggregate.mode, AgentMode.TOOL_RUNNING)
                self.assertEqual(monitor.snapshot().aggregate.mode, AgentMode.TOOL_RUNNING)
                self.assertEqual(calls, [path])

                with path.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(
                            {
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                                "type": "response_item",
                                "payload": {
                                    "type": "function_call_output",
                                    "call_id": "call-1",
                                    "output": "{}",
                                },
                            }
                        )
                        + "\n"
                    )

                self.assertEqual(monitor.snapshot().aggregate.mode, AgentMode.WORKING)
                self.assertEqual(calls, [path, path])

    def test_hook_log_records_are_cached_until_file_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "codex.jsonl"
            now = datetime.now(timezone.utc).isoformat()
            log.write_text(
                json.dumps(
                    {
                        "logged_at": now,
                        "event": {
                            "hook_event_name": "PreToolUse",
                            "session_id": "codex-session",
                            "tool_name": "Bash",
                        },
                    }
                )
                + "\n"
            )
            monitor = AgentMonitor(
                sources=(SourceSpec("codex", log),),
                stale_after_seconds=3600,
            )
            calls: list[Path] = []
            original_read_recent_lines = collector_module.read_recent_lines

            def counting_read_recent_lines(read_path: Path, max_lines: int) -> list[str]:
                if read_path == log:
                    calls.append(read_path)
                return original_read_recent_lines(read_path, max_lines)

            with patch(
                "sidepulse.collector.read_recent_lines",
                side_effect=counting_read_recent_lines,
            ):
                self.assertEqual(monitor.snapshot().aggregate.mode, AgentMode.TOOL_RUNNING)
                self.assertEqual(monitor.snapshot().aggregate.mode, AgentMode.TOOL_RUNNING)
                self.assertEqual(calls, [log])

                with log.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(
                            {
                                "logged_at": datetime.now(timezone.utc).isoformat(),
                                "event": {
                                    "hook_event_name": "PostToolUse",
                                    "session_id": "codex-session",
                                    "tool_name": "Bash",
                                },
                            }
                        )
                        + "\n"
                    )

                self.assertEqual(monitor.snapshot().aggregate.mode, AgentMode.WORKING)
                self.assertEqual(calls, [log, log])

    def test_snapshot_reuses_latest_statuses_when_inputs_are_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "codex.jsonl"
            log.write_text(
                json.dumps(
                    {
                        "logged_at": datetime.now(timezone.utc).isoformat(),
                        "event": {
                            "hook_event_name": "PreToolUse",
                            "session_id": "codex-session",
                            "tool_name": "Bash",
                        },
                    }
                )
                + "\n"
            )
            monitor = AgentMonitor(
                sources=(SourceSpec("codex", log),),
                stale_after_seconds=3600,
            )

            with patch(
                "sidepulse.collector.status_from_event",
                wraps=collector_module.status_from_event,
            ) as status_from_event:
                self.assertEqual(monitor.snapshot().aggregate.mode, AgentMode.TOOL_RUNNING)
                first_count = status_from_event.call_count
                self.assertGreater(first_count, 0)

                self.assertEqual(monitor.snapshot().aggregate.mode, AgentMode.TOOL_RUNNING)
                self.assertEqual(status_from_event.call_count, first_count)

    def test_codex_transcript_fallback_marks_tool_calls_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_id = "019ee395-2f64-7cc3-b566-afcc1d626160"
            path = root / "rollout-2026-06-29T08-27-42-" / f"{session_id}.jsonl"
            path.parent.mkdir(parents=True)
            now = datetime.now(timezone.utc).isoformat()
            path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "timestamp": now,
                                "type": "turn_context",
                                "payload": {
                                    "turn_id": "turn-1",
                                    "cwd": "/tmp/project",
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": now,
                                "type": "response_item",
                                "payload": {
                                    "type": "function_call",
                                    "name": "exec_command",
                                    "call_id": "call-1",
                                    "arguments": "{}",
                                },
                            }
                        ),
                    ]
                )
                + "\n"
            )

            monitor = AgentMonitor(
                sources=(SourceSpec("codex-transcripts", root),),
                stale_after_seconds=3600,
            )
            snapshot = monitor.snapshot()

            self.assertEqual(snapshot.aggregate.mode, AgentMode.TOOL_RUNNING)
            self.assertEqual(snapshot.statuses[0].tool_name, "exec_command")

    def test_codex_transcript_task_complete_overrides_last_tool_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_id = "019f179b-7fdc-7eb0-a3af-1ca3eb128eee"
            path = root / f"rollout-2026-06-30T01-18-14-{session_id}.jsonl"
            now = datetime.now(timezone.utc).isoformat()
            path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "timestamp": now,
                                "type": "turn_context",
                                "payload": {"cwd": "/tmp/project"},
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": now,
                                "type": "response_item",
                                "payload": {
                                    "type": "function_call_output",
                                    "call_id": "call-1",
                                    "output": "ok",
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": now,
                                "type": "event_msg",
                                "payload": {
                                    "type": "task_complete",
                                    "last_agent_message": "All set.",
                                },
                            }
                        ),
                    ]
                )
                + "\n"
            )

            monitor = AgentMonitor(
                sources=(SourceSpec("codex-transcripts", root),),
                stale_after_seconds=3600,
            )
            snapshot = monitor.snapshot()

            self.assertEqual(snapshot.aggregate.mode, AgentMode.COMPLETED)
            self.assertEqual(snapshot.statuses[0].event_name, "Stop")

    def test_claude_transcript_fallback_marks_tool_calls_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_id = "e289f361-f64f-415e-8dd3-01ed835f7869"
            path = root / "-Users-pero-pgit-sdrgb" / f"{session_id}.jsonl"
            path.parent.mkdir(parents=True)
            now = datetime.now(timezone.utc).isoformat()
            path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "timestamp": now,
                                "type": "user",
                                "sessionId": session_id,
                                "cwd": "/Users/pero/pgit/sdrgb",
                                "message": {
                                    "role": "user",
                                    "content": "make a pull request",
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": now,
                                "type": "assistant",
                                "sessionId": session_id,
                                "cwd": "/Users/pero/pgit/sdrgb",
                                "message": {
                                    "role": "assistant",
                                    "stop_reason": "tool_use",
                                    "content": [
                                        {
                                            "type": "tool_use",
                                            "id": "toolu_1",
                                            "name": "Edit",
                                            "input": {"file_path": "README.md"},
                                        }
                                    ],
                                },
                            }
                        ),
                    ]
                )
                + "\n"
            )

            monitor = AgentMonitor(
                sources=(SourceSpec("claude-transcripts", root),),
                stale_after_seconds=3600,
            )
            snapshot = monitor.snapshot()

            self.assertEqual(snapshot.aggregate.mode, AgentMode.TOOL_RUNNING)
            self.assertEqual(snapshot.statuses[0].provider, "claude")
            self.assertEqual(snapshot.statuses[0].tool_name, "Edit")

    def test_claude_transcript_mtime_extends_active_file_activity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_id = "e289f361-f64f-415e-8dd3-01ed835f7869"
            path = root / f"{session_id}.jsonl"
            old = datetime.now(timezone.utc) - timedelta(minutes=25)
            now = datetime.now(timezone.utc)
            path.write_text(
                json.dumps(
                    {
                        "timestamp": old.isoformat(),
                        "type": "user",
                        "sessionId": session_id,
                        "cwd": "/Users/pero/pgit/sdrgb",
                        "message": {
                            "role": "user",
                            "content": "keep going",
                        },
                    }
                )
                + "\n"
            )
            os.utime(path, (now.timestamp(), now.timestamp()))

            monitor = AgentMonitor(
                sources=(SourceSpec("claude-transcripts", root),),
                stale_after_seconds=3600,
            )
            snapshot = monitor.snapshot()

            self.assertEqual(snapshot.aggregate.mode, AgentMode.WORKING)
            self.assertEqual(snapshot.statuses[0].event_name, "Notification")

    def test_claude_transcript_mtime_does_not_resurrect_completed_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_id = "e289f361-f64f-415e-8dd3-01ed835f7869"
            path = root / f"{session_id}.jsonl"
            old = datetime.now(timezone.utc) - timedelta(minutes=25)
            now = datetime.now(timezone.utc)
            path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "timestamp": old.isoformat(),
                                "type": "user",
                                "sessionId": session_id,
                                "cwd": "/Users/pero/pgit/sdrgb",
                                "message": {
                                    "role": "user",
                                    "content": "it's ok, done",
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": (old + timedelta(seconds=10)).isoformat(),
                                "type": "assistant",
                                "sessionId": session_id,
                                "cwd": "/Users/pero/pgit/sdrgb",
                                "message": {
                                    "role": "assistant",
                                    "stop_reason": "end_turn",
                                    "content": [{"type": "text", "text": "Great, thanks for handling it."}],
                                },
                            }
                        ),
                    ]
                )
                + "\n"
            )
            os.utime(path, (now.timestamp(), now.timestamp()))

            monitor = AgentMonitor(
                sources=(SourceSpec("claude-transcripts", root),),
                stale_after_seconds=3600,
            )
            snapshot = monitor.snapshot()

            self.assertEqual(snapshot.aggregate.mode, AgentMode.IDLE_READY)
            self.assertEqual(snapshot.statuses, ())
            self.assertEqual(snapshot.stale_statuses[0].mode, AgentMode.COMPLETED)

    def test_final_question_maps_to_waiting_for_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "codex.jsonl"
            log.write_text(
                json.dumps(
                    {
                        "logged_at": "2026-06-20T06:00:00Z",
                        "event": {
                            "hook_event_name": "Stop",
                            "session_id": "codex-session",
                            "last_assistant_message": "Which mode do you see now?",
                        },
                    }
                )
                + "\n"
            )

            monitor = AgentMonitor(
                sources=(SourceSpec("codex", log),),
                stale_after_seconds=999999999,
                tool_running_timeout_seconds=0,
            )
            snapshot = monitor.snapshot()

            self.assertEqual(snapshot.aggregate.mode, AgentMode.WAITING_FOR_INPUT)

    def test_anything_else_prompt_maps_to_completed_before_recaps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "codex.jsonl"
            now = datetime.now(timezone.utc).isoformat()
            log.write_text(
                json.dumps(
                    {
                        "logged_at": now,
                        "event": {
                            "hook_event_name": "Stop",
                            "session_id": "codex-session",
                            "last_assistant_message": "\n".join(
                                [
                                    "Anything else you want to tweak?",
                                    "",
                                    "* Cogitated for 40s - 1 shell still running",
                                    "※ recap: We built and deployed the SidePulse Pro/SidePulse Dot product status.",
                                ]
                            ),
                        },
                    }
                )
                + "\n"
            )

            monitor = AgentMonitor(
                sources=(SourceSpec("codex", log),),
                stale_after_seconds=999999999,
                tool_running_timeout_seconds=0,
            )
            snapshot = monitor.snapshot()

            self.assertEqual(snapshot.aggregate.mode, AgentMode.COMPLETED)

    def test_concrete_followup_question_maps_to_waiting_for_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "claude.jsonl"
            log.write_text(
                json.dumps(
                    {
                        "logged_at": "2026-06-20T06:00:00Z",
                        "hook_event_name": "Stop",
                        "session_id": "claude-session",
                        "last_assistant_message": (
                            "Committed as `67b0208` but not pushed. "
                            "Want me to push?"
                        ),
                    }
                )
                + "\n"
            )

            monitor = AgentMonitor(
                sources=(SourceSpec("claude", log),),
                stale_after_seconds=999999999,
                tool_running_timeout_seconds=0,
            )
            snapshot = monitor.snapshot()

            self.assertEqual(snapshot.aggregate.mode, AgentMode.WAITING_FOR_INPUT)

    def test_question_examples_in_inline_code_do_not_map_to_waiting_for_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "codex.jsonl"
            now = datetime.now(timezone.utc).isoformat()
            log.write_text(
                json.dumps(
                    {
                        "logged_at": now,
                        "event": {
                            "hook_event_name": "Stop",
                            "session_id": "codex-session",
                            "last_assistant_message": "\n".join(
                                [
                                    "Now:",
                                    "- `Committed but not pushed. Want me to push?` => `Ask`",
                                    "- `Which mode do you see now?` => `Ask`",
                                    "",
                                    "Verified: `42` tests pass.",
                                ]
                            ),
                        },
                    }
                )
                + "\n"
            )

            monitor = AgentMonitor(
                sources=(SourceSpec("codex", log),),
                stale_after_seconds=999999999,
            )
            snapshot = monitor.snapshot()

            self.assertEqual(snapshot.aggregate.mode, AgentMode.COMPLETED)

    def test_real_question_with_inline_code_maps_to_waiting_for_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "codex.jsonl"
            log.write_text(
                json.dumps(
                    {
                        "logged_at": "2026-06-20T06:00:00Z",
                        "event": {
                            "hook_event_name": "Stop",
                            "session_id": "codex-session",
                            "last_assistant_message": "Want me to run `git push`?",
                        },
                    }
                )
                + "\n"
            )

            monitor = AgentMonitor(
                sources=(SourceSpec("codex", log),),
                stale_after_seconds=999999999,
            )
            snapshot = monitor.snapshot()

            self.assertEqual(snapshot.aggregate.mode, AgentMode.WAITING_FOR_INPUT)

    def test_answer_heading_does_not_map_to_waiting_for_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "codex.jsonl"
            now = datetime.now(timezone.utc).isoformat()
            log.write_text(
                json.dumps(
                    {
                        "logged_at": now,
                        "event": {
                            "hook_event_name": "Stop",
                            "session_id": "codex-session",
                            "last_assistant_message": "\n".join(
                                [
                                    "No. Nothing in this payload exposes live XYZ.",
                                    "",
                                    "What we can infer from this:",
                                    "",
                                    "- MQTT print status is useful for uploaded jobs.",
                                ]
                            ),
                        },
                    }
                )
                + "\n"
            )

            monitor = AgentMonitor(
                sources=(SourceSpec("codex", log),),
                stale_after_seconds=999999999,
            )
            snapshot = monitor.snapshot()

            self.assertEqual(snapshot.aggregate.mode, AgentMode.COMPLETED)

    def test_explicit_sidepulse_marker_maps_to_waiting_for_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "codex.jsonl"
            log.write_text(
                json.dumps(
                    {
                        "logged_at": "2026-06-20T06:00:00Z",
                        "event": {
                            "hook_event_name": "Stop",
                            "session_id": "codex-session",
                            "last_assistant_message": "I need your choice.\n<!-- sidepulse:ask -->",
                        },
                    }
                )
                + "\n"
            )

            monitor = AgentMonitor(
                sources=(SourceSpec("codex", log),),
                stale_after_seconds=999999999,
                tool_running_timeout_seconds=0,
            )
            snapshot = monitor.snapshot()

            self.assertEqual(snapshot.aggregate.mode, AgentMode.WAITING_FOR_INPUT)

    def test_explicit_sidepulse_marker_overrides_question_heuristic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "codex.jsonl"
            now = datetime.now(timezone.utc).isoformat()
            log.write_text(
                json.dumps(
                    {
                        "logged_at": now,
                        "event": {
                            "hook_event_name": "Stop",
                            "session_id": "codex-session",
                            "last_assistant_message": "Anything else to tweak?\n<!-- sidepulse:done -->",
                        },
                    }
                )
                + "\n"
            )

            monitor = AgentMonitor(
                sources=(SourceSpec("codex", log),),
                stale_after_seconds=999999999,
                tool_running_timeout_seconds=0,
            )
            snapshot = monitor.snapshot()

            self.assertEqual(snapshot.aggregate.mode, AgentMode.COMPLETED)

    def test_explicit_sidepulse_field_maps_to_waiting_for_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "claude.jsonl"
            log.write_text(
                json.dumps(
                    {
                        "logged_at": "2026-06-20T06:00:00Z",
                        "hook_event_name": "Stop",
                        "session_id": "claude-session",
                        "last_assistant_message": "Done-ish.",
                        "sidepulse_status": "ask",
                    }
                )
                + "\n"
            )

            monitor = AgentMonitor(
                sources=(SourceSpec("claude", log),),
                stale_after_seconds=999999999,
                tool_running_timeout_seconds=0,
            )
            snapshot = monitor.snapshot()

            self.assertEqual(snapshot.aggregate.mode, AgentMode.WAITING_FOR_INPUT)

    def test_explicit_marker_inside_code_block_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "codex.jsonl"
            now = datetime.now(timezone.utc).isoformat()
            log.write_text(
                json.dumps(
                    {
                        "logged_at": now,
                        "event": {
                            "hook_event_name": "Stop",
                            "session_id": "codex-session",
                            "last_assistant_message": "Use:\n```text\n<!-- sidepulse:ask -->\n```",
                        },
                    }
                )
                + "\n"
            )

            monitor = AgentMonitor(
                sources=(SourceSpec("codex", log),),
                stale_after_seconds=999999999,
                tool_running_timeout_seconds=0,
            )
            snapshot = monitor.snapshot()

            self.assertEqual(snapshot.aggregate.mode, AgentMode.COMPLETED)

    def test_session_display_name_uses_prompt_context_after_later_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "codex.jsonl"
            session_id = "dddddddd-eeee-7fff-8aaa-bbbbbbbbbbbb"
            prompt = """
# Files mentioned by the user:

## codex-clipboard.png: /var/folders/tmp/codex-clipboard.png

## My request for Codex:
team id YOUR_TEAM_ID, push key '/path/to/AuthKey_YOUR_KEY_ID.p8'
"""
            log.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "logged_at": "2026-06-22T19:50:58Z",
                                "event": {
                                    "hook_event_name": "UserPromptSubmit",
                                    "session_id": session_id,
                                    "cwd": "/Users/pero/pgit/sidepulse",
                                    "prompt": prompt,
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "logged_at": "2026-06-22T19:51:09Z",
                                "event": {
                                    "hook_event_name": "PreToolUse",
                                    "session_id": session_id,
                                    "cwd": "/Users/pero/pgit/sidepulse",
                                    "tool_name": "Bash",
                                },
                            }
                        ),
                    ]
                )
                + "\n"
            )

            monitor = AgentMonitor(
                sources=(SourceSpec("codex", log),),
                stale_after_seconds=999999999,
                tool_running_timeout_seconds=0,
            )
            snapshot = monitor.snapshot()
            status = snapshot.statuses[0]

            self.assertIn("sidepulse", status.display_name)
            self.assertIn("team id YOUR_TEAM_ID", status.display_name)
            self.assertIn(session_id[:8], status.display_name)
            self.assertNotIn("/Users/example", status.display_name)

    def test_codex_display_name_uses_session_index_thread_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / "home"
            codex_home = home / ".codex"
            codex_home.mkdir(parents=True)
            session_id = "bbbbbbbb-cccc-7ddd-8eee-ffffffffffff"
            (codex_home / "session_index.jsonl").write_text(
                json.dumps(
                    {
                        "id": session_id,
                        "thread_name": "Refine README agent status modes",
                        "updated_at": "2026-06-20T05:52:21.985091Z",
                    }
                )
                + "\n"
            )
            log = base / "codex.jsonl"
            now = datetime.now(timezone.utc).isoformat()
            log.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "logged_at": now,
                                "event": {
                                    "hook_event_name": "UserPromptSubmit",
                                    "session_id": session_id,
                                    "cwd": "/Users/pero/pgit/sidepulse",
                                    "prompt": "Why are we burning so much CPU",
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "logged_at": now,
                                "event": {
                                    "hook_event_name": "PreToolUse",
                                    "session_id": session_id,
                                    "cwd": "/Users/pero/pgit/sidepulse",
                                    "tool_name": "Bash",
                                },
                            }
                        ),
                    ]
                )
                + "\n"
            )

            with patch("sidepulse.collector.Path.home", return_value=home):
                monitor = AgentMonitor(
                    sources=(SourceSpec("codex", log),),
                    stale_after_seconds=999999999,
                )
                snapshot = monitor.snapshot()

            name = snapshot.statuses[0].display_name
            self.assertIn("sidepulse", name)
            self.assertIn("Refine README agent status modes", name)
            self.assertNotIn("Why are we burning", name)

    def test_session_display_name_keeps_initial_prompt_title(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            log = base / "grok.jsonl"
            now = datetime.now(timezone.utc)
            session_id = "019ffd37-1458-7d92-b077-3d0f92aedde4"
            log.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "logged_at": now.isoformat(),
                                "hookEventName": "user_prompt_submit",
                                "sessionId": session_id,
                                "workspaceRoot": "/Users/pero/temp/msdosfs",
                                "prompt": "<user_query>\nWhat is here\n</user_query>",
                            }
                        ),
                        json.dumps(
                            {
                                "logged_at": (now + timedelta(seconds=30)).isoformat(),
                                "hookEventName": "user_prompt_submit",
                                "sessionId": session_id,
                                "workspaceRoot": "/Users/pero/temp/msdosfs",
                                "prompt": "<user_query>\nNow check permissions\n</user_query>",
                            }
                        ),
                        json.dumps(
                            {
                                "logged_at": (now + timedelta(seconds=60)).isoformat(),
                                "hookEventName": "stop",
                                "sessionId": session_id,
                                "workspaceRoot": "/Users/pero/temp/msdosfs",
                            }
                        ),
                    ]
                )
                + "\n"
            )

            monitor = AgentMonitor(
                sources=(SourceSpec("grok", log),),
                stale_after_seconds=999999999,
            )
            snapshot = monitor.snapshot()

            name = snapshot.statuses[0].display_name
            self.assertIn("What is here", name)
            self.assertNotIn("Now check permissions", name)

    def test_live_monitor_refreshes_loaded_codex_display_name_from_session_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / "home"
            codex_home = home / ".codex"
            codex_home.mkdir(parents=True)
            session_id = "cccccccc-dddd-7eee-8fff-aaaaaaaaaaaa"
            (codex_home / "session_index.jsonl").write_text(
                json.dumps(
                    {
                        "id": session_id,
                        "thread_name": "Refine README agent status modes",
                    }
                )
                + "\n"
            )
            latest = base / "latest.json"
            now = datetime.now(timezone.utc)
            latest.write_text(
                json.dumps(
                    {
                        "updated_at": now.isoformat(),
                        "statuses": [
                            {
                                "provider": "codex",
                                "agent_id": f"codex:session:{session_id}",
                                "display_name": (
                                    "sidepulse: Why are we burning so much CPU "
                                    f"({session_id[:8]})"
                                ),
                                "mode": "working",
                                "updated_at": now.isoformat(),
                                "event_name": "UserPromptSubmit",
                                "session_id": session_id,
                                "cwd": "/Users/pero/pgit/sidepulse",
                            }
                        ],
                    }
                )
                + "\n"
            )

            with patch("sidepulse.collector.Path.home", return_value=home):
                monitor = LiveAgentMonitor(latest_state_path=latest)
                snapshot = monitor.snapshot()

            name = snapshot.statuses[0].display_name
            self.assertIn("Refine README agent status modes", name)
            self.assertNotIn("Why are we burning", name)

    def test_task_notification_does_not_replace_session_display_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "claude.jsonl"
            session_id = "1ca4348e-2aec-4147-9e81-d7d56364d257"
            log.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "logged_at": "2026-06-22T07:20:00Z",
                                "hook_event_name": "UserPromptSubmit",
                                "session_id": session_id,
                                "cwd": "/Users/pero/pgit/sdstatus_bitbang",
                                "prompt": "convert these videos to mp4",
                            }
                        ),
                        json.dumps(
                            {
                                "logged_at": "2026-06-22T07:24:05Z",
                                "hook_event_name": "UserPromptSubmit",
                                "session_id": session_id,
                                "cwd": "/Users/pero/pgit/sdstatus_bitbang",
                                "prompt": "<task-notification><status>completed</status></task-notification>",
                            }
                        ),
                    ]
                )
                + "\n"
            )

            monitor = AgentMonitor(
                sources=(SourceSpec("claude", log),),
                stale_after_seconds=999999999,
            )
            snapshot = monitor.snapshot()

            self.assertIn("convert these videos", snapshot.statuses[0].display_name)
            self.assertNotIn("task-notification", snapshot.statuses[0].display_name)

    def test_codex_session_actions_build_deeplink_and_resume_command(self) -> None:
        status = AgentStatus(
            provider="codex",
            agent_id="codex:session:abc",
            display_name="Codex abc",
            mode=AgentMode.WORKING,
            updated_at=datetime.now(timezone.utc),
            event_name="PreToolUse",
            session_id="019ee395-2f64-7cc3-b566-afcc1d626160",
            cwd="/tmp/project with spaces",
        )

        self.assertEqual(
            session_deep_link(status),
            "codex://threads/019ee395-2f64-7cc3-b566-afcc1d626160",
        )
        self.assertEqual(
            session_resume_command(status),
            "cd '/tmp/project with spaces' && codex resume 019ee395-2f64-7cc3-b566-afcc1d626160",
        )

    def test_session_default_open_action_follows_origin(self) -> None:
        def status_for(provider: str, origin: str) -> AgentStatus:
            return AgentStatus(
                provider=provider,
                agent_id=f"{provider}:session:abc",
                display_name=f"{provider} abc",
                mode=AgentMode.WORKING,
                updated_at=datetime.now(timezone.utc),
                event_name="PreToolUse",
                session_id="1ca4348e-2aec-4147-9e81-d7d56364d257",
                cwd="/Users/pero/pgit/example",
                origin=origin,
            )

        self.assertEqual(
            default_session_open_action(status_for("claude", "Claude in VS Code")),
            SESSION_OPEN_VSCODE,
        )
        self.assertEqual(
            default_session_open_action(status_for("claude", "Claude Code CLI")),
            SESSION_OPEN_TERMINAL,
        )
        self.assertEqual(
            default_session_open_action(status_for("claude", "Claude App")),
            SESSION_OPEN_APP,
        )
        self.assertEqual(
            default_session_open_action(status_for("codex", "Codex CLI")),
            SESSION_OPEN_TERMINAL,
        )
        self.assertEqual(
            default_session_open_action(status_for("codex", "Codex UI")),
            SESSION_OPEN_APP,
        )

    def test_claude_session_actions_build_app_link_and_resume_command(self) -> None:
        status = AgentStatus(
            provider="claude",
            agent_id="claude:session:abc",
            display_name="Claude abc",
            mode=AgentMode.WAITING_FOR_INPUT,
            updated_at=datetime.now(timezone.utc),
            event_name="Notification",
            session_id="1ca4348e-2aec-4147-9e81-d7d56364d257",
            cwd="/Users/pero/pgit/sdstatus_bitbang",
        )

        self.assertEqual(session_deep_link(status), "claude://")
        self.assertEqual(
            session_resume_command(status),
            "cd /Users/pero/pgit/sdstatus_bitbang && claude --resume 1ca4348e-2aec-4147-9e81-d7d56364d257",
        )
        self.assertEqual(
            session_vscode_link(status),
            "vscode://anthropic.claude-code/open?session=1ca4348e-2aec-4147-9e81-d7d56364d257",
        )
        self.assertEqual(default_session_open_action(status), "vscode")
        self.assertEqual(
            session_open_target(status, "vscode"),
            (
                "url",
                "vscode://anthropic.claude-code/open?session=1ca4348e-2aec-4147-9e81-d7d56364d257",
            ),
        )


if __name__ == "__main__":
    unittest.main()
