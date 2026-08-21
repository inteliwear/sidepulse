from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .battery import (
    BatteryLedController,
    format_watts,
    parse_full_watts,
    read_battery_snapshot,
    render_battery_snapshot,
)
from .collector import AgentMonitor, SourceSpec, default_sources
from .device_writer import DEFAULT_FILE_NAME, DeviceWriteError, write_led_program
from .hook import hook_log_main
from .install import (
    install_claude_hooks,
    install_codex_hooks,
    install_grok_hooks,
    uninstall_claude_hooks,
    uninstall_codex_hooks,
    uninstall_grok_hooks,
)
from .led_status import AgentLedController, LedStatusWrite
from .lid_sleep import (
    install_sleep_helper,
    sleep_helper_install_command,
    sleep_helper_installed,
    uninstall_sleep_helper,
)
from .models import AgentStatus
from .providers import (
    HOOK_PROVIDERS,
    detect_claude_config,
    detect_codex_config,
    detect_grok_config,
    detect_log_path,
    default_log_path,
)
from .remote_hosts import (
    DEFAULT_REMOTE_PROVIDERS,
    RemoteHost,
    load_remote_hosts,
    remove_remote_host,
    run_remote_monitor,
    stream_remote_events,
    upsert_remote_host,
)
from .settings import (
    LED_DISPLAY_BATTERY,
    LED_DISPLAY_CUSTOM,
    LED_DISPLAY_CHOICES,
    load_settings,
    save_settings,
)


def main(argv: list[str] | None = None, *, prog: str = "agent-monitor") -> int:
    parser = build_parser(prog=prog)
    args = parser.parse_args(argv)
    return args.func(args)


def sidepulse_main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["agent-monitor"]:
        return main(args[1:], prog="sidepulse agent-monitor")

    parser = build_sidepulse_parser()
    if not args:
        parser.print_help()
        return 0

    parsed = parser.parse_args(args)
    return parsed.func(parsed)


def build_sidepulse_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sidepulse",
        description="SidePulse command line tools.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "agent-monitor",
        help="Install hooks and show live AI agent statuses.",
    )
    setup = subparsers.add_parser(
        "setup",
        help="Install agent hooks and start the macOS status-bar app.",
    )
    setup.add_argument(
        "provider",
        choices=("all", *HOOK_PROVIDERS),
        nargs="?",
        default="all",
        help="Agent hooks to install. Default: all.",
    )
    setup.add_argument("--log-dir", type=Path, help="Directory for provider JSONL files.")
    setup.add_argument("--codex-log", type=Path, help="Codex JSONL log path.")
    setup.add_argument("--claude-log", type=Path, help="Claude JSONL log path.")
    setup.add_argument("--grok-log", type=Path, help="Grok JSONL log path.")
    setup.add_argument("--dry-run", action="store_true", help="Show what would change.")
    setup.add_argument(
        "--sd-eject-guard-scope",
        choices=("auto", "system", "user"),
        default="auto",
        help="Install the SD eject guard as a system service when possible, or as a user agent.",
    )
    setup.add_argument(
        "--no-status-bar",
        action="store_true",
        help="Do not install or start the status-bar app.",
    )
    setup.set_defaults(func=cmd_sidepulse_setup)

    write = subparsers.add_parser(
        "write",
        help=f"Write an LED program to {DEFAULT_FILE_NAME} on a mounted SidePulse Pro or SidePulse Dot device.",
    )
    write.add_argument("text", help=r"LED program text. Backslash escapes like \n are decoded.")
    write.add_argument(
        "--device",
        type=Path,
        help="Mounted device folder or LED program file path. Defaults to auto-detecting /Volumes.",
    )
    write.add_argument(
        "--file-name",
        default=DEFAULT_FILE_NAME,
        help=f"Target file name when --device is a folder. Default: {DEFAULT_FILE_NAME}.",
    )
    write.add_argument("--dry-run", action="store_true", help="Show the target without writing.")
    write.set_defaults(func=cmd_sidepulse_write)

    add_sidepulse_status_bar_parser(subparsers)
    add_sidepulse_sdejectguard_parser(subparsers)
    add_sidepulse_battery_parser(subparsers)
    add_sidepulse_remote_parser(subparsers)
    add_remote_agent_parser(subparsers)
    # Agent configs written by older installs invoke `sidepulse hook-log`
    # directly, and `python -m sidepulse` lands here too, so both CLIs must
    # accept it.
    add_hook_log_parser(subparsers)
    return parser


def add_sidepulse_remote_parser(subparsers: argparse._SubParsersAction) -> None:
    remote = subparsers.add_parser(
        "remote",
        help="Monitor Codex and Claude sessions running on SSH hosts.",
    )
    commands = remote.add_subparsers(dest="remote_command", required=True)

    add = commands.add_parser("add", help="Add or update an SSH host and start monitoring it.")
    add.add_argument("name", help="Stable display name, for example macmini.")
    add.add_argument("--ssh", dest="ssh_target", required=True, help="SSH host or config alias.")
    add.add_argument(
        "--provider",
        action="append",
        choices=HOOK_PROVIDERS,
        help="Provider to monitor. Repeat to select more than one. Default: codex and claude.",
    )
    add.add_argument("--no-start", action="store_true", help="Save without starting the monitor.")
    add.set_defaults(func=cmd_remote_add)

    remove = commands.add_parser("remove", help="Stop monitoring and remove an SSH host.")
    remove.add_argument("name")
    remove.set_defaults(func=cmd_remote_remove)

    list_parser = commands.add_parser("list", help="List configured SSH hosts.")
    list_parser.add_argument("--json", action="store_true")
    list_parser.set_defaults(func=cmd_remote_list)

    start = commands.add_parser("start", help="Install and start the remote-host monitor.")
    start.set_defaults(func=cmd_remote_start)

    stop = commands.add_parser("stop", help="Stop the remote-host monitor.")
    stop.set_defaults(func=cmd_remote_stop)

    monitor = commands.add_parser("monitor", help="Run the remote-host monitor in the foreground.")
    monitor.set_defaults(func=cmd_remote_monitor)


