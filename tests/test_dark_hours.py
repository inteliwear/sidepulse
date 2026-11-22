import json
from dataclasses import replace
from datetime import datetime

import pytest

from sidepulse.settings import AgentMonitorSettings, DarkHours, load_settings, save_settings


@pytest.mark.parametrize("start,end,local,active", [
    (0, 390, "2026-11-22 00:00", True),     # Sunday
    (0, 390, "2026-11-22 06:29", True),
    (0, 390, "2026-11-22 06:30", False),
    (1380, 390, "2026-11-22 06:00", False),  # Saturday was not selected
    (1380, 390, "2026-11-22 23:00", True),
    (1380, 390, "2026-11-23 06:29", True),   # Sunday night continues into Monday
    (1380, 390, "2026-11-23 06:30", False),
    (600, 600, "2026-11-22 09:59", False),
    (600, 600, "2026-11-23 09:59", True),
    (600, 600, "2026-11-23 10:00", False),
])
def test_schedule_uses_local_clock_and_start_day(start, end, local, active):
    schedule = DarkHours(enabled=True, start_minute=start, end_minute=end, weekdays=(6,))
    now = datetime.fromisoformat(local)
    assert schedule.is_active(now) is active
    assert not replace(schedule, enabled=False).is_active(now)
    assert not replace(schedule, weekdays=()).is_active(now)


def test_settings_round_trip_and_legacy_defaults(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{}")
    assert not load_settings(path).dark_hours.enabled
    schedule = DarkHours(True, 1400, 371, (4, 6), True, True)
    save_settings(AgentMonitorSettings(dark_hours=schedule), path)
    assert load_settings(path).dark_hours == schedule
    path.write_text(json.dumps({"dark_hours": {"enabled": "yes", "end_minute": 1440,
                                               "weekdays": [True, -1, 2, 7]}}))
    assert load_settings(path).dark_hours == DarkHours(weekdays=(2,))
