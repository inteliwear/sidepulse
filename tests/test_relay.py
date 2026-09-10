from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from sidepulse.cli import sidepulse_main
from sidepulse.device_writer import DeviceCandidate
from sidepulse.hook import hook_log_main
from sidepulse.models import AgentMode
from sidepulse.relay import (
    RelayConfig,
    configure_outbound_channel,
    ensure_receiver_config,
    load_relay_config,
    new_relay_channel,
    parse_relay_event,
    publish_relay_event,
    relay_socket_path_for_state_dir,
    relay_event_message,
    relay_link_command,
)
from sidepulse.service import SidePulseService
from sidepulse.service_launch import install_systemd_user_service
from sidepulse.settings import AgentMonitorSettings


class RelayConfigTests(unittest.TestCase):
    def test_receiver_code_is_persistent_and_128_bit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "relay.json"
            first = ensure_receiver_config(path)
            second = ensure_receiver_config(path)

            self.assertEqual(first.receiver_channel, second.receiver_channel)
            self.assertEqual(len(first.receiver_channel), 22)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_outbound_code_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "relay.json"
            config = configure_outbound_channel("a" * 22, path=path)

            self.assertEqual(config.outbound_channel, "a" * 22)
            self.assertEqual(load_relay_config(path), config)

    def test_link_command_is_short_for_default_server(self) -> None:
        config = RelayConfig(
            server="https://bridge.sidepulse.io",
            receiver_channel="a" * 22,
        )
        self.assertEqual(relay_link_command(config), f"sidepulse link {'a' * 22}")

    def test_relay_codes_are_unique_base64url(self) -> None:
        codes = {new_relay_channel() for _ in range(100)}
        alphabet = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
        self.assertEqual(len(codes), 100)
        self.assertTrue(all(len(code) == 22 for code in codes))
        self.assertTrue(all(set(code) <= alphabet for code in codes))

    def test_link_code_configures_this_computer_as_a_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            environment = {
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(home / ".config"),
            }
            with patch.dict(os.environ, environment):
                result = sidepulse_main(["link", "a" * 22])
                config = load_relay_config()

        self.assertEqual(result, 0)
        self.assertEqual(config.outbound_channel, "a" * 22)

    def test_long_state_paths_use_a_short_socket_path(self) -> None:
        path = relay_socket_path_for_state_dir(Path("/tmp") / ("very-long/" * 20))
        self.assertEqual(path.parent, Path("/tmp"))
        self.assertLessEqual(len(str(path).encode("utf-8")), 96)


class RelayTransportTests(unittest.TestCase):
    def test_event_message_round_trips(self) -> None:
        line = {"hook_event_name": "Stop", "session_id": "vm-session"}
        message = relay_event_message("claude", line)
        parsed = parse_relay_event(json.dumps(message))

        self.assertIsNotNone(parsed)
        provider, parsed_line, event_id = parsed
        self.assertEqual(provider, "claude")
        self.assertEqual(parsed_line["hook_event_name"], "Stop")
        self.assertIn("sidepulse_relay_source", parsed_line)
        self.assertTrue(parsed_line["agent_id"].startswith("relay:"))
        self.assertTrue(event_id)

    def test_unknown_provider_is_rejected(self) -> None:
        message = relay_event_message("claude", {})
        message["provider"] = "../../escape"
        self.assertIsNone(parse_relay_event(json.dumps(message)))

    def test_publish_posts_to_receiver_channel(self) -> None:
        response = MagicMock()
        response.__enter__.return_value = response
        config = RelayConfig(
            server="https://bridge.sidepulse.io",
            outbound_channel="a" * 22,
        )
        with patch("sidepulse.relay.urllib.request.urlopen", return_value=response) as open_url:
            publish_relay_event(config, "codex", {"event": {"type": "task_complete"}})

        request = open_url.call_args.args[0]
        self.assertEqual(
            request.full_url,
            f"https://bridge.sidepulse.io/api/leds/{'a' * 22}",
        )
        self.assertEqual(json.loads(request.data)["type"], "agent_event")

    def test_hook_notifies_ui_and_background_service_sockets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            relay_socket = Path(tmp) / "relay.sock"
            log = Path(tmp) / "claude.jsonl"
            with (
                patch("sys.stdin", io.StringIO('{"hook_event_name":"Stop"}')),
                patch("sidepulse.hook.hook_event_socket_disabled", return_value=False),
                patch("sidepulse.hook.send_hook_event", return_value=True) as send,
                patch(
                    "sidepulse.relay.candidate_relay_event_socket_paths",
                    return_value=(relay_socket,),
                ),
            ):
                result = hook_log_main("claude", log)

        self.assertEqual(result, 0)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(send.call_args_list[1].kwargs["socket_path"], relay_socket)


