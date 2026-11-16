"""List attached SidePulse hardware and selectors accepted by --device."""
import json

from .device_writer import discover_devices


def connected_devices():
    return [dict(model=device.model or device.root.name, serial=device.serial, version=device.version,
                 connection=device.connection, device=str(device.root))
            for device in discover_devices()]


def cmd_list(args):
    devices = connected_devices()
    if args.json:
        print(json.dumps(devices, indent=2))
    elif not devices:
        print('No SidePulse devices found.')
    else:
        rows = [('MODEL', 'SERIAL', 'FIRMWARE', 'CONNECTION', 'DEVICE')]
        rows.extend(tuple(device[key] for key in ('model', 'serial', 'version', 'connection', 'device'))
                    for device in devices)
        widths = [max(len(row[i]) for row in rows) for i in range(4)]
        for row in rows:
            print('  '.join(value.ljust(width) for value, width in zip(row[:4], widths)) + '  ' + row[4])
    return 0


def add_list_parser(subparsers):
    command = subparsers.add_parser('list', aliases=['ls'], help='List connected SidePulse devices and their --device selectors.')
    command.add_argument('--json', action='store_true', help='Print connected devices as JSON.')
    command.set_defaults(func=cmd_list)
