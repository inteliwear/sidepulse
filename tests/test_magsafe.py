"""LED lifecycle fixtures never access AppleSMC or require authorization."""
import fcntl
import os
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from dataclasses import replace
from pathlib import Path

from sidepulse.magsafe import MAGSAFE_DEVICE_ID, MAGSAFE_DEVICE_NAME, MagSafeController, magsafe_state
from sidepulse.models import AgentMode
from sidepulse.settings import AgentMonitorSettings, load_settings, save_settings


class MagSafeStateTests(unittest.TestCase):
    def test_status_query_does_not_register(self):
        from sidepulse.cli import sidepulse_main
        service = Mock()
        service.status.return_value = 0
        with patch("sidepulse.magsafe.native_service", return_value=service):
            self.assertEqual(sidepulse_main(["status-bar", "magsafe-helper-status"]), 0)
            service.registerAndReturnError_.assert_not_called()
            service.unregisterAndReturnError_.assert_not_called()

    def test_explicit_helper_uninstall_uses_native_api(self):
        from sidepulse.cli import sidepulse_main
        service = Mock()
        service.status.return_value = 1
        service.unregisterAndReturnError_.return_value = (True, None)
        with patch("sidepulse.magsafe.native_service", return_value=service):
            self.assertEqual(sidepulse_main(["status-bar", "uninstall-magsafe-helper"]), 0)
            service.unregisterAndReturnError_.assert_called_once_with(None)
            service.registerAndReturnError_.assert_not_called()

    def test_uninstall_is_harmless_when_helper_is_absent(self):
        from sidepulse.magsafe import unregister_helper
        service = Mock()
        service.status.return_value = 0
        with patch("sidepulse.magsafe.native_service", return_value=service):
            unregister_helper()
        service.unregisterAndReturnError_.assert_not_called()

    def test_all_sidepulse_states_and_sleep(self):
        expected = {
            AgentMode.IDLE_READY: "idle", AgentMode.UNKNOWN: "idle",
            AgentMode.WORKING: "working", AgentMode.TOOL_RUNNING: "working",
            AgentMode.LONG_TASK_PROGRESS: "working", AgentMode.COMPLETED: "done",
            AgentMode.WAITING_FOR_INPUT: "ask", AgentMode.BLOCKED_ERROR: "ask",
        }
        for mode, state in expected.items():
            self.assertEqual(magsafe_state(mode), state)
            self.assertEqual(magsafe_state(mode, sleep_allowed=True), "idle")

    def test_opt_in_roundtrip_preserves_sleep_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text('{"sleep_prevention_policy":"never"}')
            settings = load_settings(path)
            self.assertFalse(settings.magsafe_led_enabled)
            save_settings(replace(settings, magsafe_led_enabled=True), path)
            loaded = load_settings(path)
            self.assertTrue(loaded.magsafe_led_enabled)
            self.assertEqual(loaded.sleep_prevention_policy, "never")

    def test_background_refresh_does_not_register_or_prompt(self):
        service = Mock()
        service.status.return_value = 2
        with patch("sidepulse.magsafe.native_service", return_value=service):
            controller = MagSafeController()
            controller.start()
            self.assertIsNone(controller._thread)
            self.assertIn("System Settings", controller.last_error)
            service.registerAndReturnError_.assert_not_called()

    def test_explicit_setup_registers_once(self):
        service = Mock()
        service.status.return_value = 0
        service.registerAndReturnError_.return_value = (True, None)
        with patch("sidepulse.magsafe.native_service", return_value=service):
            controller = MagSafeController()
            controller.request_approval()
            self.assertIsNone(controller.last_error)
            service.registerAndReturnError_.assert_called_once_with(None)

    def test_pending_approval_routes_to_system_settings(self):
        service = Mock()
        service.status.return_value = 2
        framework = Mock()
        with (
            patch("sidepulse.magsafe.native_service", return_value=service),
            patch.dict(sys.modules, {"ServiceManagement": framework}),
        ):
            controller = MagSafeController()
            controller.request_approval()
            framework.SMAppService.openSystemSettingsLoginItems.assert_called_once_with()
            service.registerAndReturnError_.assert_not_called()

    def test_enabled_helper_does_not_request_approval_again(self):
        service = Mock()
        service.status.return_value = 1
        with patch("sidepulse.magsafe.native_service", return_value=service):
            MagSafeController().request_approval()
            service.registerAndReturnError_.assert_not_called()

    def test_registration_error_is_visible(self):
        service = Mock()
        service.status.return_value = 0
        service.registerAndReturnError_.return_value = (False, "Registration denied")
        with patch("sidepulse.magsafe.native_service", return_value=service):
            controller = MagSafeController()
            controller.request_approval()
            self.assertEqual(controller.last_error, "Registration denied")
            controller.start()
            self.assertEqual(controller.last_error, "Registration denied")


