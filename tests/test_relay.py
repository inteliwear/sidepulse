from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from privacy import assert_private_file
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

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
            assert_private_file(path)

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

class RelayTransportTests(unittest.TestCase):
    def test_network_failure_retries_until_stopped(self):
        from sidepulse.relay import listen_for_status_updates
        stop = MagicMock(); stop.is_set.side_effect = [False, True]
        with patch('urllib.request.urlopen', side_effect=OSError('offline')):
            listen_for_status_updates(RelayConfig('https://example.test',receiver_channel='sample-channel'), stop, Mock())
        stop.wait.assert_called_once_with(1.0)


class SidePulseServiceTests(unittest.TestCase):
    def setUp(self):
        from sidepulse.program_status import ProgramState
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = ProgramState()
        self.addCleanup(self.store.close)
        replacement = patch('sidepulse.program_status.ProgramState', return_value=self.store)
        replacement.start(); self.addCleanup(replacement.stop)

    def test_service_has_no_raw_hook_listener_or_delivery(self):
        service = SidePulseService()
        self.assertFalse(hasattr(service, 'local_server'))
        self.assertFalse(hasattr(service, 'handle_local_event'))
        self.assertFalse(hasattr(service, 'handle_remote_event'))

    def test_dead_remote_listener_is_restarted(self):
        service = SidePulseService()
        config = RelayConfig('https://example.test',receiver_channel='sample-channel')
        service.receiver_signature = (config.server,config.receiver_channel)
        service.receiver_thread = SimpleNamespace(is_alive=lambda:False)
        with patch('sidepulse.service.threading.Thread') as thread:
            service.sync_receiver(config)
        thread.return_value.start.assert_called_once()

    def test_live_remote_listener_is_not_restarted(self):
        service = SidePulseService()
        config = RelayConfig('https://example.test',receiver_channel='sample-channel')
        service.receiver_signature = (config.server,config.receiver_channel)
        service.receiver_thread = SimpleNamespace(is_alive=lambda:True)
        with patch('sidepulse.service.threading.Thread') as thread:
            service.sync_receiver(config)
        thread.assert_not_called()

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
            patch("sidepulse.service.load_settings", return_value=settings),
            patch("sidepulse.daemon_runtime.discover_devices", return_value=[]),
            patch("sidepulse.links.send_ios_program") as send,
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
                patch("sidepulse.service.load_settings", return_value=AgentMonitorSettings()),
                patch(
                    "sidepulse.daemon_runtime.discover_devices",
                    return_value=(DeviceCandidate(root, target, "test"),),
                ),
            ):
                service.runtime.devices = [DeviceCandidate(root, target, "test")]
                service.sync_outputs()

            self.assertTrue(target.exists())
            self.assertIn("#", target.read_text(encoding="utf-8"))

    def test_daemon_keeps_output_ownership_when_ui_is_running(self):
        service = SidePulseService()
        with patch('sidepulse.daemon_runtime.discover_devices',return_value=[]), patch.object(service.monitor,'snapshot',wraps=service.monitor.snapshot) as snapshot:
            service.sync_outputs()
        snapshot.assert_called_once()


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

    def test_linux_restarts_the_unit_so_upgrades_replace_the_running_service(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            completed = SimpleNamespace(returncode=0, stderr="")
            with (
                patch.object(Path, "home", return_value=home),
                patch("sidepulse.service_launch.shutil.which", return_value="/usr/bin/systemctl"),
                patch(
                    "sidepulse.service_launch.subprocess.run",
                    return_value=completed,
                ) as run,
            ):
                result = install_systemd_user_service(start=True, dry_run=False)

            self.assertTrue(result.started)
            commands = [call.args[0] for call in run.call_args_list]
            verbs = [command[2] for command in commands]
            self.assertEqual(verbs, ["daemon-reload", "enable", "restart"])
            # `enable --now` leaves an already-running unit on the old code.
            self.assertNotIn("--now", [part for command in commands for part in command])

    def test_linux_restarts_the_unit_so_upgrades_replace_the_running_service(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            completed = SimpleNamespace(returncode=0, stderr="")
            with (
                patch.object(Path, "home", return_value=home),
                patch("sidepulse.service_launch.shutil.which", return_value="/usr/bin/systemctl"),
                patch(
                    "sidepulse.service_launch.subprocess.run",
                    return_value=completed,
                ) as run,
            ):
                result = install_systemd_user_service(start=True, dry_run=False)

            self.assertTrue(result.started)
            commands = [call.args[0] for call in run.call_args_list]
            verbs = [command[2] for command in commands]
            self.assertEqual(verbs, ["daemon-reload", "enable", "restart"])
            # `enable --now` leaves an already-running unit on the old code.
            self.assertNotIn("--now", [part for command in commands for part in command])


if __name__ == "__main__":
    unittest.main()