class SidePulseServiceTests(unittest.TestCase):
    def test_local_hook_is_published_when_source_is_linked(self) -> None:
        service = SidePulseService()
        config = RelayConfig(
            server="https://bridge.sidepulse.io",
            outbound_channel="a" * 22,
        )
        line = {"hook_event_name": "Stop"}
        with (
            patch.object(service, "ingest_event"),
            patch("sidepulse.service.load_relay_config", return_value=config),
            patch("sidepulse.service.publish_relay_event") as publish,
        ):
            service.handle_local_event("claude", line)

        publish.assert_called_once_with(config, "claude", line)

    def test_remote_event_is_logged_forwarded_and_deduplicated(self) -> None:
        service = SidePulseService()
        line = {"hook_event_name": "Stop"}
        with (
            patch("sidepulse.service.detect_log_path", return_value=Path("/tmp/claude.jsonl")),
            patch("sidepulse.service.write_hook_line") as write,
            patch("sidepulse.service.send_hook_event") as forward,
        ):
            service.handle_remote_event("claude", line, "event-1")
            service.handle_remote_event("claude", line, "event-1")

        write.assert_called_once_with(Path("/tmp/claude.jsonl"), line)
        forward.assert_called_once_with("claude", line)

    def test_manual_linked_phone_is_not_updated(self) -> None:
        service = SidePulseService()
        service.monitor = SimpleNamespace(
            snapshot=lambda include_stale=False: SimpleNamespace(
                aggregate=SimpleNamespace(mode=AgentMode.WORKING)
            )
        )
        link = SimpleNamespace(link_id="phone", name="iPhone")
        settings = MagicMock()
        settings.battery_full_charge_watts = None
        settings.display_for_device.return_value = "custom"
        with (
            patch("sidepulse.service.status_bar_monitor_available", return_value=False),
            patch("sidepulse.service.load_settings", return_value=settings),
            patch("sidepulse.service.read_battery_snapshot", side_effect=OSError),
            patch("sidepulse.service.discover_devices", return_value=[]),
            patch("sidepulse.service.load_ios_links", return_value=(link,)),
            patch("sidepulse.service.send_ios_program") as send,
        ):
            service.sync_outputs()

        send.assert_not_called()

    def test_headless_service_writes_agent_status_to_local_dot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "SidePulseDot"
            root.mkdir()
            target = root / "LEDS.LED"
            service = SidePulseService()
            service.monitor = SimpleNamespace(
                snapshot=lambda include_stale=False: SimpleNamespace(
                    aggregate=SimpleNamespace(mode=AgentMode.WORKING)
                )
            )
            with (
                patch("sidepulse.service.status_bar_monitor_available", return_value=False),
                patch("sidepulse.service.load_settings", return_value=AgentMonitorSettings()),
                patch("sidepulse.service.read_battery_snapshot", side_effect=OSError),
                patch(
                    "sidepulse.service.discover_devices",
                    return_value=(DeviceCandidate(root, target, "test"),),
                ),
                patch("sidepulse.service.load_ios_links", return_value=()),
            ):
                service.sync_outputs()

            self.assertTrue(target.exists())
            self.assertIn("#", target.read_text(encoding="utf-8"))

    def test_status_bar_process_remains_output_owner_when_running(self) -> None:
        service = SidePulseService()
        with (
            patch("sidepulse.service.status_bar_monitor_available", return_value=True),
            patch.object(service.monitor, "snapshot") as snapshot,
        ):
            service.sync_outputs()

        snapshot.assert_not_called()


class ServiceLaunchTests(unittest.TestCase):
    def test_linux_installs_systemd_user_unit_without_requiring_running_systemd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with (
                patch.object(Path, "home", return_value=home),
                patch("sidepulse.service_launch.shutil.which", return_value=None),
            ):
                result = install_systemd_user_service(start=True, dry_run=False)

            text = result.path.read_text(encoding="utf-8")
            self.assertIn("SidePulse background agent", text)
            self.assertIn('"service" "run"', text)
            self.assertFalse(result.started)
            self.assertIn("unavailable", result.detail)


if __name__ == "__main__":
    unittest.main()