@unittest.skipUnless(sys.platform == "darwin", "native fixture needs macOS SDK")
class MagSafeNativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory()
        cls.helper = Path(cls.build.name) / "sidepulse-magsafe"
        source = Path(__file__).resolve().parents[1] / "src/sidepulse/resources/magsafe.c"
        subprocess.run([
            "xcrun", "clang", "-O2", "-Wall", "-Wextra", "-Werror",
            "-DSIDEPULSE_MAGSAFE_TEST", str(source), "-framework", "IOKit",
            "-framework", "CoreFoundation", "-o", str(cls.helper),
        ], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.listener = socket.socket(socket.AF_UNIX)
        self.addCleanup(self.listener.close)
        self.address = self.root / "control.sock"
        self.listener.bind(str(self.address))
        self.listener.listen(1)
        self.listener.settimeout(3)
        self.trace = self.root / "trace"
        self.lock = self.root / "lock"

    def start_helper(self):
        process = subprocess.Popen(
            [str(self.helper), str(self.address), str(os.getuid())],
            env={**os.environ, "SIDEPULSE_MAGSAFE_TRACE": str(self.trace),
                 "SIDEPULSE_MAGSAFE_LOCK": str(self.lock)},
            start_new_session=True,
        )
        self.addCleanup(self.cleanup_process, process)
        connection, _ = self.listener.accept()
        self.addCleanup(connection.close)
        connection.settimeout(3)
        return process, connection

    @staticmethod
    def cleanup_process(process):
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)

    def writes(self):
        return [int(line) for line in self.trace.read_text().splitlines()] if self.trace.exists() else []

    def wait_for_write(self, predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if predicate(self.writes()):
                return
            time.sleep(0.02)
        self.fail(f"LED trace did not match: {self.writes()}")

    def test_idle_no_override_and_quit(self):
        process, connection = self.start_helper()
        self.assertEqual(connection.recv(256), b"READY\n")
        connection.sendall(b"idle\nquit\n")
        self.assertEqual(connection.recv(256), b"RESTORED\n")
        self.assertEqual(process.wait(timeout=3), 0)
        self.assertEqual(self.writes(), [])

    def test_active_idle_active_restores_and_reacquires(self):
        process, connection = self.start_helper()
        self.assertEqual(connection.recv(256), b"READY\n")
        connection.sendall(b"working\n")
        self.wait_for_write(lambda values: values == [3])
        connection.sendall(b"idle\n")
        self.wait_for_write(lambda values: values[-2:] == [4, 0])
        connection.sendall(b"ask\n")
        self.wait_for_write(lambda values: values[-1:] == [4])
        connection.sendall(b"quit\n")
        self.assertEqual(connection.recv(256), b"RESTORED\n")
        self.assertEqual(process.wait(timeout=3), 0)
        self.assertEqual(self.writes()[-2:], [4, 0])

    def test_guardian_restores_after_parent_killed(self):
        process, connection = self.start_helper()
        self.assertEqual(connection.recv(256), b"READY\n")
        connection.sendall(b"done\n")
        self.wait_for_write(lambda values: values == [3])
        process.kill()
        process.wait(timeout=3)
        self.wait_for_write(lambda values: values[-2:] == [4, 0])

    def test_service_group_termination_still_restores(self):
        process, connection = self.start_helper()
        self.assertEqual(connection.recv(256), b"READY\n")
        connection.sendall(b"done\n")
        self.wait_for_write(lambda values: values == [3])
        os.killpg(process.pid, signal.SIGTERM)
        self.assertEqual(process.wait(timeout=3), 0)
        self.assertEqual(self.writes()[-2:], [4, 0])

    def test_missing_heartbeat_restores(self):
        process, connection = self.start_helper()
        self.assertEqual(connection.recv(256), b"READY\n")
        connection.sendall(b"ask\n")
        process.wait(timeout=6)
        self.assertEqual(self.writes()[-2:], [4, 0])

    def test_magdot_ownership_refused_without_writes(self):
        with self.lock.open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            process, connection = self.start_helper()
            self.assertIn(b"owns the MagSafe LED", connection.recv(256))
            self.assertEqual(process.wait(timeout=3), 1)
            self.assertEqual(self.writes(), [])

    def test_invalid_command_releases_control(self):
        process, connection = self.start_helper()
        self.assertEqual(connection.recv(256), b"READY\n")
        connection.sendall(b"done\n")
        self.wait_for_write(lambda values: values == [3])
        connection.sendall(b"unexpected\n")
        self.assertEqual(process.wait(timeout=3), 1)
        self.assertEqual(self.writes()[-2:], [4, 0])

    def test_disconnect_restores_without_quit(self):
        process, connection = self.start_helper()
        self.assertEqual(connection.recv(256), b"READY\n")
        connection.sendall(b"working\n")
        self.wait_for_write(lambda values: values[:2] == [3, 4])
        connection.close()
        self.assertEqual(process.wait(timeout=3), 0)
        self.assertEqual(self.writes()[-2:], [4, 0])

    def test_python_transport_handshake_and_clean_removal(self):
        native_socket = socket.socket

        class FixtureSocket(native_socket):
            def getsockopt(self, level, option, length=0):
                value = super().getsockopt(level, option, length)
                if level == 0 and option == socket.LOCAL_PEERCRED:
                    value = bytearray(value)
                    struct.pack_into("=I", value, 4, 0)
                    return bytes(value)
                return value

        # A tiny fake daemon exercises the new client-to-service connection.
        import threading
        messages = []
        def serve():
            with self.listener.accept()[0] as connection:
                connection.sendall(b"READY\n")
                stream = connection.makefile("rb")
                for line in stream:
                    messages.append(line.strip())
                    if line == b"quit\n":
                        connection.sendall(b"RESTORED\n")
                        break
        server = threading.Thread(target=serve, daemon=True)
        server.start()
        controller = MagSafeController(connection_reader=lambda: True)
        service = Mock()
        service.status.return_value = 1
        with (
            patch("sidepulse.magsafe.native_service", return_value=service),
            patch("sidepulse.magsafe.MAGSAFE_SOCKET", str(self.address)),
            patch("sidepulse.magsafe.socket.socket", FixtureSocket),
        ):
            controller.update(AgentMode.WORKING)
            controller.start()
            try:
                deadline = time.monotonic() + 3
                while not messages and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(controller.ready)
                self.assertIn(b"working", messages)
            finally:
                controller.stop()
            self.assertFalse(controller.ready)
            self.assertIsNone(controller.last_error)
            service.registerAndReturnError_.assert_not_called()
        server.join(timeout=2)
        self.assertEqual(messages[-1], b"quit")


@unittest.skipUnless(sys.platform == "darwin", "AppKit integration needs macOS")
class MagSafeMenuTests(unittest.TestCase):
    def test_sleep_blocks_queued_refresh_until_wake(self):
        from AppKit import NSApplication
        from sidepulse import status_bar as sb
        NSApplication.sharedApplication()
        settings = replace(AgentMonitorSettings(), magsafe_led_enabled=True)
        with patch.object(sb, "load_settings", return_value=settings):
            controller = sb.StatusBarController.alloc().init()
        controller.magsafe = Mock()
        with patch.object(sb.StatusBarController, "refresh_") as refresh:
            controller.magsafeWillSleep_(None)
            controller.sync_magsafe(AgentMode.WORKING)
            controller.magsafe.stop.assert_called_once_with()
            controller.magsafe.start.assert_not_called()
            controller.magsafe.update.assert_not_called()
            controller.magsafeDidWake_(None)
            self.assertFalse(controller.magsafe_suspended)
            refresh.assert_called_once_with(controller, None)

    def test_agent_only_device_never_becomes_a_usb_target(self):
        from AppKit import NSApplication
        from sidepulse import status_bar as sb
        NSApplication.sharedApplication()
        settings = replace(AgentMonitorSettings(), magsafe_led_enabled=True)
        with patch.object(sb, "load_settings", return_value=settings):
            controller = sb.StatusBarController.alloc().init()
        controller.magsafe = Mock(ready=True, last_error=None)
        with (
            patch.object(sb, "discover_devices", return_value=[]),
            patch.object(sb, "load_ios_links", return_value=()),
            patch.object(sb, "save_settings") as save,
            patch.object(sb, "write_led_program") as write,
        ):
            devices = controller.status_bar_devices()
            self.assertEqual([d.device_id for d in devices], [MAGSAFE_DEVICE_ID])
            menu_item = sb.build_device_menu_item(devices[0], controller)
            self.assertEqual(menu_item.title(), MAGSAFE_DEVICE_NAME)
            titles = [item.title() for item in menu_item.submenu().itemArray()]
            self.assertIn("Agent Status", titles)
            self.assertNotIn("Battery Level", titles)
            self.assertNotIn("Manual", titles)
            self.assertNotIn("Idle: normal charging light", titles)
            self.assertIn("Remove MagSafe 3 LED", titles)
            controller.sync_leds_now(AgentMode.WORKING, None, "agent")
            self.assertEqual(controller.status_keepalive_targets(), [sb.MOUNT_ROOT / name / sb.KEEPALIVE_FILE_NAME for name in sb.STATUS_BAR_KEEPALIVE_VOLUME_NAMES])
            write.assert_not_called()
            save.assert_not_called()
            controller.magsafe.ready = False
            controller.magsafe.status_reason = "MagSafe disconnected"
            controller.magsafe.needs_setup = False
            disconnected = controller.status_bar_devices(remember=False)[0]
            menu_item = sb.build_device_menu_item(disconnected, controller)
            self.assertEqual(menu_item.state(), 1)
            titles = [item.title() for item in menu_item.submenu().itemArray()]
            self.assertIn("MagSafe disconnected", titles)
            self.assertNotIn("Set Up MagSafe Helper…", titles)
