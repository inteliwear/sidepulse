from __future__ import annotations

import pytest

from sidepulse.settings import (
    MENU_BAR_ANIMATION_OFF,
    MENU_BAR_ANIMATION_REDUCED,
    MENU_BAR_ANIMATION_SMOOTH,
    AgentMonitorSettings,
    load_settings,
    save_settings,
)


def test_menu_bar_animation_defaults_to_smooth_and_round_trips(tmp_path) -> None:
    path = tmp_path / "settings.json"
    assert AgentMonitorSettings().menu_bar_icon_animation == MENU_BAR_ANIMATION_SMOOTH
    for mode in (MENU_BAR_ANIMATION_REDUCED, MENU_BAR_ANIMATION_OFF, MENU_BAR_ANIMATION_SMOOTH):
        save_settings(AgentMonitorSettings().with_menu_bar_icon_animation(mode), path)
        assert load_settings(path).menu_bar_icon_animation == mode


def test_unknown_menu_bar_animation_is_rejected_or_ignored(tmp_path) -> None:
    with pytest.raises(ValueError):
        AgentMonitorSettings().with_menu_bar_icon_animation("disco")
    path = tmp_path / "settings.json"
    path.write_text('{"menu_bar_icon_animation": "disco"}', encoding="utf-8")
    assert load_settings(path).menu_bar_icon_animation == MENU_BAR_ANIMATION_SMOOTH
