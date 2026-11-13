from __future__ import annotations

import argparse
import json
import math
import os
import queue
import select
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from . import __version__
from .battery import read_battery_snapshot
from .device_writer import (
    DEFAULT_FILE_NAME,
    DeviceWriteError,
    discover_devices,
    normalize_led_text,
    validate_led_text,
    write_normalized_led_program,
)
from .hook import hook_log_main
from .install import (
    install_claude_hooks,
    install_codex_hooks,
    install_cursor_hooks,
    install_grok_hooks,
    install_junie_hooks,
    uninstall_claude_hooks,
    uninstall_codex_hooks,
    uninstall_cursor_hooks,
    uninstall_grok_hooks,
    uninstall_junie_hooks,
)
from .links import (
    IOSLink,
    LinkError,
    PAIRING_TIMEOUT_SECONDS,
    bridge_server,
    listen_for_ios_registration,
    load_ios_links,
    new_pairing_channel,
    normalize_phone_token,
    pairing_url,
    remove_ios_link,
    render_terminal_qr,
    send_ios_program,
    store_ios_link,
)
from .relay import (
    configure_outbound_channel,
    ensure_receiver_config,
    relay_link_command,
)
from .lid_sleep import (
    install_sleep_helper,
    sleep_helper_install_command,
    sleep_helper_installed,
    uninstall_sleep_helper,
)
from .models import AgentStatus
from .providers import (
    HOOK_PROVIDERS,
    detect_provider_configs,
    default_log_path,
)
from .settings import (
    load_settings,
    save_settings,
)


def sidepulse_main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    parser = build_sidepulse_parser()
    if not args:
        parser.print_help()
        return 0

    parsed = parser.parse_args(args)
    return parsed.func(parsed)


def add_version_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--version",
        "-V",
        action="version",
        version=f"sidepulse {__version__}",
        help="Print the installed SidePulse version and exit.",
    )


def cmd_version(args: argparse.Namespace) -> int:
    print(f"sidepulse {__version__}")
    return 0


