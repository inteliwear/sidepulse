"""Settings remain reachable without a menu bar item."""

import tempfile
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from sidepulse.cli import sidepulse_main
from sidepulse.ipc import HookEventServer, request_settings_window
from sidepulse.settings import AgentMonitorSettings, load_settings, save_settings


class SettingsAccessTests(unittest.TestCase):
    def test_explicit_status_bar_start_restores_hidden_icon_before_launch(self):
        for args in (["status-bar"], ["status-bar", "start"]):
            with self.subTest(args=args), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "settings.json"
                hidden = AgentMonitorSettings().with_menu_bar_icon(False)
                save_settings(hidden, path)

                def launch(**kwargs):
                    self.assertTrue(load_settings(path).show_menu_bar_icon)
                    return Mock(changed=False, started=True, plist_path="test.plist")

                with (
                    patch("sidepulse.settings.default_settings_path", return_value=path),
                    patch("sidepulse.status_bar_launch.install_launch_agent", side_effect=launch) as install,
                ):
                    self.assertEqual(sidepulse_main(args), 0)
                    install.assert_called_once_with(start=True)
                self.assertEqual(load_settings(path), hidden.with_menu_bar_icon(True))

    def test_launch_agent_foreground_start_preserves_hidden_preference(self):
        app = Mock()
        app.main.return_value = 0
        with (
            patch.dict(sys.modules, {"sidepulse.status_bar": app}),
            patch("sidepulse.cli.save_settings") as save,
        ):
            self.assertEqual(sidepulse_main(["status-bar", "--foreground"]), 0)
            app.main.assert_called_once_with()
            save.assert_not_called()

    def test_stopping_status_bar_does_not_change_icon_preference(self):
        with (
            patch("sidepulse.status_bar_launch.uninstall_launch_agent"),
            patch("sidepulse.cli.save_settings") as save,
        ):
            self.assertEqual(sidepulse_main(["status-bar", "stop"]), 0)
            save.assert_not_called()

    def test_icon_visibility_defaults_visible_and_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text("{}")
            self.assertTrue(load_settings(path).show_menu_bar_icon)
            for visible in (False, True):
                settings = AgentMonitorSettings().with_menu_bar_icon(visible)
                save_settings(settings, path)
                self.assertEqual(load_settings(path).show_menu_bar_icon, visible)

    def test_settings_request_is_acknowledged_only_by_a_ui_listener(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            path = Path(directory) / "events.sock"
            self.assertFalse(request_settings_window(socket_path=path))
            for has_ui in (False, True):
                open_settings = Mock()
                on_event = Mock()
                server = HookEventServer(
                    on_event, socket_path=path,
                    on_open_settings=open_settings if has_ui else None,
                )
                try:
                    server.start()
                    self.assertEqual(request_settings_window(socket_path=path), has_ui)
                    self.assertEqual(open_settings.call_count, int(has_ui))
                    on_event.assert_not_called()
                finally:
                    server.stop()
                    server.thread.join(timeout=2)

    @patch("sidepulse.cli.sys.platform", "darwin")
    def test_settings_command_reuses_running_app(self):
        with (
            patch("sidepulse.ipc.request_settings_window", return_value=True),
            patch("sidepulse.status_bar_launch.install_launch_agent") as install,
        ):
            self.assertEqual(sidepulse_main(["settings"]), 0)
            install.assert_not_called()

    @patch("sidepulse.cli.sys.platform", "darwin")
    def test_settings_command_starts_app_when_needed(self):
        with (
            patch("sidepulse.ipc.request_settings_window", side_effect=[False, False, True]),
            patch("sidepulse.status_bar_launch.install_launch_agent") as install,
            patch("sidepulse.cli.time.sleep"),
        ):
            self.assertEqual(sidepulse_main(["settings"]), 0)
            install.assert_called_once_with(start=True)

    @patch("sidepulse.cli.sys.platform", "darwin")
    def test_settings_command_reports_startup_timeout(self):
        with (
            patch("sidepulse.ipc.request_settings_window", return_value=False),
            patch("sidepulse.status_bar_launch.install_launch_agent"),
            patch("sidepulse.cli.time.monotonic", side_effect=[0, 11]),
            patch("sys.stderr"),
        ):
            self.assertEqual(sidepulse_main(["settings"]), 1)
