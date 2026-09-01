"""Provider-neutral AI agent status monitoring."""

from .battery import (
    BatteryLedController,
    BatterySnapshot,
    program_for_battery,
    read_battery_snapshot,
)
from .collector import AgentMonitor, LiveAgentMonitor, MonitorSnapshot, SourceSpec
from .ipc import (
    HookEventServer,
    default_event_socket_path,
    default_latest_state_path,
    send_hook_event,
)
from .led_status import (
    AgentLedController,
    LedDisplayState,
    display_state_for_mode,
    program_for_display_state,
    write_mode_to_leds,
)
from .lid_sleep import (
    ClosedLidAwakeController,
    install_sleep_helper,
    read_lid_closed,
    read_sleep_disabled,
    run_sudo_pmset_disablesleep,
    sleep_helper_install_command,
    sleep_helper_installed,
    uninstall_sleep_helper,
)
from .models import AgentMode, AgentStatus, AggregateStatus, HookEvent

__all__ = [
    "AgentLedController",
    "AgentMode",
    "AgentStatus",
    "AggregateStatus",
    "BatteryLedController",
    "BatterySnapshot",
    "AgentMonitor",
    "LiveAgentMonitor",
    "HookEvent",
    "HookEventServer",
    "LedDisplayState",
    "MonitorSnapshot",
    "SourceSpec",
    "ClosedLidAwakeController",
    "default_event_socket_path",
    "default_latest_state_path",
    "display_state_for_mode",
    "program_for_battery",
    "read_battery_snapshot",
    "read_lid_closed",
    "read_sleep_disabled",
    "install_sleep_helper",
    "run_sudo_pmset_disablesleep",
    "sleep_helper_install_command",
    "sleep_helper_installed",
    "uninstall_sleep_helper",
    "send_hook_event",
    "program_for_display_state",
    "write_mode_to_leds",
]

try:
    from importlib.metadata import version

    __version__ = version("sidepulse")
except Exception:  # pragma: no cover - only an unpackaged source tree.
    __version__ = "0+unknown"
