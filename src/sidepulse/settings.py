from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .battery import DEFAULT_POWER_CHANGE_PREVIEW_SECONDS
from .session_actions import SESSION_OPEN_CHOICES, SESSION_OPEN_TERMINAL


TERMINAL_APP_TERMINAL = "terminal"
TERMINAL_APP_ITERM = "iterm"
TERMINAL_APP_GHOSTTY = "ghostty"
TERMINAL_APP_WARP = "warp"
TERMINAL_APP_KITTY = "kitty"
TERMINAL_APP_WEZTERM = "wezterm"
TERMINAL_APP_ALACRITTY = "alacritty"
TERMINAL_APP_CUSTOM = "custom"
TERMINAL_APP_CHOICES = (
    TERMINAL_APP_TERMINAL,
    TERMINAL_APP_ITERM,
    TERMINAL_APP_GHOSTTY,
    TERMINAL_APP_WARP,
    TERMINAL_APP_KITTY,
    TERMINAL_APP_WEZTERM,
    TERMINAL_APP_ALACRITTY,
    TERMINAL_APP_CUSTOM,
)
LED_DISPLAY_AGENT = "agent"
LED_DISPLAY_BATTERY = "battery"
LED_DISPLAY_CUSTOM = "custom"
LED_DISPLAY_CHOICES = (LED_DISPLAY_AGENT, LED_DISPLAY_BATTERY, LED_DISPLAY_CUSTOM)
SLEEP_PREVENTION_NEVER = "never"
SLEEP_PREVENTION_AGENTS = "agents"
SLEEP_PREVENTION_ALWAYS = "always"
SLEEP_PREVENTION_CHOICES = (
    SLEEP_PREVENTION_NEVER,
    SLEEP_PREVENTION_AGENTS,
    SLEEP_PREVENTION_ALWAYS,
)
CLOSED_LID_AWAKE_NEVER = SLEEP_PREVENTION_NEVER
CLOSED_LID_AWAKE_AGENTS = SLEEP_PREVENTION_AGENTS
CLOSED_LID_AWAKE_ALWAYS = SLEEP_PREVENTION_ALWAYS
CLOSED_LID_AWAKE_CHOICES = SLEEP_PREVENTION_CHOICES
OPEN_LID_AWAKE_NEVER = SLEEP_PREVENTION_NEVER
OPEN_LID_AWAKE_AGENTS = SLEEP_PREVENTION_AGENTS
OPEN_LID_AWAKE_ALWAYS = SLEEP_PREVENTION_ALWAYS
OPEN_LID_AWAKE_CHOICES = SLEEP_PREVENTION_CHOICES
LID_ANIMATION_CLOSED = "closed"
LID_ANIMATION_OPEN = "open"
LID_ANIMATION_CHOICES = (LID_ANIMATION_CLOSED, LID_ANIMATION_OPEN)
DEFAULT_LID_CLOSED_ANIMATION_PROGRAM = "\n".join(
    [
        "off 90ms cosine",
        (
            "0:#FF7A00 180ms ease; 7:#FF7A00 180ms ease; "
            "1:#FF7A00 180ms ease 80ms; 6:#FF7A00 180ms ease 80ms"
        ),
        (
            "2:#FF4A00 180ms ease; 5:#FF4A00 180ms ease; "
            "3:#FF3000 180ms ease 80ms; 4:#FF3000 180ms ease 80ms"
        ),
        "off 360ms ease-out",
    ]
)
DEFAULT_LID_OPEN_ANIMATION_PROGRAM = "\n".join(
    [
        "off 90ms cosine",
        (
            "3:#00E5FF 180ms ease; 4:#00E5FF 180ms ease; "
            "2:#00E5FF 180ms ease 80ms; 5:#00E5FF 180ms ease 80ms"
        ),
        (
            "1:#00FFB0 180ms ease; 6:#00FFB0 180ms ease; "
            "0:#00FF66 180ms ease 80ms; 7:#00FF66 180ms ease 80ms"
        ),
        "#00FF66 220ms ease",
        "off 320ms ease-out",
    ]
)
DEFAULT_LID_CLOSED_ANIMATION_SECONDS = 0.9
DEFAULT_LID_OPEN_ANIMATION_SECONDS = 1.0
DEFAULT_RECENT_SESSION_RETENTION_SECONDS = 48 * 60 * 60
DEFAULT_IDLE_TIMEOUT_SECONDS = 60 * 60
DEFAULT_SLEEP_PREVENTION_MIN_BATTERY_PERCENT = 20.0
HISTORY_TIMEFRAME_1H_SECONDS = 60 * 60
HISTORY_TIMEFRAME_6H_SECONDS = 6 * 60 * 60
HISTORY_TIMEFRAME_12H_SECONDS = 12 * 60 * 60
HISTORY_TIMEFRAME_24H_SECONDS = 24 * 60 * 60
HISTORY_TIMEFRAME_48H_SECONDS = 48 * 60 * 60
DEFAULT_HISTORY_TIMEFRAME_SECONDS = HISTORY_TIMEFRAME_12H_SECONDS
HISTORY_TIMEFRAME_CHOICES = (
    HISTORY_TIMEFRAME_1H_SECONDS,
    HISTORY_TIMEFRAME_6H_SECONDS,
    HISTORY_TIMEFRAME_12H_SECONDS,
    HISTORY_TIMEFRAME_24H_SECONDS,
    HISTORY_TIMEFRAME_48H_SECONDS,
)