def add_remote_agent_parser(subparsers: argparse._SubParsersAction) -> None:
    remote_agent = subparsers.add_parser(
        "remote-agent",
        help="Internal remote event streaming entry point.",
    )
    commands = remote_agent.add_subparsers(dest="remote_agent_command", required=True)
    stream = commands.add_parser("stream", help=argparse.SUPPRESS)
    stream.add_argument("--provider", action="append", choices=HOOK_PROVIDERS)
    stream.add_argument("--replay-lines", type=int, default=300)
    stream.add_argument("--poll-interval", type=float, default=0.25)
    stream.set_defaults(func=cmd_remote_agent_stream)


def add_hook_log_parser(subparsers: argparse._SubParsersAction) -> None:
    hook_log = subparsers.add_parser("hook-log", help="Internal hook logging entry point.")
    hook_log.add_argument("--provider", choices=HOOK_PROVIDERS, required=True)
    hook_log.add_argument("--log", type=Path, required=True)
    hook_log.set_defaults(func=cmd_hook_log)


def add_sidepulse_status_bar_parser(subparsers: argparse._SubParsersAction) -> None:
    status_bar = subparsers.add_parser(
        "status-bar",
        help="Start or stop the macOS SidePulse menu-bar app.",
    )
    status_bar.add_argument(
        "status_bar_command",
        choices=(
            "start",
            "stop",
            "install-sleep-helper",
            "uninstall-sleep-helper",
            "sleep-helper-status",
        ),
        nargs="?",
        default="start",
        help="Start/stop the menu-bar app, or manage the closed-lid sleep helper. Default: start.",
    )
    status_bar.add_argument(
        "--foreground",
        action="store_true",
        help="Run the menu-bar app in the foreground instead of installing a LaunchAgent.",
    )
    status_bar.add_argument(
        "--dry-run",
        action="store_true",
        help="Show sleep-helper changes without writing them.",
    )
    status_bar.set_defaults(func=cmd_sidepulse_status_bar)


def add_sidepulse_sdejectguard_parser(subparsers: argparse._SubParsersAction) -> None:
    guard = subparsers.add_parser(
        "sdejectguard",
        help="Start, stop, uninstall, or inspect SidePulse Pro Eject Prevention.",
    )
    guard_subparsers = guard.add_subparsers(dest="sdejectguard_command", required=True)

    start = guard_subparsers.add_parser("start", help="Install and start SidePulse Pro Eject Prevention.")
    add_sdejectguard_scope_arg(start)
    start.add_argument("--dry-run", action="store_true", help="Show what would change.")
    start.add_argument(
        "-it",
        "--interactive",
        action="store_true",
        help="Run the guard in this terminal instead of launchd.",
    )
    start.set_defaults(func=cmd_sidepulse_sdejectguard_start)

    stop = guard_subparsers.add_parser("stop", help="Stop SidePulse Pro Eject Prevention.")
    add_sdejectguard_scope_arg(stop)
    stop.add_argument("--dry-run", action="store_true", help="Show what would stop.")
    stop.set_defaults(func=cmd_sidepulse_sdejectguard_stop)

    uninstall = guard_subparsers.add_parser("uninstall", help="Remove SidePulse Pro Eject Prevention.")
    add_sdejectguard_scope_arg(uninstall)
    uninstall.add_argument("--dry-run", action="store_true", help="Show what would be removed.")
    uninstall.set_defaults(func=cmd_sidepulse_sdejectguard_uninstall)

    logs = guard_subparsers.add_parser("logs", help="Show SidePulse Pro Eject Prevention logs.")
    add_sdejectguard_scope_arg(logs)
    logs.add_argument("--lines", type=int, default=80, help="Lines to show per log file.")
    logs.add_argument("-f", "--follow", action="store_true", help="Follow existing log files.")
    logs.set_defaults(func=cmd_sidepulse_sdejectguard_logs)


def add_sdejectguard_scope_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--scope",
        choices=("auto", "system", "user"),
        default="auto",
        help="Use system scope when possible, or target one scope explicitly.",
    )


