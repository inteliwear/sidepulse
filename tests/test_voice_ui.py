"""Exercise the real companion's voice-to-normal display handoff without hardware."""
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

if sys.platform != "darwin":
    raise unittest.SkipTest("AppKit integration requires macOS")

from AppKit import NSApplication
from sidepulse import status_bar as sb
from sidepulse.battery import BatterySnapshot, program_for_battery
from sidepulse.models import AgentMode
from sidepulse.led_status import display_state_for_mode
from sidepulse.settings import AgentMonitorSettings


class VoiceDisplayHandoffTests(unittest.TestCase):
    def setUp(self):
        NSApplication.sharedApplication()
        with patch.object(sb, "load_settings", return_value=AgentMonitorSettings(voice_visualizer_enabled=True)):
            self.controller = sb.StatusBarController.alloc().init()
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name) / "SidePulsePro"
        self.root.mkdir()
        self.target = self.root / "LEDS.LED"
        self.target.write_text("off\n")
        self.device = sb.StatusBarDevice("test-pro", "SidePulse Pro", self.root, self.target, True, sb.LED_DISPLAY_AGENT)
        self.device_patch = patch.object(self.controller, "status_bar_devices", side_effect=lambda **kw: [self.device])
        self.device_patch.start()
        self.addCleanup(self.device_patch.stop)

    def sync(self, mode, battery=None):
        self.controller.sync_leds_now(mode, battery, self.device.display)

    def test_stop_restores_same_agent_mode_even_when_controller_cached_it(self):
        self.sync(AgentMode.WORKING)
        before = self.target.read_text()
        self.controller.voice_monitor._accept({"active": True, "level": 0.8})
        self.sync(AgentMode.WORKING)
        self.assertNotEqual(self.target.read_text(), before)
        self.controller.voice_monitor._accept({"active": False})
        self.sync(AgentMode.WORKING)
        self.assertEqual(self.target.read_text(), before)

    def test_agent_change_during_dictation_is_restored_to_latest_mode(self):
        self.controller.voice_monitor._accept({"active": True, "level": 0.8})
        self.sync(AgentMode.WORKING)
        self.sync(AgentMode.WAITING_FOR_INPUT)
        voice = self.target.read_text()
        self.controller.voice_monitor._accept({"active": False})
        self.sync(AgentMode.WAITING_FOR_INPUT)
        self.assertNotEqual(self.target.read_text(), voice)
        self.assertEqual(self.controller.agent_controller_for_device(self.device).last_state,
                         display_state_for_mode(AgentMode.WAITING_FOR_INPUT))

    def test_stop_restores_latest_battery_not_pre_dictation_level(self):
        self.device = replace(self.device, display=sb.LED_DISPLAY_BATTERY)
        self.sync(AgentMode.IDLE_READY, BatterySnapshot(percent=30))
        self.controller.voice_monitor._accept({"active": True, "level": 0.4})
        self.sync(AgentMode.IDLE_READY, BatterySnapshot(percent=30))
        self.controller.voice_monitor._accept({"active": False})
        battery = BatterySnapshot(percent=70)
        self.sync(AgentMode.IDLE_READY, battery)
        self.assertEqual(self.target.read_text(), program_for_battery(battery, led_count=8, brightness=255))

    def test_stale_or_disabled_voice_returns_control(self):
        self.controller.voice_monitor._accept({"active": True, "level": 0.6})
        self.sync(AgentMode.COMPLETED)
        self.controller.settings = self.controller.settings.with_voice_visualizer(False)
        self.sync(AgentMode.COMPLETED)
        self.assertFalse(self.controller.voice_overlay.programs)
        self.assertNotIn("50ms linear", self.target.read_text())

    def test_dot_and_manual_output_are_not_taken_over(self):
        self.controller.voice_monitor._accept({"active": True, "level": 1})
        self.device = replace(self.device, display=sb.LED_DISPLAY_CUSTOM)
        self.sync(AgentMode.WORKING)
        self.assertEqual(self.target.read_text(), "off\n")
        self.device = replace(self.device, target=Path(self.scratch.name) / "PulseDot" / "LEDS.LED", display=sb.LED_DISPLAY_AGENT)
        self.sync(AgentMode.WORKING)
        self.assertFalse(self.controller.voice_overlay.programs)
        self.assertNotIn("50ms linear", self.device.target.read_text())


if __name__ == "__main__":
    unittest.main()