def build_sidepulse_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sidepulse",
        description="SidePulse command line tools.",
    )
    add_version_argument(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)
    report = subparsers.add_parser("report", help="Report OSC 7501-compatible program status.")
    report.add_argument("--scope")
    report_input = report.add_mutually_exclusive_group()
    report_input.add_argument("--state", choices=["idle", "working", "blocked", "done", "error", "clear"])
    for field in ("app", "id", "title", "msg", "kind", "request-id"):
        report.add_argument("--" + field)
    report.add_argument("--progress", type=int)
    report_input.add_argument("--json", action="store_true", help="Read a complete JSON report from stdin.")
    report_input.add_argument("--osc", nargs="?", const="-", metavar="LINE",
                              help="Decode one OSC 7501 line (terminator optional); omit LINE or use - to read stdin. Requires --scope.")
    report.set_defaults(func=cmd_report)

    wrap = subparsers.add_parser("wrap", help="Run a command in a terminal and capture OSC 7501 status.")
    wrap.add_argument("argv", nargs=argparse.REMAINDER, metavar="COMMAND",
                      help="Command and its arguments; -- is optional.")
    wrap.set_defaults(func=cmd_wrap)

    version = subparsers.add_parser("version", help="Print the installed SidePulse version.")
    version.set_defaults(func=cmd_version)
    update = subparsers.add_parser("update", help="Update using the one-command setup installer.")
    update.add_argument(
        "--dry-run", action="store_true",
        help="Show the setup command without downloading or changing anything.",
    )
    update.set_defaults(func=cmd_sidepulse_update)
    from .firmware import add_firmware_parser

    add_firmware_parser(subparsers)
    from .storage import add_storage_parser

    add_storage_parser(subparsers)
    from .device_list import add_list_parser

    add_list_parser(subparsers)
    status = subparsers.add_parser("status", help="Show the daemon's current session status once.")
    add_status_args(status)
    status.set_defaults(func=cmd_status)
    live = subparsers.add_parser("live", help="Refresh the same daemon status in the terminal.")
    add_status_args(live)
    live.add_argument("--interval", type=positive_interval, default=1.0, help="Refresh interval in seconds.")
    live.add_argument("--once", action="store_true", help="Show one frame and exit.")
    live.set_defaults(func=cmd_live)
    doctor = subparsers.add_parser("doctor", help="Show detected agent hook configuration.")
    doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    doctor.set_defaults(func=cmd_doctor)
    setup = subparsers.add_parser(
        "setup",
        help="Install agent hooks and, on macOS, start the status-bar app.",
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
    setup.add_argument("--cursor-log", type=Path, help="Cursor JSONL log path.")
    setup.add_argument("--junie-log", type=Path, help="Junie JSONL log path.")
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
    setup.add_argument("--uninstall", action="store_true", help="Remove the selected provider hooks.")
    setup.set_defaults(func=cmd_sidepulse_setup)

    write = subparsers.add_parser(
        "write",
        help="Send an LED program, notification, or both to SidePulse.",
    )
    add_sidepulse_delivery_arguments(write)
    write.set_defaults(func=cmd_sidepulse_write)

    push = subparsers.add_parser(
        "push",
        help="Send an LED program, notification, or both, preferring a linked phone.",
    )
    add_sidepulse_delivery_arguments(push)
    push.set_defaults(func=cmd_sidepulse_push)

    link = subparsers.add_parser(
        "link",
        help="Link an iPhone or Android phone, or connect this computer to a remote SidePulse receiver.",
    )
    link.add_argument("relay_code", nargs="?", help="Relay code printed by the receiving Mac.")
    link.set_defaults(func=cmd_sidepulse_link)

    unlink = subparsers.add_parser(
        "unlink",
        help="List linked phones or remove one by its displayed ID.",
    )
    unlink.add_argument("link_id", nargs="?", help="Exact linked phone ID to remove.")
    unlink.set_defaults(func=cmd_sidepulse_unlink)

    service = subparsers.add_parser(
        "service",
        help="Manage the headless SidePulse background service.",
    )
    service.add_argument(
        "service_command",
        choices=("start", "stop", "status", "run"),
        nargs="?",
        default="status",
    )
    service.set_defaults(func=cmd_sidepulse_service)

    add_sidepulse_status_bar_parser(subparsers)
    settings = subparsers.add_parser(
        "settings",
        help="Open SidePulse settings, even when the menu bar icon is hidden.",
    )
    settings.set_defaults(func=cmd_sidepulse_settings)
    add_sidepulse_sdejectguard_parser(subparsers)
    # Hook entry point shared by installed scripts and frozen executables.
    add_hook_log_parser(subparsers)
    return parser


def add_sidepulse_delivery_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "text",
        nargs="?",
        help=r"LED program text. Backslash escapes like \n are decoded. Use - to read stdin.",
    )
    parser.add_argument("--title", help="Notification title.")
    parser.add_argument("--message", help="Notification message.")
    parser.add_argument("--to", help="Destination name or ID. Use 'local' or 'phone' when unambiguous.")
    parser.add_argument("--all", action="store_true", help="Send to every compatible destination.")
    parser.add_argument(
        "--device",
        type=Path,
        help="Mounted device folder, LED program file, or Dot USB selector. Cannot be combined with --to or --all.",
    )
    parser.add_argument(
        "--file-name",
        default=DEFAULT_FILE_NAME,
        help=f"Target file name when --device is a folder. Default: {DEFAULT_FILE_NAME}.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Show the target without writing or sending.")


def add_hook_log_parser(subparsers: argparse._SubParsersAction) -> None:
    hook_log = subparsers.add_parser("hook-log", help="Internal hook logging entry point.")
    hook_log.add_argument("--provider", choices=HOOK_PROVIDERS, required=True)
    hook_log.add_argument("--log", type=Path, required=True)
    hook_log.add_argument("--event", help="Provider-native lifecycle event name (used by cursor).")
    hook_log.set_defaults(func=cmd_hook_log)