def add_sidepulse_battery_parser(subparsers: argparse._SubParsersAction) -> None:
    battery = subparsers.add_parser(
        "battery",
        help="Show or mirror Mac battery state to SidePulse Pro/SidePulse Dot LEDs.",
    )
    battery_subparsers = battery.add_subparsers(dest="battery_command", required=True)

    status = battery_subparsers.add_parser("status", help="Show current Mac battery state.")
    status.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    status.add_argument(
        "--full-watts",
        help="Full-speed charger wattage baseline, or 'auto'. Defaults to saved settings.",
    )
    status.set_defaults(func=cmd_sidepulse_battery_status)

    leds = battery_subparsers.add_parser("leds", help="Mirror Mac battery state to LEDs.")
    leds.add_argument("--interval", type=float, default=1.0, help="Refresh interval in seconds.")
    leds.add_argument(
        "--device",
        type=Path,
        help="Mounted device folder or LED program file path. Defaults to auto-detecting /Volumes.",
    )
    leds.add_argument(
        "--file-name",
        default=DEFAULT_FILE_NAME,
        help=f"Target file name when --device is a folder. Default: {DEFAULT_FILE_NAME}.",
    )
    leds.add_argument("--dry-run", action="store_true", help="Show writes without touching the device.")
    leds.add_argument("--once", action="store_true", help="Write the current battery state once and exit.")
    leds.add_argument(
        "--full-watts",
        help="Full-speed charger wattage baseline, or 'auto'. Defaults to saved settings.",
    )
    leds.set_defaults(func=cmd_sidepulse_battery_leds)

    configure = battery_subparsers.add_parser("configure", help="Save battery LED settings.")
    configure.add_argument("--display", choices=LED_DISPLAY_CHOICES, help="Status-bar LED display source.")
    configure.add_argument(
        "--full-watts",
        help="Full-speed charger wattage baseline, or 'auto' to use laptop defaults.",
    )
    configure.add_argument(
        "--show-on-power-change",
        choices=("yes", "no"),
        help="Briefly show battery LEDs when power is plugged/unplugged.",
    )
    configure.add_argument(
        "--power-change-preview-seconds",
        type=float,
        help="Seconds to show battery LEDs after plug/unplug.",
    )
    configure.set_defaults(func=cmd_sidepulse_battery_configure)


