"""Persistent Dot USB storage control, including safe host ejection."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import time

from . import usb_control as usb
from .firmware import USB_DEVICE_ROOT, _usb_address
from .readonly_control import _plist_command, usb_address_for_volume


def find_dots():
    from .usb_discovery import dots

    return [dict(info, device=str(info['root'])) for info in dots(force=True)]


def select_dots(dots, selector):
    if selector is None:
        return dots
    if selector.startswith('SPD-'):
        return [dot for dot in dots if dot['serial'] == selector]
    root = Path(selector).expanduser()
    address = _usb_address(root)
    if address is None:
        address = usb_address_for_volume(root)
    return [dot for dot in dots if (dot['bus'], dot['address']) == address]


def eject_storage(bus, address):
    """Eject only media belonging to the selected USB connection, without force."""
    if sys.platform == 'darwin':
        tree = _plist_command([
            '/usr/sbin/ioreg', '-a', '-l', '-p', 'IOService', '-r', '-c', 'IOUSBHostDevice',
        ])
        disks = set()

        def visit(node, selected=False):
            if node.get('IOObjectClass') == 'IOUSBHostDevice':
                selected = ((node.get('idVendor'), node.get('idProduct')) == (0x1a86, 0xfe10)
                            and node.get('locationID', 0) >> 24 == bus
                            and node.get('USB Address') == address)
            name = node.get('BSD Name', '')
            if selected and re.fullmatch(r'disk\d+', name):
                disks.add(name)
            for child in node.get('IORegistryEntryChildren', []):
                visit(child, selected)

        for node in tree:
            visit(node)
        for disk in sorted(disks):
            subprocess.run(['/usr/sbin/diskutil', 'eject', '/dev/' + disk],
                           check=True, capture_output=True, timeout=15)
    elif sys.platform.startswith('linux'):
        # Match the USB parent, then unmount any mounted block children.
        mounted = {line.split()[2] for line in Path('/proc/self/mountinfo').read_text().splitlines()}
        for node in Path('/sys/bus/usb/devices').iterdir():
            if not (node / 'busnum').exists() or not (node / 'devnum').exists():
                continue
            if (int((node / 'busnum').read_text()), int((node / 'devnum').read_text())) != (bus, address):
                continue
            for block in node.glob('**/block/*'):
                for media in [*block.iterdir(), block]:
                    if (media / 'dev').is_file() and (media / 'dev').read_text().strip() in mounted:
                        subprocess.run(['udisksctl', 'unmount', '-b', '/dev/' + media.name],
                                       check=True, capture_output=True, timeout=15)
    else:
        raise RuntimeError('Automatic storage ejection requires macOS or Linux.')


def wait_for_storage(original, enabled):
    deadline = time.monotonic() + 10
    # The firmware intentionally detaches for two seconds. Allow the host to
    # observe removal before looking for the same serial at its new address.
    time.sleep(2.1)
    while True:
        matches = [dot for dot in find_dots() if dot['serial'] == original['serial']]
        if len(matches) == 1:
            dot = matches[0]
            try:
                with usb.Device(bus=dot['bus'], address=dot['address']) as device:
                    status = device.storage_status()
                if status == dict(saved=enabled, active=enabled, state='READY'):
                    return dict(dot, **status)
            except (OSError, ValueError):
                pass
        if time.monotonic() >= deadline:
            raise RuntimeError('USB reconnect could not be verified. The setting is saved; '
                               'run sidepulse storage status or unplug and reconnect the Dot.')
        time.sleep(0.2)


def cmd_storage(args):
    try:
        dots = select_dots(find_dots(), args.device)
        if not dots:
            raise RuntimeError('No compatible SidePulse Dot found. Connect a Dot with storage-control firmware; '
                               'older firmware needs an update. SidePulse Pro does not support this setting.')
        if args.storage_action != 'status' and len(dots) != 1:
            choices = ', '.join(f"{dot['serial']} ({dot['device']})" for dot in dots)
            raise RuntimeError(f'Multiple Dots found: {choices}. Select one with --device SERIAL or USB selector.')
        results = []
        for dot in dots:
            with usb.Device(bus=dot['bus'], address=dot['address']) as device:
                status = device.storage_status()
                if args.storage_action != 'status':
                    enabled = args.storage_action == 'on'
                    if status['state'] == 'BUSY':
                        raise RuntimeError('The Dot is saving a setting; try again after it finishes.')
                    reconnect = status['active'] != enabled
                    if reconnect and status['active']:
                        eject_storage(dot['bus'], dot['address'])
                    if status['saved'] != enabled or status['state'] == 'FAILED':
                        status = device.set_storage(enabled)
                    if reconnect:
                        device.reconnect()
            result = dict(dot, **status)
            if args.storage_action != 'status' and reconnect:
                result = wait_for_storage(dot, enabled)
            results.append({key: result[key] for key in ('serial', 'device', 'saved', 'active', 'state')})
        if args.json:
            print(json.dumps(results, indent=2))
        else:
            for result in results:
                mode = lambda value: 'on' if value else 'off'
                print(f"{result['serial']}: USB storage {mode(result['active'])} "
                      f"(saved: {mode(result['saved'])}, {result['state'].lower()})"
                      + (f" — {result['device']}" if args.storage_action == 'status' else ''))
        return 0
    except (ImportError, OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        detail = exc.stderr.decode(errors='replace').strip() if isinstance(exc, subprocess.CalledProcessError) and exc.stderr else str(exc)
        print(f'SidePulse storage failed: {detail}', file=sys.stderr)
        return 1


def add_storage_parser(subparsers: argparse._SubParsersAction):
    storage = subparsers.add_parser('storage', help='Turn Dot USB storage on or off, or show its setting.')
    commands = storage.add_subparsers(dest='storage_action', required=True)
    for action, help_text in [('on', 'Enable USB storage and reconnect automatically.'),
                              ('off', 'Safely eject, disable USB storage, and reconnect automatically.'),
                              ('status', 'Show saved and active USB storage settings.')]:
        command = commands.add_parser(action, help=help_text)
        command.add_argument('--device', help='Dot serial, USB selector, or mounted drive; required to change one of multiple Dots.')
        command.add_argument('--json', action='store_true', help='Print storage settings as JSON.')
        command.set_defaults(func=cmd_storage)