def add_sidepulse_status_bar_parser(subparsers: argparse._SubParsersAction) -> None:
    status_bar = subparsers.add_parser(
        "status-bar",
        help="Show and start the macOS SidePulse menu-bar app, or stop it.",
    )
    status_bar.add_argument(
        "status_bar_command",
        choices=(
            "start",
            "stop",
            "install-sleep-helper",
            "uninstall-sleep-helper",
            "sleep-helper-status",
            "uninstall-magsafe-helper",
            "magsafe-helper-status",
        ),
        nargs="?",
        default="start",
        help="Start/stop the menu-bar app, or manage its helpers. Default: start.",
    )
    status_bar.add_argument(
        "--foreground",
        action="store_true",
        help="Run the menu-bar app in the foreground instead of installing a LaunchAgent.",
    )
    status_bar.add_argument(
        "--dry-run",
        action="store_true",
        help="Show helper changes without writing them.",
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


def cmd_sidepulse_write(args: argparse.Namespace) -> int:
    return _cmd_sidepulse_delivery(args, command="write", prefer_phone=False)


def cmd_sidepulse_push(args: argparse.Namespace) -> int:
    return _cmd_sidepulse_delivery(args, command="push", prefer_phone=True)


def _cmd_sidepulse_delivery(
    args: argparse.Namespace,
    *,
    command: str,
    prefer_phone: bool,
) -> int:
    prefix = f"sidepulse {command}"
    if args.to and args.all:
        print(f"{prefix}: Use either --to or --all, not both.", file=sys.stderr)
        return 2
    if args.device is not None and (args.to or args.all):
        print(f"{prefix}: --device cannot be combined with --to or --all.", file=sys.stderr)
        return 2

    title = _clean_notification_text(args.title)
    message = _clean_notification_text(args.message)
    has_notification = title is not None or message is not None
    try:
        program = _program_from_args(args.text, allow_implicit_stdin=not has_notification)
        if program is not None:
            validate_led_text(program)
    except DeviceWriteError as exc:
        print(f"{prefix}: {exc}", file=sys.stderr)
        return 2

    if program is None and not has_notification:
        print(
            f"{prefix}: Provide an LED program, --title, or --message.",
            file=sys.stderr,
        )
        return 2

    if args.device is not None:
        if has_notification:
            print(
                f"{prefix}: A local SidePulse cannot display notifications. "
                "Choose a linked phone with --to.",
                file=sys.stderr,
            )
            return 2
        return _write_explicit_local(args, program, prefix=prefix)

    local_devices = discover_devices(file_name=args.file_name)
    phone_links = load_ios_links()
    try:
        local_targets, phone_targets = _select_delivery_targets(
            requested=args.to,
            send_all=args.all,
            program=program,
            has_notification=has_notification,
            prefer_phone=prefer_phone,
            local_devices=local_devices,
            phone_links=phone_links,
        )
    except DeviceWriteError as exc:
        print(f"{prefix}: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        for candidate in local_targets:
            print(f"would write: {candidate.target}")
        for link in phone_targets:
            print(f"would send: {link.name} ({link.link_id})")
        return 0

    event_id = str(uuid.uuid4())
    event_data = _remote_event_data(event_id) if phone_targets else None
    failed = False
    for candidate in local_targets:
        try:
            target = write_normalized_led_program(
                program or "",
                device_path=candidate.target,
                file_name=args.file_name,
            )
            print(f"wrote: {target}")
        except (DeviceWriteError, OSError) as exc:
            failed = True
            print(f"{prefix}: {candidate.root}: {exc}", file=sys.stderr)

    for link in phone_targets:
        try:
            send_ios_program(
                link,
                program,
                event_id=event_id,
                title="Update" if command == "push" and not has_notification else title,
                message=message,
                data=event_data,
            )
            print(f"sent: {link.name} ({link.link_id})")
        except LinkError as exc:
            failed = True
            print(f"{prefix}: {link.name}: {exc}", file=sys.stderr)
    return 1 if failed else 0


def _clean_notification_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _program_from_args(text: str | None, *, allow_implicit_stdin: bool) -> str | None:
    if text == "-":
        return normalize_led_text(sys.stdin.read())
    if text is not None:
        return normalize_led_text(text)
    if allow_implicit_stdin:
        piped = _read_available_stdin()
        if piped is not None:
            program = normalize_led_text(piped)
            # Nobody asked for this read, so an empty one means nothing was
            # piped in. That is "no program given" -- which earns the usage
            # hint -- rather than an empty program to reject.
            if program.strip():
                return program
    return None


def _read_available_stdin() -> str | None:
    try:
        if sys.stdin.isatty():
            return None
        readable, _, _ = select.select([sys.stdin], [], [], 0)
        if readable:
            return sys.stdin.read()
    except (AttributeError, OSError, TypeError, ValueError):
        return None
    return None


def _write_explicit_local(args: argparse.Namespace, program: str | None, *, prefix: str) -> int:
    if program is None:
        print(f"{prefix}: A local SidePulse requires an LED program.", file=sys.stderr)
        return 2
    try:
        target = write_normalized_led_program(
            program,
            device_path=args.device,
            file_name=args.file_name,
            dry_run=args.dry_run,
        )
    except DeviceWriteError as exc:
        print(f"{prefix}: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"{prefix}: {exc}", file=sys.stderr)
        return 1
    action = "would write" if args.dry_run else "wrote"
    print(f"{action}: {target}")
    return 0


def _select_delivery_targets(
    *,
    requested: str | None,
    send_all: bool,
    program: str | None,
    has_notification: bool,
    prefer_phone: bool,
    local_devices,
    phone_links: tuple[IOSLink, ...],
):
    if send_all:
        if has_notification and not phone_links:
            raise DeviceWriteError(
                "No linked phone can display the notification. Run `sidepulse link`."
            )
        local_targets = list(local_devices) if program is not None else []
        phone_targets = list(phone_links)
        if not local_targets and not phone_targets:
            raise DeviceWriteError("No destinations found. Run `sidepulse link` to connect your phone.")
        return local_targets, phone_targets

    if requested:
        matches = _matching_destinations(requested, local_devices, phone_links)
        if not matches:
            available = _format_destinations(local_devices, phone_links)
            suffix = f"\nAvailable destinations:\n{available}" if available else ""
            raise DeviceWriteError(f"No destination matches {requested!r}.{suffix}")
        if len(matches) > 1:
            rendered = "\n".join(f"  {label}" for _, _, label in matches)
            raise DeviceWriteError(
                f"Destination {requested!r} is ambiguous:\n{rendered}\nUse its ID with --to."
            )
        kind, target, _ = matches[0]
        if kind == "local":
            if has_notification:
                raise DeviceWriteError(
                    "A local SidePulse cannot display notifications. Choose a linked phone with --to."
                )
            if program is None:
                raise DeviceWriteError("A local SidePulse requires an LED program.")
            return [target], []
        return [], [target]

    targets_are_phones = has_notification or (prefer_phone and bool(phone_links)) or not local_devices
    compatible = list(phone_links) if targets_are_phones else list(local_devices)
    if not compatible:
        if has_notification:
            raise DeviceWriteError("No linked phone found. Run `sidepulse link`.")
        raise DeviceWriteError("No SidePulse destination found. Run `sidepulse link` to connect your phone.")
    if len(compatible) > 1:
        if targets_are_phones:
            rendered = _format_destinations([], compatible)
        else:
            rendered = _format_destinations(compatible, ())
        raise DeviceWriteError(
            "More than one destination is available:\n"
            f"{rendered}\nChoose one with --to, or use --all."
        )
    if targets_are_phones:
        return [], compatible
    return compatible, []


def _matching_destinations(requested: str, local_devices, phone_links: tuple[IOSLink, ...]):
    query = requested.strip().casefold()
    if not query:
        return []
    if query == "local":
        return [("local", item, f"{item.root.name} ({item.root})") for item in local_devices]
    if query == "phone":
        return [("phone", item, f"{item.name} ({item.link_id})") for item in phone_links]

    matches = []
    for item in local_devices:
        names = {item.root.name.casefold(), str(item.root).casefold(), str(item.target).casefold()}
        if query in names:
            matches.append(("local", item, f"{item.root.name} ({item.root})"))
    for item in phone_links:
        id_matches = query == item.link_id.casefold() or (
            len(query) >= 4 and item.link_id.casefold().startswith(query)
        )
        if query == item.name.casefold() or id_matches:
            matches.append(("phone", item, f"{item.name} ({item.link_id})"))
    return matches


def _format_destinations(local_devices, phone_links) -> str:
    lines = [f"  {item.root.name} ({item.root})" for item in local_devices]
    lines.extend(f"  {item.name} ({item.link_id})" for item in phone_links)
    return "\n".join(lines)


def _remote_event_data(event_id: str) -> dict[str, object]:
    source: dict[str, object] = {"name": socket.gethostname()}
    try:
        battery = read_battery_snapshot()
    except Exception:
        battery = None
    if battery is not None and battery.battery_present:
        source["battery"] = {
            "level": battery.percent,
            "charging": battery.is_charging,
            "plugged_in": battery.is_plugged,
        }
    return {
        "sidepulse_event_id": event_id,
        "source": source,
    }


def cmd_sidepulse_link(args: argparse.Namespace) -> int:
    relay_code = getattr(args, "relay_code", None)
    if relay_code:
        try:
            config = configure_outbound_channel(relay_code, server=bridge_server())
        except LinkError as exc:
            print(f"sidepulse link: {exc}", file=sys.stderr)
            return 1
        print(
            "Linked this computer to the remote SidePulse receiver "
            f"({config.outbound_channel[:12]})."
        )
        print("Agent events will be relayed by the SidePulse background service.")
        return 0

    try:
        relay_config = ensure_receiver_config()
        server = bridge_server()
        channel = new_pairing_channel()
        url = pairing_url(server, channel)
        qr = render_terminal_qr(
            url,
            ansi=sys.stdout.isatty()
            and os.environ.get("TERM") != "dumb"
            and "NO_COLOR" not in os.environ,
        )
    except LinkError as exc:
        print(f"sidepulse link: {exc}", file=sys.stderr)
        return 1

    print("Link a remote computer")
    print()
    print("Run this command on the VM or other computer:")
    print()
    print(f"  {relay_link_command(relay_config)}")
    print()

    results: queue.Queue[IOSLink] = queue.Queue()
    stop = threading.Event()
    deadline = time.monotonic() + PAIRING_TIMEOUT_SECONDS
    listener = threading.Thread(
        target=listen_for_ios_registration,
        args=(server, channel, results, stop),
        kwargs={"deadline": deadline},
        daemon=True,
    )
    listener.start()

    existing = load_ios_links()
    if existing:
        print(
            "Linked phones: "
            + ", ".join(f"{link.name} ({link.link_id})" for link in existing)
        )
        print()
    print("Link your iPhone or Android phone")
    print()
    print("Scan this QR code with your phone:")
    print()
    print(qr)
    print()
    print("Or paste the push token shown in the SidePulse app.")

    input_enabled = True
    prompt_visible = False
    try:
        while time.monotonic() < deadline:
            try:
                link = results.get_nowait()
                break
            except queue.Empty:
                pass

            if input_enabled and not prompt_visible:
                print("Push token: ", end="", flush=True)
                prompt_visible = True

            readable = []
            if input_enabled:
                try:
                    readable, _, _ = select.select([sys.stdin], [], [], 0.2)
                except (OSError, ValueError):
                    input_enabled = False
            else:
                time.sleep(0.2)

            if readable:
                value = sys.stdin.readline()
                if not value:
                    input_enabled = False
                    continue
                try:
                    token = normalize_phone_token(value)
                except LinkError as exc:
                    print(f"Invalid token: {exc}")
                    prompt_visible = False
                    continue
                link = IOSLink(
                    name="Android phone" if token.startswith("fcm_") else "iPhone",
                    token=token,
                    server=server,
                    linked_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                )
                break
        else:
            print("\nPairing timed out. Run `sidepulse link` to try again.", file=sys.stderr)
            return 1
    except KeyboardInterrupt:
        print("\nLinking stopped.")
        return 130
    finally:
        stop.set()

    if prompt_visible:
        print()
    try:
        store_ios_link(link)
    except OSError as exc:
        print(f"sidepulse link: Could not save the phone: {exc}", file=sys.stderr)
        return 1
    print(
        f"Linked {link.name} ({link.link_id}). "
        "`sidepulse write` uses it when no local device is mounted; "
        "`sidepulse push` prefers it."
    )
    return 0


def cmd_sidepulse_unlink(args: argparse.Namespace) -> int:
    links = load_ios_links()
    if args.link_id is None:
        if not links:
            print("No linked phones.")
            return 0
        print("Linked phones:")
        for link in links:
            print(f"  {link.name} ({link.link_id})")
        print("Remove one with `sidepulse unlink ID_FROM_LIST`.")
        return 0

    matches = [link for link in links if link.link_id.casefold() == args.link_id.casefold()]
    if not matches:
        print(f"sidepulse unlink: No linked phone has ID {args.link_id!r}.", file=sys.stderr)
        return 1
    if len(matches) != 1:
        print(
            f"sidepulse unlink: ID {args.link_id!r} matches multiple links. "
            "Remove the intended entry from links.json by its full token.",
            file=sys.stderr,
        )
        return 1

    try:
        removed = remove_ios_link(matches[0].token)
    except OSError as exc:
        print(f"sidepulse unlink: Could not save the linked phones: {exc}", file=sys.stderr)
        return 1
    if removed is None:
        print("sidepulse unlink: The linked phone has already been removed.", file=sys.stderr)
        return 1
    print(f"Unlinked {removed.name} ({removed.link_id}).")
    return 0


def cmd_sidepulse_status_bar(args: argparse.Namespace) -> int:
    if args.status_bar_command == "magsafe-helper-status":
        from .magsafe import native_service
        states = {0: "not registered", 1: "enabled", 2: "requires approval", 3: "not found"}
        try:
            status = int(native_service().status())
        except Exception as exc:
            print(f"MagSafe helper: {exc}", file=sys.stderr)
            return 1
        print(f"MagSafe helper: {states.get(status, 'unknown')}")
        return 0
    if args.status_bar_command == "uninstall-magsafe-helper":
        if args.dry_run:
            print("MagSafe helper: would unregister")
            return 0
        try:
            from .magsafe import unregister_helper
            unregister_helper()
        except Exception as exc:
            print(f"MagSafe helper: {exc}", file=sys.stderr)
            return 1
        print("MagSafe helper: unregistered")
        return 0
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
    if getattr(args, "uninstall", False):
        return cmd_uninstall(args)
    results = install_hook_results(args)
    print_install_results(results, dry_run=args.dry_run)

    from .service_launch import install_service

    service_result = install_service(start=True, dry_run=args.dry_run)
    service_action = "would install" if args.dry_run else "installed"
    if service_result.started:
        service_action += " and started"
    elif service_result.detail and not args.dry_run:
        service_action += f"; not started ({service_result.detail})"
    print(f"background service: {service_action}")
    if str(service_result.path):
        print(f"  config: {service_result.path}")

    if sys.platform != "darwin":
        print("macOS integrations: skipped (CLI-only setup on this platform)")
        print("sidepulse: ready for linked phones and mounted SidePulse devices")
        return 0

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


def cmd_sidepulse_service(args: argparse.Namespace) -> int:
    from .service_launch import install_service, service_is_running, stop_service

    command = args.service_command
    if command == "run":
        from .service import run_service

        return run_service()
    if command == "stop":
        stopped = stop_service()
        print(f"background service: {'stopped' if stopped else 'not running'}")
        return 0
    if command == "start":
        result = install_service(start=True)
        action = "started" if result.started else "installed but not started"
        print(f"background service: {action}")
        print(f"  config: {result.path}")
        if result.detail:
            print(f"  {result.detail}")
        return 0 if result.started else 1

    running = service_is_running()
    print(f"background service: {'running' if running else 'not running'}")
    return 0 if running else 1


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


def positive_interval(value: str) -> float:
    try:
        interval = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("interval must be a positive finite number") from None
    if not math.isfinite(interval) or interval <= 0:
        raise argparse.ArgumentTypeError("interval must be a positive finite number")
    return interval


def add_status_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="Print one JSON snapshot per line.")
    parser.add_argument("--all", action="store_true", help="Include inactive sessions from the daemon.")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI color output.")


def cmd_doctor(args: argparse.Namespace) -> int:
    configs = detect_provider_configs()
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
    return _show_daemon_status(args, live=False)


def cmd_live(args: argparse.Namespace) -> int:
    return _show_daemon_status(args, live=True)


def _show_daemon_status(args: argparse.Namespace, *, live: bool) -> int:
    from .program_presentation import DaemonMonitor

    monitor = DaemonMonitor()
    interactive = live and not args.json and not args.once and sys.stdout.isatty()
    color = not args.json and should_use_color(args.no_color)
    try:
        if interactive:
            print("\033[?25l", end="")
        while True:
            snapshot = monitor.snapshot(include_stale=args.all)
            if interactive:
                print("\033[2J\033[H", end="")
            if args.json:
                print(json.dumps(snapshot.to_dict()))
            else:
                print(render_status_view(snapshot, include_stale=args.all, color=color))
            sys.stdout.flush()
            if not live or args.once:
                return 0
            time.sleep(args.interval)
    except (OSError, ValueError) as exc:
        print(f"sidepulse: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        if interactive:
            print("\033[?25h", end="", flush=True)


def cmd_sidepulse_update(args: argparse.Namespace) -> int:
    from .update import update_installation

    return update_installation(dry_run=args.dry_run)


def cmd_sidepulse_settings(_args: argparse.Namespace) -> int:
    if sys.platform != "darwin":
        print("SidePulse settings requires macOS.", file=sys.stderr)
        return 1
    from .ipc import request_settings_window
    from .status_bar_launch import install_launch_agent

    if request_settings_window():
        return 0
    install_launch_agent(start=True)
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if request_settings_window():
            return 0
        time.sleep(0.1)
    print("Could not open SidePulse settings. Check the status-bar logs.", file=sys.stderr)
    return 1


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

    # Explicit starts restore the icon. LaunchAgent starts use --foreground
    # above, so login and automatic restarts keep the saved visibility choice.
    if not args.no_start:
        settings = load_settings()
        if not settings.show_menu_bar_icon:
            save_settings(settings.with_menu_bar_icon(True))
    result = install_launch_agent(start=not args.no_start)
    action = "installed" if result.changed else "already installed"
    if result.started:
        action += " and started"
    print(f"status-bar: {action}")
    print(f"  plist: {result.plist_path}")
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
        elif provider == "cursor":
            results.append(install_cursor_hooks(log_path=log_path, dry_run=args.dry_run))
        elif provider == "grok":
            results.append(install_grok_hooks(log_path=log_path, dry_run=args.dry_run))
        else:
            results.append(install_junie_hooks(log_path=log_path, dry_run=args.dry_run))
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
        if result.provider == "codex" and result.changed and not dry_run:
            print("  Restart Codex to activate these hooks in new sessions.")


def cmd_uninstall(args: argparse.Namespace) -> int:
    providers = selected_hook_providers(args.provider)
    results = []
    for provider in providers:
        log_path = uninstall_log_path(provider, args)
        if provider == "codex":
            results.append(uninstall_codex_hooks(log_path=log_path, dry_run=args.dry_run))
        elif provider == "claude":
            results.append(uninstall_claude_hooks(log_path=log_path, dry_run=args.dry_run))
        elif provider == "cursor":
            results.append(uninstall_cursor_hooks(log_path=log_path, dry_run=args.dry_run))
        elif provider == "grok":
            results.append(uninstall_grok_hooks(log_path=log_path, dry_run=args.dry_run))
        else:
            results.append(uninstall_junie_hooks(log_path=log_path, dry_run=args.dry_run))

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
    return hook_log_main(args.provider, args.log, event=getattr(args, "event", None))


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


def render_status_view(
    snapshot,
    include_stale: bool = False,
    color: bool = False,
) -> str:
    width = max(80, shutil.get_terminal_size((120, 24)).columns)
    statuses = visible_statuses(snapshot, include_stale)
    aggregate = snapshot.aggregate
    timestamp = snapshot.collected_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    title = colorize("SidePulse", "1", color)
    aggregate_text = colorize(aggregate.mode_label, mode_color(aggregate.mode), color)

    lines = [
        f"{title}  aggregate={aggregate_text}  sessions={len(statuses)}  updated={timestamp}",
        f"active={aggregate.active_count}  inactive={aggregate.stale_count}",
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
        lines.append("reason: no active session status")

    lines.extend(["", "Sessions"])
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


def visible_statuses(snapshot, include_stale: bool = False) -> list[AgentStatus]:
    statuses = list(snapshot.statuses)
    if include_stale:
        statuses.extend(snapshot.stale_statuses)
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


def cmd_wrap(args: argparse.Namespace) -> int:
    from .terminal_wrap import run_wrapped
    command = args.argv[1:] if args.argv[:1] == ['--'] else args.argv
    if not command:
        print("Usage: sidepulse wrap COMMAND [ARGS ...]", file=sys.stderr)
        return 2
    try:
        return run_wrapped(command)
    except (OSError, ValueError) as exc:
        print(f"Could not wrap command: {exc}", file=sys.stderr)
        return 1


def cmd_report(args: argparse.Namespace) -> int:
    from .program_transport import submit_status
    from .program_status import StatusError, MAX_BYTES
    try:
        if args.json:
            raw = sys.stdin.read(MAX_BYTES + 1)
            if len(raw.encode()) > MAX_BYTES:
                raise StatusError("Report too large")
            report = json.loads(raw)
            if not isinstance(report, dict):
                raise StatusError("Report must be an object")
        elif args.osc is not None:
            from .osc7501 import MAX_OSC_BYTES, parse_osc7501
            if not args.scope:
                raise StatusError("Supply --scope with --osc")
            if any(getattr(args, key) is not None for key in ("app", "id", "title", "msg", "kind", "progress")):
                raise StatusError("Put status fields inside the OSC sequence when using --osc")
            raw = sys.stdin.read(MAX_OSC_BYTES + 3) if args.osc == "-" else args.osc
            report = {"v": 1, "type": "program_status", "scope": args.scope,
                      "status": parse_osc7501(raw)}
        else:
            if not args.scope or not args.state:
                raise StatusError("Supply --scope and --state")
            status = {key: getattr(args, key) for key in ("state", "app", "id", "title", "msg", "kind", "progress") if getattr(args, key) is not None}
            report = {"v": 1, "type": "program_status", "scope": args.scope, "status": status}
        report.setdefault("request_id", args.request_id or str(uuid.uuid4()))
        print(json.dumps(submit_status(report)))
        return 0
    except (ValueError, OSError) as exc:
        print(f"Could not submit status: {exc}. Check sidepulse service status.", file=sys.stderr)
        return 1