def cmd_sidepulse_write(args: argparse.Namespace) -> int:
    try:
        target = write_led_program(
            args.text,
            device_path=args.device,
            file_name=args.file_name,
            dry_run=args.dry_run,
        )
    except DeviceWriteError as exc:
        print(f"sidepulse write: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"sidepulse write: {exc}", file=sys.stderr)
        return 1

    action = "would write" if args.dry_run else "wrote"
    print(f"{action}: {target}")
    return 0


def cmd_sidepulse_battery_status(args: argparse.Namespace) -> int:
    try:
        snapshot = read_battery_snapshot(full_charge_watts=full_watts_from_args(args))
    except Exception as exc:
        print(f"sidepulse battery status: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(snapshot.to_dict(), indent=2))
    else:
        print(render_battery_snapshot(snapshot))
    return 0


def cmd_sidepulse_battery_leds(args: argparse.Namespace) -> int:
    leds = BatteryLedController(
        device_path=args.device,
        file_name=args.file_name,
        dry_run=args.dry_run,
    )
    full_watts = full_watts_from_args(args)

    try:
        while True:
            snapshot = read_battery_snapshot(full_charge_watts=full_watts)
            result = leds.sync_snapshot(snapshot)
            if result.changed or result.error or args.once:
                print(render_battery_led_result(result, snapshot, dry_run=args.dry_run))
                sys.stdout.flush()

            if args.once:
                return 2 if result.error else 0

            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"sidepulse battery leds: {exc}", file=sys.stderr)
        return 1


def cmd_sidepulse_battery_configure(args: argparse.Namespace) -> int:
    settings = load_settings()
    try:
        if args.display is not None:
            settings = settings.with_led_display(args.display)
        if args.full_watts is not None:
            settings = settings.with_battery_full_charge_watts(parse_full_watts(args.full_watts))
        if args.show_on_power_change is not None:
            settings = settings.with_battery_power_change_preview(
                enabled=args.show_on_power_change == "yes",
            )
        if args.power_change_preview_seconds is not None:
            settings = settings.with_battery_power_change_preview(
                seconds=args.power_change_preview_seconds,
            )
        target = save_settings(settings)
    except Exception as exc:
        print(f"sidepulse battery configure: {exc}", file=sys.stderr)
        return 1

    full_watts = (
        "auto"
        if settings.battery_full_charge_watts is None
        else format_watts(settings.battery_full_charge_watts)
    )
    preview = "on" if settings.battery_show_on_power_change else "off"
    print(f"settings: {target}")
    print(f"  led display: {settings.led_display}")
    print(f"  full charge watts: {full_watts}")
    print(
        "  power-change preview: "
        f"{preview} ({settings.battery_power_change_preview_seconds:g}s)"
    )
    if settings.led_display == LED_DISPLAY_BATTERY:
        print("  status bar LEDs will show battery.")
    elif settings.led_display == LED_DISPLAY_CUSTOM:
        print("  status bar LEDs will leave devices on manual output.")
    return 0


def cmd_sidepulse_status_bar(args: argparse.Namespace) -> int:
    if args.status_bar_command == "install-sleep-helper":
        return cmd_sidepulse_sleep_helper_install(args)
    if args.status_bar_command == "uninstall-sleep-helper":
        return cmd_sidepulse_sleep_helper_uninstall(args)
    if args.status_bar_command == "sleep-helper-status":
        return cmd_sidepulse_sleep_helper_status(args)

    args.uninstall = args.status_bar_command == "stop"
    args.no_start = False
    if args.uninstall:
        args.foreground = False
    return cmd_status_bar(args)


def cmd_sidepulse_sleep_helper_install(args: argparse.Namespace) -> int:
    try:
        result = install_sleep_helper(dry_run=args.dry_run)
    except (PermissionError, OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"sleep-helper: {exc}", file=sys.stderr)
        return 1

    action = "would install" if result.dry_run and result.changed else "installed"
    if not result.changed:
        action = "already installed"
    print(f"sleep-helper: {action}")
    print(f"  user: {result.user}")
    print(f"  sudoers: {result.path}")
    return 0


def cmd_sidepulse_sleep_helper_uninstall(args: argparse.Namespace) -> int:
    try:
        result = uninstall_sleep_helper(dry_run=args.dry_run)
    except (PermissionError, OSError) as exc:
        print(f"sleep-helper: {exc}", file=sys.stderr)
        return 1

    action = "would remove" if result.dry_run and result.changed else "removed"
    if not result.changed:
        action = "not installed"
    print(f"sleep-helper: {action}")
    print(f"  sudoers: {result.path}")
    return 0


def cmd_sidepulse_sleep_helper_status(_args: argparse.Namespace) -> int:
    installed = sleep_helper_installed()
    print(f"sleep-helper: {'installed' if installed else 'not installed'}")
    if not installed:
        print(f"  install: {sleep_helper_install_command()}")
    return 0


def cmd_sidepulse_sdejectguard_start(args: argparse.Namespace) -> int:
    from .sd_eject_guard_launch import (
        SD_EJECT_GUARD_DISPLAY_NAME,
        SdEjectGuardInstallError,
        install_sd_eject_guard,
        run_sd_eject_guard_interactive,
    )

    try:
        if args.interactive:
            if args.dry_run:
                print(f"{SD_EJECT_GUARD_DISPLAY_NAME}: would run interactively ({args.scope})")
                return 0
            return run_sd_eject_guard_interactive(scope=args.scope)

        result = install_sd_eject_guard(scope=args.scope, dry_run=args.dry_run)
    except (SdEjectGuardInstallError, OSError, subprocess.CalledProcessError) as exc:
        print(f"{SD_EJECT_GUARD_DISPLAY_NAME}: {exc}", file=sys.stderr)
        return 1

    print_sd_eject_guard_result(result)
    return 0


def cmd_sidepulse_sdejectguard_stop(args: argparse.Namespace) -> int:
    from .sd_eject_guard_launch import SD_EJECT_GUARD_DISPLAY_NAME, SdEjectGuardInstallError, stop_sd_eject_guard

    try:
        results = stop_sd_eject_guard(scope=args.scope, dry_run=args.dry_run)
    except (SdEjectGuardInstallError, OSError, subprocess.CalledProcessError) as exc:
        print(f"{SD_EJECT_GUARD_DISPLAY_NAME}: {exc}", file=sys.stderr)
        return 1

    for result in results:
        if result.skipped:
            action = f"skipped ({result.skipped})"
        elif result.stopped:
            action = "would stop" if args.dry_run else "stopped"
        else:
            action = "not installed"
        print(f"{SD_EJECT_GUARD_DISPLAY_NAME}: {action} ({result.scope})")
        print(f"  plist: {result.plist_path}")
    return 0


def cmd_sidepulse_sdejectguard_uninstall(args: argparse.Namespace) -> int:
    from .sd_eject_guard_launch import SD_EJECT_GUARD_DISPLAY_NAME, SdEjectGuardInstallError, uninstall_sd_eject_guard

    try:
        results = uninstall_sd_eject_guard(scope=args.scope, dry_run=args.dry_run)
    except (SdEjectGuardInstallError, OSError, subprocess.CalledProcessError) as exc:
        print(f"{SD_EJECT_GUARD_DISPLAY_NAME}: {exc}", file=sys.stderr)
        return 1

    for result in results:
        if result.skipped:
            action = f"skipped ({result.skipped})"
        elif result.removed_paths:
            action = "would uninstall" if result.dry_run else "uninstalled"
        else:
            action = "not installed"
        print(f"{SD_EJECT_GUARD_DISPLAY_NAME}: {action} ({result.scope})")
        print(f"  plist: {result.plist_path}")
        for path in result.removed_paths:
            print(f"  removed: {path}")
    return 0


def cmd_sidepulse_sdejectguard_logs(args: argparse.Namespace) -> int:
    from .sd_eject_guard_launch import log_paths_for_requested_scope, read_log_tail

    try:
        paths = log_paths_for_requested_scope(args.scope)
    except Exception as exc:
        print(f"SidePulse Pro Eject Prevention logs: {exc}", file=sys.stderr)
        return 1

    existing_paths = [path for path in paths if path.exists()]
    if args.follow:
        if not existing_paths:
            for path in paths:
                print(f"{path}: missing")
            return 1
        try:
            return subprocess.run(
                ["tail", "-n", str(args.lines), "-f", *(str(path) for path in existing_paths)],
                check=False,
            ).returncode
        except KeyboardInterrupt:
            return 130

    for path in paths:
        print(f"==> {path} <==")
        if not path.exists():
            print("(missing)")
            continue
        text = read_log_tail(path, args.lines)
        print(text if text else "(empty)")
    return 0


def cmd_sidepulse_setup(args: argparse.Namespace) -> int:
    results = install_hook_results(args)
    print_install_results(results, dry_run=args.dry_run)

    from .sd_eject_guard_launch import SD_EJECT_GUARD_DISPLAY_NAME, SdEjectGuardInstallError, install_sd_eject_guard

    try:
        guard_result = install_sd_eject_guard(
            scope=args.sd_eject_guard_scope,
            dry_run=args.dry_run,
        )
    except (SdEjectGuardInstallError, OSError, subprocess.CalledProcessError) as exc:
        print(f"{SD_EJECT_GUARD_DISPLAY_NAME}: {exc}", file=sys.stderr)
        return 1
    print_sd_eject_guard_result(guard_result)

    if args.no_status_bar:
        return 0

    if args.dry_run:
        print("status-bar: would install and start")
        return 0

    from .status_bar_launch import install_launch_agent

    result = install_launch_agent(start=True)
    action = "installed" if result.changed else "already installed"
    if result.started:
        action += " and started"
    print(f"status-bar: {action}")
    print(f"  plist: {result.plist_path}")
    return 0


def print_sd_eject_guard_result(result) -> None:
    from .sd_eject_guard_launch import SD_EJECT_GUARD_DISPLAY_NAME

    if result.dry_run:
        action = "would install and start" if result.changed else "would start (already configured)"
    else:
        action = "installed" if result.changed else "already installed"
        if result.started:
            action += " and started"
    print(f"{SD_EJECT_GUARD_DISPLAY_NAME}: {action} ({result.scope})")
    print(f"  plist: {result.plist_path}")
    print(f"  binary: {result.binary_path}")
    if result.cleanup_removed:
        print(f"  removed other scope: {result.cleanup_removed}")
    if result.cleanup_skipped:
        print(f"  cleanup skipped: {result.cleanup_skipped}")
    for path in getattr(result, "legacy_removed", ()):
        print(f"  removed legacy helper: {path}")


def build_parser(prog: str = "agent-monitor") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Collect and aggregate local AI agent statuses.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="Show detected agent hook config.")
    doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    doctor.set_defaults(func=cmd_doctor)

    status = subparsers.add_parser("status", help="Show current aggregate status once.")
    add_status_args(status)
    status.set_defaults(func=cmd_status)

    add_live_parser(subparsers, "live", "Show live statuses in the terminal.")
    add_live_parser(subparsers, "watch", "Alias for live.")

    leds = subparsers.add_parser("leds", help="Mirror aggregate agent status to SidePulse Pro/SidePulse Dot LEDs.")
    add_status_args(leds, include_json=False)
    leds.add_argument("--interval", type=float, default=1.0, help="Refresh interval in seconds.")
    leds.add_argument(
        "--device",
        type=Path,
        help="Mounted device folder or LED program file path. Defaults to auto-detecting /Volumes.",
    )
    leds.add_argument(
        "--file-name",
        default=DEFAULT_FILE_NAME,
        help=f"Target file name when --device is a folder. Default: {DEFAULT_FILE_NAME}.",
    )
    leds.add_argument("--dry-run", action="store_true", help="Show writes without touching the device.")
    leds.add_argument("--once", action="store_true", help="Write the current status once and exit.")
    leds.set_defaults(func=cmd_leds)

    status_bar = subparsers.add_parser("status-bar", help="Install and start the macOS menu-bar app.")
    status_bar_mode = status_bar.add_mutually_exclusive_group()
    status_bar_mode.add_argument(
        "--foreground",
        action="store_true",
        help="Run the menu-bar app in the foreground instead of installing a LaunchAgent.",
    )
    status_bar_mode.add_argument(
        "--uninstall",
        action="store_true",
        help="Stop and remove the status-bar LaunchAgent.",
    )
    status_bar.add_argument(
        "--no-start",
        action="store_true",
        help="Install the LaunchAgent without starting it immediately.",
    )
    status_bar.set_defaults(func=cmd_status_bar)

    install = subparsers.add_parser("install", help="Install Codex, Claude, and/or Grok monitor hooks.")
    install.add_argument("provider", choices=("all", *HOOK_PROVIDERS), nargs="?", default="all")
    install.add_argument("--log-dir", type=Path, help="Directory for provider JSONL files.")
    install.add_argument("--codex-log", type=Path, help="Codex JSONL log path.")
    install.add_argument("--claude-log", type=Path, help="Claude JSONL log path.")
    install.add_argument("--grok-log", type=Path, help="Grok JSONL log path.")
    install.add_argument("--dry-run", action="store_true", help="Show what would change.")
    install.set_defaults(func=cmd_install)

    uninstall = subparsers.add_parser("uninstall", help="Remove Codex, Claude, and/or Grok monitor hooks.")
    uninstall.add_argument("provider", choices=("all", *HOOK_PROVIDERS), nargs="?", default="all")
    uninstall.add_argument("--codex-log", type=Path, help="Codex JSONL log path.")
    uninstall.add_argument("--claude-log", type=Path, help="Claude JSONL log path.")
    uninstall.add_argument("--grok-log", type=Path, help="Grok JSONL log path.")
    uninstall.add_argument("--dry-run", action="store_true", help="Show what would change.")
    uninstall.set_defaults(func=cmd_uninstall)

    add_hook_log_parser(subparsers)

    return parser


