"""CLI diagnostics for the Hermes SidePulse plugin."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .bridge import PluginSettings, emit_hook

_TEST_HOOKS = {
    "idle": ("on_session_start", {}),
    "working": ("pre_llm_call", {}),
    "tool": ("pre_tool_call", {"tool_name": "sidepulse-test"}),
    "ask": ("pre_approval_request", {}),
    "done": ("post_llm_call", {"assistant_response": "<!-- sidepulse:done -->"}),
    "error": ("api_request_error", {}),
}


def doctor_report(settings: PluginSettings) -> dict[str, Any]:
    state_dir = settings.resolved_state_dir()
    socket_path = settings.resolved_socket_path()
    event_log = state_dir / f"{settings.provider}.jsonl"
    latest_state = state_dir / "latest.json"
    volumes = Path("/Volumes")
    devices = []
    if volumes.is_dir():
        for candidate in sorted(volumes.iterdir()):
            if (
                candidate.name.lower().startswith("sidepulse")
                or (candidate / "LEDS.LED").is_file()
            ):
                devices.append(str(candidate))
    return {
        "agent_id": settings.agent_id,
        "provider": settings.provider,
        "state_dir": str(state_dir),
        "event_log_path": str(event_log),
        "event_log_exists": event_log.is_file(),
        "latest_state_path": str(latest_state),
        "latest_state_exists": latest_state.is_file(),
        "socket_path": str(socket_path),
        "socket_exists": socket_path.exists(),
        "sidepulse_command": shutil.which("sidepulse"),
        "devices": devices,
    }


def setup_cli(parser) -> None:
    subcommands = parser.add_subparsers(dest="sidepulse_command")
    doctor = subcommands.add_parser("doctor", help="Inspect SidePulse transport health")
    doctor.add_argument("--json", action="store_true", dest="as_json")
    test = subcommands.add_parser("test", help="Emit a synthetic SidePulse status")
    test.add_argument("--mode", choices=sorted(_TEST_HOOKS), default="working")
    test.add_argument("--json", action="store_true", dest="as_json")
    compat = subcommands.add_parser(
        "compat",
        help="Check that SidePulse still works after a Hermes update",
    )
    compat.add_argument("--json", action="store_true", dest="as_json")
    compat.add_argument("--update-baseline", action="store_true")
    parser.set_defaults(sidepulse_command="doctor", as_json=False)


def _test_report(settings: PluginSettings, mode: str) -> dict[str, Any]:
    hook_name, extra = _TEST_HOOKS[mode]
    result = emit_hook(
        hook_name,
        {"platform": "cli", "session_id": "hermes-sidepulse-test", **extra},
        settings,
    )
    return {
        "requested_mode": mode,
        "mode": (result.event or {}).get("sidepulse_mode"),
        "logged": result.logged,
        "delivered": result.delivered,
    }


def _compat_script() -> Path | None:
    here = Path(__file__).resolve().parent
    candidate = here / "scripts" / "check_hermes_compat.py"
    return candidate if candidate.is_file() else None


def handle_cli(args, settings: PluginSettings) -> None:
    if getattr(args, "sidepulse_command", "doctor") == "compat":
        script = _compat_script()
        if script is None:
            print("compat script missing: integrations/hermes/scripts/check_hermes_compat.py")
            raise SystemExit(1)
        command = [sys.executable, str(script)]
        if getattr(args, "as_json", False):
            command.append("--json")
        if getattr(args, "update_baseline", False):
            command.append("--update-baseline")
        raise SystemExit(subprocess.call(command))

    if getattr(args, "sidepulse_command", "doctor") == "test":
        extra = dict(_TEST_HOOKS[getattr(args, "mode", "working")][1])
        hook_name = _TEST_HOOKS[getattr(args, "mode", "working")][0]
        payload = {
            "platform": "cli",
            "session_id": "hermes-sidepulse-test",
            **extra,
        }
        result = emit_hook(hook_name, payload, settings)
        report = {
            "requested_mode": getattr(args, "mode", "working"),
            "mode": (result.event or {}).get("sidepulse_mode"),
            "logged": result.logged,
            "delivered": result.delivered,
        }
        if getattr(args, "as_json", False):
            print(json.dumps(report, sort_keys=True))
        else:
            print(
                f"SidePulse test: {report['mode']} "
                f"(socket={'delivered' if report['delivered'] else 'unavailable'}, "
                f"log={'written' if report['logged'] else 'not written'})"
            )
        return

    report = doctor_report(settings)
    if getattr(args, "as_json", False):
        print(json.dumps(report, sort_keys=True))
        return

    print("Hermes SidePulse integration")
    print(f"  agent: {report['agent_id']}")
    print(f"  event log: {report['event_log_path']}")
    print(f"  event socket: {report['socket_path']}")
    print(f"  socket active: {'yes' if report['socket_exists'] else 'no'}")
    print(f"  SidePulse CLI: {report['sidepulse_command'] or 'not found'}")
    print(f"  devices: {', '.join(report['devices']) if report['devices'] else 'none'}")
