"""Report agents from Herdr's enabled saved machines through the local daemon."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from collections.abc import Callable

from .herdr import STATES, _agent_report, _cli, _digest, _registration, executable_path, terminal_scope
from .models import REMOTE_HERDR_ORIGIN
from .program_adapter import clean_text
from .program_status import CONTEXT_LIMITS, SESSION_CLOSED_EVENT, StatusError


POLL_SECONDS = 5
RETRY_SECONDS = 20


class RemoteHerdrMonitor:
    def __init__(self, executable: str, stop: threading.Event, submit: Callable[[dict], dict]):
        self.executable = executable
        self.stop = stop
        self.submit = submit
        self.reported: dict[str, dict[str, dict]] = {}
        self.retry_at: dict[str, float] = {}

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                self.poll()
            except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
                print(f"sidepulse Herdr monitor: {exc}", file=sys.stderr)
            self.stop.wait(POLL_SECONDS)

    def poll(self) -> None:
        registration = _registration(self.executable)
        machines = []
        if registration and registration.get("enabled"):
            machines = json.loads(_cli(
                self.executable, "machine", "list", "--json", timeout=3, stdin=subprocess.DEVNULL,
            ))
            if not isinstance(machines, list) or any(not isinstance(machine, dict) for machine in machines):
                raise StatusError("Invalid Herdr saved-machine list")
        enabled = {machine["id"]: machine for machine in machines if machine.get("enabled") is True}
        for machine_id in self.reported.keys() - enabled.keys():
            self.publish(machine_id, {})
            self.reported.pop(machine_id, None)
        self.retry_at = {machine_id: value for machine_id, value in self.retry_at.items() if machine_id in enabled}

        for machine_id, machine in enabled.items():
            if self.stop.is_set():
                return
            if time.monotonic() < self.retry_at.get(machine_id, 0):
                continue
            origin = clean_text(f"{REMOTE_HERDR_ORIGIN}{machine['label']}", CONTEXT_LIMITS["origin"] - 20)
            try:
                agents = json.loads(_cli(
                    self.executable, "--machine", machine_id, "agent", "list",
                    timeout=3, stdin=subprocess.DEVNULL,
                ))["result"]["agents"]
                if not isinstance(agents, list) or any(not isinstance(agent, dict) for agent in agents):
                    raise StatusError("Invalid Herdr remote agent list")
            except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
                self.retry_at[machine_id] = time.monotonic() + RETRY_SECONDS
                self.publish(machine_id, {
                    scope: {
                        **report,
                        "status": {**report["status"], "state": "idle"},
                        "context": {**report["context"], "origin": origin + " · Disconnected"},
                    }
                    for scope, report in self.reported.get(machine_id, {}).items()
                })
                continue

            self.retry_at.pop(machine_id, None)
            prefix = "herdr-" + _digest("machine:" + machine_id) + "-"
            reports = {}
            for agent in agents:
                terminal_id = agent.get("terminal_id")
                state = agent.get("agent_status")
                if (not isinstance(terminal_id, str) or not terminal_id
                        or not isinstance(agent.get("agent"), str) or not agent["agent"]
                        or not isinstance(state, str) or state not in STATES):
                    continue
                scope = terminal_scope(prefix, terminal_id)
                report = _agent_report(scope, agent)
                report["context"]["origin"] = origin
                reports[scope] = report
            self.publish(machine_id, reports, ended=True)

    def publish(self, machine_id: str, reports: dict[str, dict], *, ended: bool = False) -> None:
        previous = self.reported.setdefault(machine_id, {})
        for scope in previous.keys() - reports.keys():
            if self.stop.is_set():
                return
            report = {"v": 1, "type": "program_status", "scope": scope, "status": {"state": "clear"}}
            if ended:
                report["context"] = {"event_name": SESSION_CLOSED_EVENT}
            self.submit(report)
            del previous[scope]
        for scope, report in reports.items():
            if self.stop.is_set():
                return
            if previous.get(scope) != report:
                self.submit(report)
                previous[scope] = report


def monitor_remote_herdr(stop: threading.Event, submit: Callable[[dict], dict]) -> None:
    executable = executable_path()
    if executable:
        RemoteHerdrMonitor(executable, stop, submit).run()
