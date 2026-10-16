#!/usr/bin/env python3
"""Send one test session through the existing bridge to a linked iPhone.

Run with ~/.local/share/sidepulse/venv/bin/python scripts/push-session-test.py working
Use clear to remove only this test publisher's session. No credentials are printed.
"""
import argparse
import time

from sidepulse.links import load_phone_links
from sidepulse.program_transport import publish_snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('state', nargs='?', default='working', choices=['working', 'blocked', 'error', 'done', 'idle', 'clear'])
    parser.add_argument('--to', help='Linked iPhone name or ID when more than one supports sessions.')
    args = parser.parse_args()
    phones = [p for p in load_phone_links() if p.platform == 'ios' and p.status_protocol == 1
              and (not args.to or args.to in (p.name, p.link_id))]
    if len(phones) != 1:
        parser.error('Choose one session-capable iPhone with --to, or refresh its link using sidepulse link.')
    now = time.time_ns() // 1_000_000
    status = {'state': args.state, 'app': 'sidepulse', 'title': 'Phone connection test',
              'msg': 'Test session sent from your Mac.'}
    if args.state == 'working': status['progress'] = 35
    if args.state == 'blocked': status['kind'] = 'permission'
    scopes = {} if args.state == 'clear' else {'phone-test': {'seq': now, 'records': [{'status': status, 'updated_at': now}]}}
    snapshot = {'v': 1, 'type': 'program_status_snapshot', 'publisher': 'sidepulse-phone-test', 'revision': now, 'scopes': scopes}
    phone = phones[0]
    try:
        publish_snapshot(phone.server, 'apns_' + phone.token, snapshot, phone=True)
    except Exception:
        parser.exit(1, 'Bridge did not acknowledge the test. Check the phone link and connection.\n')
    print(f'Bridge accepted {args.state} test for {phone.name}. Open SidePulse → Sessions to verify receipt.')


if __name__ == '__main__':
    main()