def add_live_parser(
    subparsers: argparse._SubParsersAction,
    name: str,
    help_text: str,
) -> None:
    live = subparsers.add_parser(name, help=help_text)
    add_status_args(live, include_json=False)
    live.add_argument("--interval", type=float, default=1.0, help="Refresh interval in seconds.")
    live.add_argument(
        "--recent-seconds",
        type=float,
        default=3600.0,
        help="Only show agents updated within this many seconds unless --all is set.",
    )
    live.add_argument("--no-color", action="store_true", help="Disable ANSI color output.")
    live.set_defaults(func=cmd_watch)


def add_status_args(parser: argparse.ArgumentParser, include_json: bool = True) -> None:
    if include_json:
        parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--all", action="store_true", help="Include stale statuses in table output.")
    parser.add_argument("--stale-after", type=float, default=3600.0, help="Seconds before a status is stale.")
    parser.add_argument(
        "--tool-running-timeout",
        type=float,
        default=0.0,
        help="Seconds before an unmatched Tool Running event is treated as stale; 0 disables this.",
    )
    parser.add_argument("--max-lines", type=int, default=5000, help="Recent JSONL lines to scan per source.")
    parser.add_argument("--codex-log", type=Path, help="Codex JSONL log path.")
    parser.add_argument("--claude-log", type=Path, help="Claude JSONL log path.")
    parser.add_argument("--grok-log", type=Path, help="Grok JSONL log path.")


