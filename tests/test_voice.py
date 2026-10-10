from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sidepulse.device_writer import validate_led_text
from sidepulse.voice import VoiceLedOverlay, VoiceMonitor, VoiceRibbon, VoiceSnapshot, program_for_voice


class VoiceTests(unittest.TestCase):
    def test_rainbow_has_eight_distinct_colors_and_respects_firmware_limits(self):
        for level in (0, 0.2, 0.5, 1, -1, 10, float("nan")):
            program = program_for_voice(level, 70)
            validate_led_text(program)
            colors = re.findall(r"#[0-9a-f]{6}", program)
            self.assertEqual(len(colors), 8)
            self.assertEqual(len(set(colors)), 8)
            self.assertIn("brightness 70", program)
        self.assertGreater(int(re.findall(r"#[0-9a-f]{6}", program_for_voice(1))[7][1:], 16),
                           int(re.findall(r"#[0-9a-f]{6}", program_for_voice(0))[7][1:], 16))

    def test_quiet_listening_is_visible_but_below_speech_level(self):
        quiet = re.findall(r"#[0-9a-f]{6}", program_for_voice(0))
        self.assertTrue(all(color != "#000000" for color in quiet))
        loud = re.findall(r"#[0-9a-f]{6}", program_for_voice(1))
        self.assertNotEqual(quiet, loud)

    def test_stale_missing_and_disabled_telemetry_never_hold_voice_display(self):
        monitor = VoiceMonitor()
        with patch("sidepulse.voice.time.monotonic", return_value=10):
            monitor._accept({"active": True, "level": 0.7, "app": "Wispr Flow"})
            self.assertTrue(monitor.snapshot().active)
        with patch("sidepulse.voice.time.monotonic", return_value=10.6):
            self.assertFalse(monitor.snapshot().active)
        monitor.stop()
        self.assertFalse(monitor.snapshot().active)

    def test_untrusted_levels_are_bounded(self):
        monitor = VoiceMonitor()
        monitor._accept({"active": True, "level": 100})
        self.assertEqual(monitor.snapshot().level, 1)
        with self.assertRaises(ValueError):
            monitor._accept({"active": True, "level": float("inf")})

    def test_failed_write_can_retry_and_release_invalidates_controller_cache(self):
        overlay = VoiceLedOverlay()
        target = Path("/Volumes/SidePulse/LEDS.LED")
        with patch("sidepulse.voice.write_led_program", side_effect=OSError("unplugged")):
            with self.assertRaises(OSError):
                overlay.sync(target, VoiceSnapshot(True, 0.7), 255)
        self.assertNotIn(target, overlay.programs)
        with patch("sidepulse.voice.write_led_program") as writer:
            self.assertTrue(overlay.sync(target, VoiceSnapshot(True, 0.7), 255))
            writer.assert_called_once()
        self.assertFalse(overlay.sync(target, VoiceSnapshot(False), 255))
        self.assertTrue(overlay.release(target))
        self.assertFalse(overlay.release(target))

    def test_disconnect_forgets_voice_for_next_reconnect(self):
        overlay = VoiceLedOverlay()
        target = Path("/Volumes/SidePulse/LEDS.LED")
        with patch("sidepulse.voice.write_led_program"):
            overlay.sync(target, VoiceSnapshot(True, 0.5), 255)
        overlay.forget_disconnected(set())
        self.assertFalse(overlay.programs)
        self.assertFalse(overlay.ribbons)

    def test_monitor_death_returns_inactive_even_if_last_frame_was_loud(self):
        monitor = VoiceMonitor()
        monitor._accept({"active": True, "level": 1})
        self.assertTrue(monitor.snapshot().active)
        monitor._accept({"active": False, "error": "Voice helper stopped."})
        self.assertFalse(monitor.snapshot().active)

    def test_syllable_ripple_travels_after_the_speech_attack(self):
        ribbon = VoiceRibbon()
        ribbon.colors(VoiceSnapshot(True, 0), 0)
        center = ribbon.colors(VoiceSnapshot(True, 0.8), 0.2)
        edge = ribbon.colors(VoiceSnapshot(True, 0.2), 0.5)
        # Compare brightness against each LED's own RGB basis: no filled bar.
        self.assertGreater(center[3][1] / 255, center[0][0] / 255)
        self.assertGreater(edge[0][0] / 255, edge[3][1] / 255)

    def test_same_volume_with_different_cadence_changes_motion(self):
        steady, syllables = VoiceRibbon(), VoiceRibbon()
        for step in range(20):
            now = step * 0.05
            steady.colors(VoiceSnapshot(True, 0.6), now)
            syllables.colors(VoiceSnapshot(True, 0.6 if step % 8 < 3 else 0.05), now)
        self.assertNotEqual(steady.colors(VoiceSnapshot(True, 0.6), 1.0),
                            syllables.colors(VoiceSnapshot(True, 0.6), 1.0))

    def test_voice_timbre_changes_motion_at_the_same_volume(self):
        low, high = VoiceRibbon(), VoiceRibbon()
        for step in range(20):
            now = step * 0.05
            level = 0.7 if step % 8 < 3 else 0.02
            low.colors(VoiceSnapshot(True, level, tone=0), now)
            high.colors(VoiceSnapshot(True, level, tone=1), now)
        self.assertNotEqual(low.colors(VoiceSnapshot(True, 0.3, tone=0), 1),
                            high.colors(VoiceSnapshot(True, 0.3, tone=1), 1))

    def test_settings_round_trip_keeps_voice_preference(self):
        from sidepulse.settings import AgentMonitorSettings, load_settings, save_settings

        settings = AgentMonitorSettings().with_voice_visualizer(True)
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "settings.json"
            self.assertFalse(load_settings(path).voice_visualizer_enabled)
            save_settings(settings, path)
            self.assertTrue(load_settings(path).voice_visualizer_enabled)


if __name__ == "__main__":
    unittest.main()