@dataclass(frozen=True)
class LedAnimationSetting:
    program: str
    duration_seconds: float

    def to_dict(self) -> dict[str, object]:
        return {
            "program": self.program,
            "duration_seconds": self.duration_seconds,
        }


@dataclass(frozen=True)
class DeviceDisplaySetting:
    device_id: str
    name: str
    path: str
    led_display: str = LED_DISPLAY_AGENT
    brightness: int = 255

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.device_id,
            "name": self.name,
            "path": self.path,
            "led_display": self.led_display,
            "brightness": self.brightness,
        }


@dataclass(frozen=True)
class AgentMonitorSettings:
    codex_transcripts_enabled: bool = False
    claude_transcripts_enabled: bool = False
    led_display: str = LED_DISPLAY_AGENT
    devices: tuple[DeviceDisplaySetting, ...] = ()
    sleep_prevention_policy: str = SLEEP_PREVENTION_AGENTS
    virtual_status_device_enabled: bool = False
    closed_lid_system_override_enabled: bool = False
    lid_closed_animation: LedAnimationSetting = field(
        default_factory=lambda: default_lid_animation(LID_ANIMATION_CLOSED)
    )
    lid_open_animation: LedAnimationSetting = field(
        default_factory=lambda: default_lid_animation(LID_ANIMATION_OPEN)
    )
    battery_full_charge_watts: float | None = None
    battery_show_on_power_change: bool = True
    kitt_mode_enabled: bool = False
    battery_power_change_preview_seconds: float = DEFAULT_POWER_CHANGE_PREVIEW_SECONDS
    session_open_preferences: dict[str, str] = field(default_factory=dict)
    grok_session_open_action: str = SESSION_OPEN_TERMINAL
    session_terminal_app: str = TERMINAL_APP_TERMINAL
    custom_terminal_path: str = ""
    recent_session_retention_seconds: float = DEFAULT_RECENT_SESSION_RETENTION_SECONDS
    idle_timeout_seconds: float = DEFAULT_IDLE_TIMEOUT_SECONDS
    sleep_prevention_min_battery_percent: float = DEFAULT_SLEEP_PREVENTION_MIN_BATTERY_PERCENT
    history_timeframe_seconds: float = DEFAULT_HISTORY_TIMEFRAME_SECONDS
    setup_screen_completed: bool = False

    def transcript_enabled(self, provider: str) -> bool:
        if provider == "codex":
            return self.codex_transcripts_enabled
        if provider == "claude":
            return self.claude_transcripts_enabled
        return False

    def with_transcript_provider(self, provider: str, enabled: bool) -> "AgentMonitorSettings":
        if provider == "codex":
            return replace(self, codex_transcripts_enabled=enabled)
        if provider == "claude":
            return replace(self, claude_transcripts_enabled=enabled)
        raise ValueError(f"Unknown transcript provider: {provider}")

    def with_led_display(self, display: str) -> "AgentMonitorSettings":
        if display not in LED_DISPLAY_CHOICES:
            raise ValueError(f"Unknown LED display: {display}")
        return replace(self, led_display=display)

    def display_for_device(self, device_id: str) -> str:
        for device in self.devices:
            if device.device_id == device_id:
                return device.led_display
        return self.led_display

    def brightness_for_device(self, device_id: str) -> int:
        for device in self.devices:
            if device.device_id == device_id:
                return normalize_brightness(device.brightness)
        return 255

    def with_device_display(
        self,
        device_id: str,
        display: str,
        *,
        name: str | None = None,
        path: str | None = None,
    ) -> "AgentMonitorSettings":
        if display not in LED_DISPLAY_CHOICES:
            raise ValueError(f"Unknown LED display: {display}")

        devices: list[DeviceDisplaySetting] = []
        updated = False
        for device in self.devices:
            if device.device_id == device_id:
                devices.append(
                    DeviceDisplaySetting(
                        device_id=device.device_id,
                        name=name or device.name,
                        path=path or device.path,
                        led_display=display,
                        brightness=device.brightness,
                    )
                )
                updated = True
            else:
                devices.append(device)
        if not updated:
            devices.append(
                DeviceDisplaySetting(
                    device_id=device_id,
                    name=name or device_id,
                    path=path or device_id,
                    led_display=display,
                    brightness=self.brightness_for_device(device_id),
                )
            )
        return replace(self, devices=tuple(devices))

    def with_device_brightness(
        self,
        device_id: str,
        brightness: int | float,
        *,
        name: str | None = None,
        path: str | None = None,
    ) -> "AgentMonitorSettings":
        value = normalize_brightness(brightness)
        devices: list[DeviceDisplaySetting] = []
        updated = False
        for device in self.devices:
            if device.device_id == device_id:
                devices.append(
                    DeviceDisplaySetting(
                        device_id=device.device_id,
                        name=name or device.name,
                        path=path or device.path,
                        led_display=device.led_display,
                        brightness=value,
                    )
                )
                updated = True
            else:
                devices.append(device)
        if not updated:
            devices.append(
                DeviceDisplaySetting(
                    device_id=device_id,
                    name=name or device_id,
                    path=path or device_id,
                    led_display=self.display_for_device(device_id),
                    brightness=value,
                )
            )
        return replace(self, devices=tuple(devices))

    def with_remembered_device(
        self,
        *,
        device_id: str,
        name: str,
        path: str,
    ) -> "AgentMonitorSettings":
        return self.with_device_display(
            device_id,
            self.display_for_device(device_id),
            name=name,
            path=path,
        )

    def without_device(self, device_id: str) -> "AgentMonitorSettings":
        devices = tuple(device for device in self.devices if device.device_id != device_id)
        if devices == self.devices:
            return self
        return replace(self, devices=devices)

    def session_open_action(self, provider: str, origin: str | None = None) -> str | None:
        provider_key = provider.lower()
        if origin:
            action = self.session_open_preferences.get(
                session_open_preference_key(provider_key, origin)
            )
            if action in SESSION_OPEN_CHOICES:
                return action
            action = self.session_open_preferences.get(
                f"origin:{normalize_session_origin_key(origin)}"
            )
            if action in SESSION_OPEN_CHOICES:
                return action

        if provider_key == "grok":
            action = self.grok_session_open_action
            if action in SESSION_OPEN_CHOICES:
                return action

        action = self.session_open_preferences.get(provider_key)
        if action in SESSION_OPEN_CHOICES:
            return action
        return None

    def with_session_open_action(
        self,
        provider: str,
        action: str,
        origin: str | None = None,
    ) -> "AgentMonitorSettings":
        if action not in SESSION_OPEN_CHOICES:
            raise ValueError(f"Unknown session open action: {action}")
        if provider.lower() == "grok" and not origin:
            return self.with_provider_session_open_action(provider, action)
        key = session_open_preference_key(provider, origin)
        preferences = dict(self.session_open_preferences)
        preferences[key] = action
        return replace(self, session_open_preferences=preferences)

    def with_provider_session_open_action(
        self, provider: str, action: str
    ) -> "AgentMonitorSettings":
        """Set the provider-wide opener and discard older per-origin overrides."""
        if action not in SESSION_OPEN_CHOICES:
            raise ValueError(f"Unknown session open action: {action}")
        provider_key = provider.lower()
        prefix = f"origin:{provider_key}:"
        preferences = {
            key: value
            for key, value in self.session_open_preferences.items()
            if key != provider_key and not key.startswith(prefix)
        }
        if provider_key == "grok":
            return replace(
                self,
                session_open_preferences=preferences,
                grok_session_open_action=action,
            )
        preferences[provider_key] = action
        return replace(self, session_open_preferences=preferences)

    def with_session_terminal(
        self,
        terminal_app: str,
        custom_path: str | None = None,
    ) -> "AgentMonitorSettings":
        terminal = normalize_terminal_app(terminal_app)
        path = self.custom_terminal_path
        if custom_path is not None:
            path = str(custom_path)
        if terminal != TERMINAL_APP_CUSTOM:
            path = self.custom_terminal_path
        return replace(
            self,
            session_terminal_app=terminal,
            custom_terminal_path=path,
        )

    def with_battery_full_charge_watts(self, watts: float | None) -> "AgentMonitorSettings":
        if watts is not None and watts <= 0:
            watts = None
        return replace(self, battery_full_charge_watts=watts)

    def with_battery_power_change_preview(
        self,
        *,
        enabled: bool | None = None,
        seconds: float | None = None,
    ) -> "AgentMonitorSettings":
        preview_seconds = self.battery_power_change_preview_seconds
        if seconds is not None:
            preview_seconds = max(0.0, float(seconds))
        return replace(
            self,
            battery_show_on_power_change=(
                self.battery_show_on_power_change if enabled is None else enabled
            ),
            battery_power_change_preview_seconds=preview_seconds,
        )

    def lid_animation(self, kind: str) -> LedAnimationSetting:
        if kind == LID_ANIMATION_CLOSED:
            return self.lid_closed_animation
        if kind == LID_ANIMATION_OPEN:
            return self.lid_open_animation
        raise ValueError(f"Unknown lid animation: {kind}")

    def with_closed_lid_awake_policy(self, policy: str) -> "AgentMonitorSettings":
        return self.with_sleep_prevention_policy(policy)

    def with_open_lid_awake_policy(self, policy: str) -> "AgentMonitorSettings":
        return self.with_sleep_prevention_policy(policy)

    def with_sleep_prevention_policy(self, policy: str) -> "AgentMonitorSettings":
        if policy not in SLEEP_PREVENTION_CHOICES:
            raise ValueError(f"Unknown sleep prevention policy: {policy}")
        return replace(self, sleep_prevention_policy=policy)

    def with_closed_lid_system_override(self, enabled: bool) -> "AgentMonitorSettings":
        return replace(self, closed_lid_system_override_enabled=bool(enabled))

    def with_lid_animation(
        self,
        kind: str,
        *,
        program: str,
        duration_seconds: float,
    ) -> "AgentMonitorSettings":
        animation = LedAnimationSetting(
            program=program,
            duration_seconds=normalize_animation_duration(duration_seconds),
        )
        if kind == LID_ANIMATION_CLOSED:
            return replace(self, lid_closed_animation=animation)
        if kind == LID_ANIMATION_OPEN:
            return replace(self, lid_open_animation=animation)
        raise ValueError(f"Unknown lid animation: {kind}")

    def with_kitt_mode(self, enabled: bool) -> "AgentMonitorSettings":
        return replace(self, kitt_mode_enabled=bool(enabled))

    def with_setup_screen_completed(self, completed: bool = True) -> "AgentMonitorSettings":
        return replace(self, setup_screen_completed=bool(completed))

    def with_virtual_status_device(self, enabled: bool) -> "AgentMonitorSettings":
        return replace(self, virtual_status_device_enabled=bool(enabled))

    def with_agent_list_timing(
        self,
        *,
        recent_session_retention_seconds: float | None = None,
        idle_timeout_seconds: float | None = None,
    ) -> "AgentMonitorSettings":
        retention = self.recent_session_retention_seconds
        idle_timeout = self.idle_timeout_seconds
        if recent_session_retention_seconds is not None:
            retention = normalize_seconds_setting(recent_session_retention_seconds)
        if idle_timeout_seconds is not None:
            idle_timeout = normalize_seconds_setting(idle_timeout_seconds)
        return replace(
            self,
            recent_session_retention_seconds=retention,
            idle_timeout_seconds=idle_timeout,
        )

    def with_sleep_prevention_battery_safeguard(self, percent: float) -> "AgentMonitorSettings":
        return replace(
            self,
            sleep_prevention_min_battery_percent=normalize_percent_setting(percent),
        )

    def with_history_timeframe(self, seconds: float) -> "AgentMonitorSettings":
        return replace(self, history_timeframe_seconds=normalize_history_timeframe(seconds))

    def to_dict(self) -> dict[str, Any]:
        return {
            "led_display": self.led_display,
            "devices": [device.to_dict() for device in self.devices],
            "sleep_prevention_policy": self.sleep_prevention_policy,
            "virtual_status_device_enabled": self.virtual_status_device_enabled,
            "closed_lid_system_override_enabled": self.closed_lid_system_override_enabled,
            "lid_closed_animation": self.lid_closed_animation.to_dict(),
            "lid_open_animation": self.lid_open_animation.to_dict(),
            "transcript_monitoring": {
                "codex": self.codex_transcripts_enabled,
                "claude": self.claude_transcripts_enabled,
            },
            "battery_monitoring": {
                "full_charge_watts": self.battery_full_charge_watts,
                "show_on_power_change": self.battery_show_on_power_change,
                "power_change_preview_seconds": self.battery_power_change_preview_seconds,
            },
            "session_open_preferences": dict(sorted(self.session_open_preferences.items())),
            "grok_session_open_action": self.grok_session_open_action,
            "session_terminal": {
                "app": self.session_terminal_app,
                "custom_path": self.custom_terminal_path,
            },
            "agent_list": {
                "recent_session_retention_seconds": self.recent_session_retention_seconds,
                "idle_timeout_seconds": self.idle_timeout_seconds,
            },
            "sleep_prevention": {
                "min_battery_percent": self.sleep_prevention_min_battery_percent,
            },
            "history": {
                "timeframe_seconds": self.history_timeframe_seconds,
            },
            "kitt_mode_enabled": self.kitt_mode_enabled,
            "setup_screen_completed": self.setup_screen_completed,
        }