def cmd_doctor(args: argparse.Namespace) -> int:
    configs = [detect_codex_config(), detect_claude_config(), detect_grok_config()]
    payload = {"providers": [config.to_dict() for config in configs]}
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0

    for config in configs:
        print(f"{config.provider}:")
        print(f"  config: {config.config_path} ({'found' if config.exists else 'missing'})")
        print(f"  hooks enabled: {config.hooks_enabled}")
        print(f"  events: {', '.join(config.hook_events) if config.hook_events else '-'}")
        print(f"  logs: {', '.join(str(path) for path in config.log_paths) if config.log_paths else '-'}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    monitor = monitor_from_args(args)
    snapshot = monitor.snapshot(include_stale=args.all)
    if args.json:
        print(json.dumps(snapshot.to_dict(), indent=2))
    else:
        print(render_snapshot(snapshot, include_stale=args.all))
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    monitor = monitor_from_args(args)
    color = should_use_color(args.no_color)
    try:
        if sys.stdout.isatty():
            print("\033[?25l", end="")
        while True:
            snapshot = monitor.snapshot(include_stale=args.all)
            print("\033[2J\033[H", end="")
            print(
                render_watch_dashboard(
                    snapshot,
                    interval=args.interval,
                    recent_seconds=args.recent_seconds,
                    include_stale=args.all,
                    color=color,
                )
            )
            sys.stdout.flush()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    finally:
        if sys.stdout.isatty():
            print("\033[?25h", end="")


def cmd_leds(args: argparse.Namespace) -> int:
    monitor = monitor_from_args(args)
    leds = AgentLedController(
        device_path=args.device,
        file_name=args.file_name,
        dry_run=args.dry_run,
    )

    try:
        while True:
            snapshot = monitor.snapshot(include_stale=args.all)
            result = leds.sync_mode(snapshot.aggregate.mode)
            if result.changed or result.error or args.once:
                print(render_led_sync_result(result, snapshot, dry_run=args.dry_run))
                sys.stdout.flush()

            if args.once:
                return 2 if result.error else 0

            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


def cmd_status_bar(args: argparse.Namespace) -> int:
    if args.foreground:
        from .status_bar import main as status_bar_main

        return status_bar_main()

    from .status_bar_launch import install_launch_agent, uninstall_launch_agent

    if args.uninstall:
        result = uninstall_launch_agent()
        action = "removed" if result.changed else "already removed"
        print(f"status-bar: {action}")
        print(f"  plist: {result.plist_path}")
        return 0

    result = install_launch_agent(start=not args.no_start)
    action = "installed" if result.changed else "already installed"
    if result.started:
        action += " and started"
    print(f"status-bar: {action}")
    print(f"  plist: {result.plist_path}")
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    results = install_hook_results(args)
    print_install_results(results, dry_run=args.dry_run)
    return 0


def selected_hook_providers(provider: str) -> tuple[str, ...]:
    return HOOK_PROVIDERS if provider == "all" else (provider,)


def install_hook_results(args: argparse.Namespace):
    providers = selected_hook_providers(args.provider)
    results = []
    for provider in providers:
        log_path = install_log_path(provider, args)
        if provider == "codex":
            results.append(install_codex_hooks(log_path=log_path, dry_run=args.dry_run))
        elif provider == "claude":
            results.append(install_claude_hooks(log_path=log_path, dry_run=args.dry_run))
        else:
            results.append(install_grok_hooks(log_path=log_path, dry_run=args.dry_run))
    return results


def print_install_results(results, *, dry_run: bool) -> None:
    for result in results:
        action = "would update" if dry_run and result.changed else "updated"
        if not result.changed:
            action = "already configured"
        print(f"{result.provider}: {action}")
        print(f"  config: {result.config_path}")
        print(f"  log: {result.log_path}")
        if result.backup_path:
            print(f"  backup: {result.backup_path}")


def cmd_uninstall(args: argparse.Namespace) -> int:
    providers = selected_hook_providers(args.provider)
    results = []
    for provider in providers:
        log_path = uninstall_log_path(provider, args)
        if provider == "codex":
            results.append(uninstall_codex_hooks(log_path=log_path, dry_run=args.dry_run))
        elif provider == "claude":
            results.append(uninstall_claude_hooks(log_path=log_path, dry_run=args.dry_run))
        else:
            results.append(uninstall_grok_hooks(log_path=log_path, dry_run=args.dry_run))

    for result in results:
        action = "would remove" if args.dry_run and result.changed else "removed"
        if not result.changed:
            action = "already uninstalled"
        print(f"{result.provider}: {action}")
        print(f"  config: {result.config_path}")
        print(f"  log: {result.log_path}")
        if result.backup_path:
            print(f"  backup: {result.backup_path}")
    return 0


def cmd_hook_log(args: argparse.Namespace) -> int:
    return hook_log_main(args.provider, args.log)


def cmd_remote_add(args: argparse.Namespace) -> int:
    providers = tuple(args.provider or DEFAULT_REMOTE_PROVIDERS)
    try:
        host = RemoteHost(args.name, args.ssh_target, tuple(dict.fromkeys(providers)))
    except ValueError as exc:
        print(f"remote: {exc}", file=sys.stderr)
        return 2
    target = upsert_remote_host(host)
    print(f"remote: saved {host.name} ({host.ssh_target})")
    print(f"  providers: {', '.join(host.providers)}")
    print(f"  config: {target}")
    if not args.no_start:
        from .remote_launch import install_remote_launch_agent

        result = install_remote_launch_agent(start=True)
        print(f"  monitor: started ({result.plist_path})")
    return 0


def cmd_remote_remove(args: argparse.Namespace) -> int:
    target, changed = remove_remote_host(args.name)
    if not changed:
        print(f"remote: {args.name} is not configured")
        return 1

    remaining = load_remote_hosts(target)
    from .remote_launch import install_remote_launch_agent, uninstall_remote_launch_agent

    if remaining:
        install_remote_launch_agent(start=True)
        action = "monitor restarted"
    else:
        uninstall_remote_launch_agent()
        action = "monitor stopped"
    print(f"remote: removed {args.name}; {action}")
    print(f"  config: {target}")
    return 0


def cmd_remote_list(args: argparse.Namespace) -> int:
    hosts = load_remote_hosts()
    if args.json:
        print(json.dumps({"hosts": [host.to_dict() for host in hosts]}, indent=2))
        return 0
    if not hosts:
        print("No remote hosts configured.")
        return 0
    for host in hosts:
        print(f"{host.name}: {host.ssh_target} ({', '.join(host.providers)})")
    return 0


def cmd_remote_start(_args: argparse.Namespace) -> int:
    if not load_remote_hosts():
        print("remote: no hosts configured", file=sys.stderr)
        return 1
    from .remote_launch import install_remote_launch_agent

    result = install_remote_launch_agent(start=True)
    print(f"remote: started ({result.plist_path})")
    return 0


def cmd_remote_stop(_args: argparse.Namespace) -> int:
    from .remote_launch import uninstall_remote_launch_agent

    result = uninstall_remote_launch_agent()
    action = "stopped" if result.changed else "already stopped"
    print(f"remote: {action}")
    return 0


def cmd_remote_monitor(_args: argparse.Namespace) -> int:
    return run_remote_monitor()


def cmd_remote_agent_stream(args: argparse.Namespace) -> int:
    return stream_remote_events(
        args.provider or DEFAULT_REMOTE_PROVIDERS,
        replay_lines=max(0, args.replay_lines),
        poll_interval=args.poll_interval,
    )


def monitor_from_args(args: argparse.Namespace) -> AgentMonitor:
    if any(getattr(args, f"{provider}_log", None) for provider in HOOK_PROVIDERS):
        fallback_sources = default_sources()
        sources = []
        for provider in HOOK_PROVIDERS:
            explicit = getattr(args, f"{provider}_log", None)
            if explicit:
                sources.append(SourceSpec(provider, explicit.expanduser()))
            else:
                sources.extend(source for source in fallback_sources if source.provider == provider)
    else:
        sources = list(default_sources())

    return AgentMonitor(
        sources=sources,
        stale_after_seconds=args.stale_after,
        tool_running_timeout_seconds=args.tool_running_timeout,
        max_lines_per_source=args.max_lines,
    )


def install_log_path(provider: str, args: argparse.Namespace) -> Path:
    explicit = getattr(args, f"{provider}_log", None)
    if explicit:
        return explicit.expanduser()
    log_dir = getattr(args, "log_dir", None)
    if log_dir:
        return log_dir.expanduser() / f"{provider}.jsonl"
    return default_log_path(provider)


def uninstall_log_path(provider: str, args: argparse.Namespace) -> Path | None:
    explicit = getattr(args, f"{provider}_log", None)
    return explicit.expanduser() if explicit else None


def full_watts_from_args(args: argparse.Namespace) -> float | None:
    if getattr(args, "full_watts", None) is not None:
        return parse_full_watts(args.full_watts)
    return load_settings().battery_full_charge_watts


def render_snapshot(snapshot, include_stale: bool = False) -> str:
    lines = []
    aggregate = snapshot.aggregate
    lines.append(
        f"Aggregate: {aggregate.mode_label} "
        f"({aggregate.active_count} active, {aggregate.stale_count} stale)"
    )
    if aggregate.representative:
        lines.append(f"Reason: {describe_status(aggregate.representative, snapshot.collected_at)}")
    lines.append("")
    lines.append("Sources:")
    for source in snapshot.sources:
        marker = "ok" if source.path.exists() else "missing"
        lines.append(f"  {source.provider}: {source.path} [{marker}]")

    statuses = list(snapshot.statuses)
    if include_stale:
        statuses.extend(snapshot.stale_statuses)

    lines.append("")
    lines.append("Agents:")
    if not statuses:
        lines.append("  none")
    else:
        for status in statuses:
            lines.append(f"  {describe_status(status, snapshot.collected_at)}")
    return "\n".join(lines)


def render_watch_dashboard(
    snapshot,
    interval: float,
    recent_seconds: float,
    include_stale: bool = False,
    color: bool = False,
) -> str:
    width = max(80, shutil.get_terminal_size((120, 24)).columns)
    statuses = visible_watch_statuses(snapshot, recent_seconds, include_stale)
    aggregate = snapshot.aggregate
    timestamp = snapshot.collected_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    title = colorize("Agent Monitor", "1", color)
    aggregate_text = colorize(aggregate.mode_label, mode_color(aggregate.mode), color)
    recent_text = "all known agents" if include_stale else f"last {format_duration(recent_seconds)}"

    lines = [
        f"{title}  aggregate={aggregate_text}  agents={len(statuses)}  updated={timestamp}",
        f"refresh={interval:g}s  showing={recent_text}  active={aggregate.active_count}  stale={aggregate.stale_count}  quit=Ctrl-C",
        "=" * min(width, 120),
    ]

    if aggregate.representative:
        lines.append(
            "reason: "
            + colorize(
                describe_status(aggregate.representative, snapshot.collected_at),
                mode_color(aggregate.representative.mode),
                color,
            )
        )
    else:
        lines.append("reason: no recent agent status")

    lines.extend(["", "Sources"])
    for source in snapshot.sources:
        ok = source.path.exists()
        marker = colorize("OK", "32", color) if ok else colorize("MISS", "31", color)
        lines.append(f"  {marker:<4} {source.provider:<7} {source.path}")

    lines.extend(["", "Recently Active Agents"])
    if not statuses:
        lines.append("  none")
        return "\n".join(lines)

    table_width = min(width, 140)
    fixed_width = 9 + 22 + 18 + 20 + 8 + 18 + 16 + 18
    cwd_width = max(18, table_width - fixed_width)
    widths = [9, 22, 18, 20, 8, 18, 16, cwd_width]
    headers = ["Provider", "Agent", "Origin", "Mode", "Age", "Event", "Tool", "Cwd"]
    lines.append(table_separator(widths))
    lines.append(table_row(headers, widths))
    lines.append(table_separator(widths))
    for status in statuses:
        age = format_duration(status.age_seconds(snapshot.collected_at))
        row = [
            status.provider,
            status.display_name,
            status.origin or "-",
            status.mode_label,
            age,
            status.event_name,
            status.tool_name or "-",
            status.cwd or "-",
        ]
        lines.append(table_row(row, widths, mode_index=3, mode=status.mode, color=color))
    lines.append(table_separator(widths))
    return "\n".join(lines)


def visible_watch_statuses(snapshot, recent_seconds: float, include_stale: bool) -> list[AgentStatus]:
    statuses = list(snapshot.statuses)
    if include_stale:
        statuses.extend(snapshot.stale_statuses)
        return sorted(statuses, key=lambda status: (status.priority, -status.updated_at.timestamp()))

    if recent_seconds > 0:
        statuses = [
            status
            for status in statuses
            if status.age_seconds(snapshot.collected_at) <= recent_seconds
        ]

    return sorted(statuses, key=lambda status: (status.priority, -status.updated_at.timestamp()))


def describe_status(status: AgentStatus, now) -> str:
    age = int(status.age_seconds(now))
    stale = " stale" if status.stale else ""
    origin = f" origin={status.origin}" if status.origin else ""
    tool = f" tool={status.tool_name}" if status.tool_name else ""
    cwd = f" cwd={status.cwd}" if status.cwd else ""
    return (
        f"{status.display_name}: {status.mode_label}"
        f" event={status.event_name}{origin}{tool} age={age}s{stale}{cwd}"
    )


def render_led_sync_result(result: LedStatusWrite, snapshot, dry_run: bool = False) -> str:
    if result.error:
        return f"LEDs: {result.label} error={result.error}"

    action = "would write" if dry_run else "wrote"
    target = result.target if result.target is not None else "-"
    lines = [
        (
            f"LEDs: {action} {result.label} to {target} "
            f"(aggregate={snapshot.aggregate.mode_label}, active={snapshot.aggregate.active_count})"
        )
    ]
    if dry_run and result.program:
        lines.append(result.program)
    return "\n".join(lines)


def render_battery_led_result(result, snapshot, dry_run: bool = False) -> str:
    if result.error:
        return f"Battery LEDs: error={result.error}"

    action = "would write" if dry_run else "wrote"
    target = result.target if result.target is not None else "-"
    lines = [
        (
            f"Battery LEDs: {action} {snapshot.percent}% "
            f"({format_watts(snapshot.adapter_power)}/"
            f"{format_watts(snapshot.full_charge_watts)}, "
            f"{snapshot.charge_speed_ratio() * 100:.0f}% speed) to {target}"
        )
    ]
    if dry_run and result.program:
        lines.append(result.program)
    return "\n".join(lines)


def table_separator(widths: list[int]) -> str:
    return "+" + "+".join("-" * (width + 2) for width in widths) + "+"


def table_row(
    cells: list[str],
    widths: list[int],
    mode_index: int | None = None,
    mode=None,
    color: bool = False,
) -> str:
    padded = []
    for index, (cell, width) in enumerate(zip(cells, widths)):
        text = truncate(str(cell), width).ljust(width)
        if mode_index is not None and index == mode_index and mode is not None:
            text = colorize(text, mode_color(mode), color)
        padded.append(f" {text} ")
    return "|" + "|".join(padded) + "|"


def truncate(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[: width - 1] + "."


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{rest:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def should_use_color(no_color: bool) -> bool:
    return (
        not no_color
        and "NO_COLOR" not in os.environ
        and sys.stdout.isatty()
    )


def colorize(text: str, code: str, enabled: bool) -> str:
    if not enabled:
        return text
    return f"\033[{code}m{text}\033[0m"


def mode_color(mode) -> str:
    return {
        "blocked_error": "31;1",
        "waiting_for_input": "33;1",
        "tool_running": "36;1",
        "long_task_progress": "35;1",
        "working": "34;1",
        "completed": "32;1",
        "idle_ready": "37",
    }.get(getattr(mode, "value", str(mode)), "37")
