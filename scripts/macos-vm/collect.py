#!/usr/bin/env python3
"""Receive one guest evidence archive on the host's UTM-only network interface."""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import ipaddress
from pathlib import Path
import secrets
import shlex
import time

MAX_BYTES = 8 * 1024 * 1024 * 1024  # Bound full-resolution recordings from both agents.


def receive(bind, output, timeout=300):
    address = ipaddress.ip_address(bind)
    if not address.is_private or address.is_unspecified:
        raise ValueError("Bind to the host's specific private UTM interface address")
    output.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    destination = output / ("guest-evidence-" + time.strftime("%Y%m%d-%H%M%S") + ".tar.gz")
    received = False

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            nonlocal received
            if not secrets.compare_digest(self.path, "/" + token):
                self.send_error(403)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if not 0 < length <= MAX_BYTES:
                self.send_error(413)
                return
            self.connection.settimeout(30)
            partial = destination.with_suffix(".partial")
            try:
                with partial.open("xb") as stream:
                    remaining = length
                    while remaining:
                        chunk = self.rfile.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise OSError("Incomplete upload")
                        stream.write(chunk)
                        remaining -= len(chunk)
                partial.rename(destination)
            except OSError:
                partial.unlink(missing_ok=True)
                self.send_error(400)
                return
            received = True
            self.send_response(201)
            self.end_headers()
            self.wfile.write(b"Evidence saved on host.\n")

        def log_message(self, *args):
            pass  # The one-time upload URL is not logged.

    server = HTTPServer((bind, 0), Handler)
    server.timeout = 1
    url = f"http://{bind}:{server.server_port}/{token}"
    launcher = output / "collect-results.command"
    launcher.write_text('''#!/bin/sh
set -eu
case "$(sysctl -n hw.model)" in VirtualMac*) ;; *) echo 'Guest only'; exit 2 ;; esac
ARCHIVE=$(mktemp -t sidepulse-evidence)
trap 'rm -f "$ARCHIVE"' EXIT
tar -czf "$ARCHIVE" -C "$HOME" sidepulse-vm-results
curl --fail --silent --show-error --max-time 180 --request POST --upload-file "$ARCHIVE" ''' + shlex.quote(url) + '\n')
    launcher.chmod(0o700)
    print(f"Run sh '/Volumes/My Shared Files/macos-vm/{launcher.name}' in the guest.", flush=True)
    deadline = time.monotonic() + timeout
    try:
        while not received and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
        launcher.unlink(missing_ok=True)
    if not received:
        raise TimeoutError("No guest evidence received before timeout")
    digest_state = hashlib.sha256()
    with destination.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest_state.update(chunk)
    digest = digest_state.hexdigest()
    destination.with_suffix(".sha256").write_text(digest + "\n")
    print(f"Saved: {destination}\nSHA256: {digest}", flush=True)
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", required=True, help="Host UTM bridge IP, e.g. 192.168.64.1")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[2] / "build/macos-vm")
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    receive(args.bind, args.output, args.timeout)