def default_config_dir(home: Path | None = None) -> Path:
    if home is None:
        xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
        if xdg_config_home:
            return Path(xdg_config_home).expanduser() / "sidepulse" / "agent-monitor"

    base = home or Path.home()
    return base / ".config" / "sidepulse" / "agent-monitor"


def default_settings_path(home: Path | None = None) -> Path:
    return default_config_dir(home) / "settings.json"


def load_settings(path: Path | None = None) -> AgentMonitorSettings:
    target = (path or default_settings_path()).expanduser()
    if not target.exists():
        return AgentMonitorSettings()

    try:
        data = json.loads(target.read_text())
    except Exception:
        return AgentMonitorSettings()

    if not isinstance(data, dict):
        return AgentMonitorSettings()

    transcript = data.get("transcript_monitoring")
    if not isinstance(transcript, dict):
        transcript = {}

    battery = data.get("battery_monitoring")
    if not isinstance(battery, dict):
        battery = {}

    terminal = data.get("session_terminal")
    if not isinstance(terminal, dict):
        terminal = {}

    agent_list = data.get("agent_list")
    if not isinstance(agent_list, dict):
        agent_list = {}

    sleep_prevention = data.get("sleep_prevention")
    if not isinstance(sleep_prevention, dict):
        sleep_prevention = {}

    history = data.get("history")
    if not isinstance(history, dict):
        history = {}

    led_display = _led_display_setting(data.get("led_display"), LED_DISPLAY_AGENT)
    session_open_preferences = _session_open_preferences(
        data.get("session_open_preferences")
    )
    grok_session_open_action = _session_open_action_setting(
        data.get("grok_session_open_action")
    )
    if grok_session_open_action is None:
        grok_session_open_action = session_open_preferences.get(
            "grok",
            SESSION_OPEN_TERMINAL,
        )
    session_open_preferences.pop("grok", None)
    return AgentMonitorSettings(
        codex_transcripts_enabled=_bool_setting(transcript.get("codex"), False),
        claude_transcripts_enabled=_bool_setting(transcript.get("claude"), False),
        led_display=led_display,
        devices=_device_display_settings(data.get("devices"), led_display),
        sleep_prevention_policy=_sleep_prevention_policy_from_settings(data),
        virtual_status_device_enabled=_bool_setting(
            data.get("virtual_status_device_enabled"), False
        ),
        closed_lid_system_override_enabled=_bool_setting(
            data.get("closed_lid_system_override_enabled"),
            False,
        ),
        lid_closed_animation=_lid_animation_setting(
            data.get("lid_closed_animation"),
            default_lid_animation(LID_ANIMATION_CLOSED),
        ),
        lid_open_animation=_lid_animation_setting(
            data.get("lid_open_animation"),
            default_lid_animation(LID_ANIMATION_OPEN),
        ),
        battery_full_charge_watts=_optional_float_setting(
            battery.get("full_charge_watts"),
        ),
        battery_show_on_power_change=_bool_setting(
            battery.get("show_on_power_change"),
            True,
        ),
        battery_power_change_preview_seconds=_float_setting(
            battery.get("power_change_preview_seconds"),
            DEFAULT_POWER_CHANGE_PREVIEW_SECONDS,
        ),
        session_open_preferences=session_open_preferences,
        grok_session_open_action=grok_session_open_action,
        session_terminal_app=normalize_terminal_app(terminal.get("app")),
        custom_terminal_path=_string_setting(terminal.get("custom_path")),
        recent_session_retention_seconds=_nonnegative_float_setting(
            agent_list.get(
                "recent_session_retention_seconds",
                data.get("recent_session_retention_seconds"),
            ),
            DEFAULT_RECENT_SESSION_RETENTION_SECONDS,
        ),
        idle_timeout_seconds=_nonnegative_float_setting(
            agent_list.get("idle_timeout_seconds", data.get("idle_timeout_seconds")),
            DEFAULT_IDLE_TIMEOUT_SECONDS,
        ),
        sleep_prevention_min_battery_percent=normalize_percent_setting(
            sleep_prevention.get(
                "min_battery_percent",
                data.get(
                    "sleep_prevention_min_battery_percent",
                    DEFAULT_SLEEP_PREVENTION_MIN_BATTERY_PERCENT,
                ),
            )
        ),
        history_timeframe_seconds=normalize_history_timeframe(
            history.get(
                "timeframe_seconds",
                data.get("history_timeframe_seconds", DEFAULT_HISTORY_TIMEFRAME_SECONDS),
            )
        ),
        kitt_mode_enabled=_bool_setting(data.get("kitt_mode_enabled"), False),
        setup_screen_completed=_bool_setting(data.get("setup_screen_completed"), False),
    )


