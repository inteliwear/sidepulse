"""Supervise a separate T3 Code log scanner and accept its local status reports."""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

from .program_status import ProgramState, StatusError, integer
from .program_transport import submit_report
from .t3code import T3CodeSource, monitoring_status


TOKEN_ENV = "SIDEPULSE_T3_SCANNER_TOKEN"
COMMANDS = {"t3_report", "t3_diagnostics"}


class T3Scanner:
    def __init__(self, state: ProgramState, socket_path: Path):
        self.state = state
        self.socket_path = socket_path
        self.lock = threading.RLock()
        self.process: subprocess.Popen | None = None
        self.token: str | None = None
        self.enabled = False
        self.next_start = 0.0
        self.status: dict = {}
        self.initial_status = monitoring_status()

    def sync(self, enabled: bool) -> None:
        if not enabled:
            if self.enabled:
                self.close()
                self.state.clear_t3_scan_sessions()
            return

        with self.lock:
            self.enabled = True
            if self.process is not None:
                if self.process.poll() is None:
                    return
                self.process.stdin.close()
                self.process = None
                self.token = None
                self.status = {}

            now = time.monotonic()
            if now < self.next_start:
                return
            self.next_start = now + 5
            token = secrets.token_hex(16)
            command = [sys.executable]
            if not getattr(sys, "frozen", False):
                command += ["-m", "sidepulse"]
            command += ["service", "scan-t3", "--socket", str(self.socket_path)]
            self.process = subprocess.Popen(
                command, stdin=subprocess.PIPE, env={**os.environ, TOKEN_ENV: token},
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            self.token = token

    def close(self) -> None:
        with self.lock:
            process, self.process = self.process, None
            self.token = None
            self.enabled = False
            self.status = {}
            self.next_start = 0.0
        if process is not None:
            # The scanner also exits on EOF if the daemon exits unexpectedly.
            process.stdin.close()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    def diagnostics(self) -> dict:
        with self.lock:
            return {
                **self.initial_status,
                **self.status,
                "enabled": self.enabled,
            }

    def handle_command(self, command: dict) -> dict:
        with self.lock:
            if self.token is None or command.get("token") != self.token:
                raise StatusError("T3 Code scanner is inactive")
            if command["type"] == "t3_diagnostics":
                status = command.get("status")
                if not isinstance(status, dict):
                    raise StatusError("Invalid T3 Code diagnostics")
                self.status = status
                return {"ok": True}

            observed_at = integer(command.get("observed_at"), "observed_at")
            wait_cleared_at = command.get("wait_cleared_at")
            if wait_cleared_at is not None:
                wait_cleared_at = integer(wait_cleared_at, "wait_cleared_at")
                if wait_cleared_at > observed_at:
                    raise StatusError("Wait release is newer than the T3 Code report")
            return self.state.submit(
                command.get("report"), observed_at=observed_at, wait_cleared_at=wait_cleared_at,
                t3_scan=True,
            )


def run_scanner(socket_path: Path) -> int:
    token = os.environ.get(TOKEN_ENV)
    if not token:
        print("T3 Code scanning is started by the SidePulse service.", file=sys.stderr)
        return 1

    stop = threading.Event()

    def watch_parent() -> None:
        try:
            sys.stdin.buffer.read()
        finally:
            stop.set()

    threading.Thread(target=watch_parent, daemon=True).start()

    def send(kind: str, **fields) -> dict:
        return submit_report({"v": 1, "type": kind, "token": token, **fields}, path=socket_path)

    def submit(report: dict, *, observed_at: int, wait_cleared_at: int | None = None) -> dict:
        return send("t3_report", report=report, observed_at=observed_at, wait_cleared_at=wait_cleared_at)

    source = T3CodeSource(submit)
    last_status = None
    while not stop.is_set():
        try:
            source.poll()
            status = source.diagnostics()
            if status != last_status:
                send("t3_diagnostics", status=status)
                last_status = status
        except (OSError, StatusError) as exc:
            print(f"sidepulse T3 Code scanner: {exc}", file=sys.stderr)
        stop.wait(1)
    return 0