def save_settings(
    settings: AgentMonitorSettings,
    path: Path | None = None,
) -> Path:
    target = (path or default_settings_path()).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(settings.to_dict(), indent=2, sort_keys=True) + "\n")
    return target


def _bool_setting(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    return default


def _led_display_setting(value: object, default: str) -> str:
    if isinstance(value, str) and value in LED_DISPLAY_CHOICES:
        return value
    return default


def normalize_terminal_app(value: object) -> str:
    if isinstance(value, str) and value in TERMINAL_APP_CHOICES:
        return value
    return TERMINAL_APP_TERMINAL


def _string_setting(value: object) -> str:
    return value if isinstance(value, str) else ""


def _sleep_prevention_policy(value: object, default: str = SLEEP_PREVENTION_AGENTS) -> str:
    if isinstance(value, str) and value in SLEEP_PREVENTION_CHOICES:
        return value
    return default


def _sleep_prevention_policy_from_settings(data: dict[str, Any]) -> str:
    direct = _sleep_prevention_policy(data.get("sleep_prevention_policy"), "")
    if direct:
        return direct

    legacy_values = (
        data.get("open_lid_awake_policy"),
        data.get("closed_lid_awake_policy"),
    )
    legacy_policies = [
        _sleep_prevention_policy(value, "")
        for value in legacy_values
    ]
    if SLEEP_PREVENTION_ALWAYS in legacy_policies:
        return SLEEP_PREVENTION_ALWAYS
    if SLEEP_PREVENTION_AGENTS in legacy_policies:
        return SLEEP_PREVENTION_AGENTS
    if SLEEP_PREVENTION_NEVER in legacy_policies:
        return SLEEP_PREVENTION_NEVER
    return SLEEP_PREVENTION_AGENTS


def _device_display_settings(value: object, default_display: str) -> tuple[DeviceDisplaySetting, ...]:
    if not isinstance(value, list):
        return ()

    devices: list[DeviceDisplaySetting] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        device_id = item.get("id")
        path = item.get("path")
        if not isinstance(device_id, str) or not device_id:
            continue
        if not isinstance(path, str) or not path:
            path = device_id
        if device_id in seen:
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name:
            name = Path(path).name or device_id
        display = _led_display_setting(item.get("led_display"), default_display)
        brightness = normalize_brightness(item.get("brightness"))
        devices.append(
            DeviceDisplaySetting(
                device_id=device_id,
                name=name,
                path=path,
                led_display=display,
                brightness=brightness,
            )
        )
        seen.add(device_id)
    return tuple(devices)


def _session_open_preferences(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    for provider, action in value.items():
        if not isinstance(provider, str) or not isinstance(action, str):
            continue
        if action not in SESSION_OPEN_CHOICES:
            continue
        result[provider.lower()] = action
    return result


def _session_open_action_setting(value: object) -> str | None:
    if isinstance(value, str) and value in SESSION_OPEN_CHOICES:
        return value
    return None


def session_open_preference_key(provider: str, origin: str | None = None) -> str:
    if origin:
        return f"origin:{provider.lower()}:{normalize_session_origin_key(origin)}"
    return provider.lower()


def normalize_session_origin_key(origin: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", origin.strip().lower()).strip("_")
    return normalized or "unknown"


def _optional_float_setting(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    return None


def _float_setting(value: object, default: float) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    return default


def _nonnegative_float_setting(value: object, default: float) -> float:
    if isinstance(value, (int, float)):
        return normalize_seconds_setting(value)
    return default


def normalize_seconds_setting(value: object) -> float:
    if isinstance(value, (int, float)):
        return max(0.0, float(value))
    return 0.0


def normalize_percent_setting(value: object) -> float:
    if isinstance(value, (int, float)):
        return max(0.0, min(100.0, float(value)))
    return DEFAULT_SLEEP_PREVENTION_MIN_BATTERY_PERCENT


def normalize_history_timeframe(value: object) -> float:
    if isinstance(value, (int, float)):
        candidate = float(value)
        for choice in HISTORY_TIMEFRAME_CHOICES:
            if abs(candidate - float(choice)) < 0.5:
                return float(choice)
    return float(DEFAULT_HISTORY_TIMEFRAME_SECONDS)


def normalize_brightness(value: object) -> int:
    if value is None:
        return 255
    if isinstance(value, (int, float)):
        return max(0, min(255, int(round(float(value)))))
    return 255


def default_lid_animation(kind: str) -> LedAnimationSetting:
    if kind == LID_ANIMATION_CLOSED:
        return LedAnimationSetting(
            program=DEFAULT_LID_CLOSED_ANIMATION_PROGRAM,
            duration_seconds=DEFAULT_LID_CLOSED_ANIMATION_SECONDS,
        )
    if kind == LID_ANIMATION_OPEN:
        return LedAnimationSetting(
            program=DEFAULT_LID_OPEN_ANIMATION_PROGRAM,
            duration_seconds=DEFAULT_LID_OPEN_ANIMATION_SECONDS,
        )
    raise ValueError(f"Unknown lid animation: {kind}")


def normalize_animation_duration(value: object) -> float:
    if not isinstance(value, (int, float)):
        return 1.0
    return max(0.1, min(10.0, float(value)))


def _lid_animation_setting(
    value: object,
    default: LedAnimationSetting,
) -> LedAnimationSetting:
    if not isinstance(value, dict):
        return default
    program = value.get("program")
    if not isinstance(program, str) or not program.strip():
        program = default.program
    duration = value.get("duration_seconds")
    if not isinstance(duration, (int, float)):
        duration = default.duration_seconds
    return LedAnimationSetting(
        program=program,
        duration_seconds=normalize_animation_duration(duration),
    )
