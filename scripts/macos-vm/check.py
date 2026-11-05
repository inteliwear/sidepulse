#!/usr/bin/env python3
"""Black-box checks against the guest's installed SidePulse and real agent CLIs."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import tarfile
import tempfile
import time
try:
    import tomllib
except ModuleNotFoundError:  # Repository's Python 3.10 test environment.
    import tomli as tomllib
import uuid
import xml.etree.ElementTree as ET

PROVIDERS = ("claude", "codex")
EVENTS = (
    ("UserPromptSubmit", "working"),
    ("PreToolUse", "tool_running"),
    ("PermissionRequest", "waiting_for_input"),
    ("PostToolUse", "working"),
    ("Stop", "completed"),
)


def run(argv, *, timeout=60, **kwargs):
    """Bound the entire subprocess tree, including spawned agent tools."""
    with subprocess.Popen(argv, start_new_session=True, **kwargs) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise RuntimeError(f"Timed out after {timeout}s: {argv[0]}") from None
        if process.returncode:
            raise RuntimeError(f"{argv[0]} exited {process.returncode}; see command log")
        return stdout, stderr


def installed_commands(provider, home):
    path = home / (".claude/settings.json" if provider == "claude" else ".codex/config.toml")
    data = json.loads(path.read_text()) if provider == "claude" else tomllib.loads(path.read_text())
    result = {}
    for event, entries in data.get("hooks", {}).items():
        # Codex stores hook-trust metadata alongside event arrays in hooks.state.
        # Only event arrays contain executable hook entries.
        if not isinstance(entries, list):
            continue
        commands = [hook["command"] for entry in entries for hook in entry.get("hooks", [])
                    if hook.get("type") == "command" and "sidepulse" in hook.get("command", "")]
        if commands:
            if len(commands) != 1:
                raise AssertionError(f"Duplicate {provider}/{event} hooks: {len(commands)}")
            result[event] = commands[0]
    return result


def read_records(path, offset=0):
    if not path.exists():
        return []
    with path.open("rb") as stream:
        stream.seek(offset)
        rows = []
        for line in stream:
            try:
                row = json.loads(line)
                if isinstance(row, dict):
                    if isinstance(row.get("event"), dict):
                        row = {**row, **row["event"]}  # Codex wraps native payloads.
                    rows.append(row)
            except (ValueError, UnicodeError):
                continue  # A writer may still be appending the last line.
        return rows


def state_rows(reply):
    snapshot = reply["snapshot"]
    return snapshot["statuses"] + snapshot.get("stale_statuses", [])


def legacy_snapshot(path):
    """Observe main's persisted live monitor state, without interpreting hooks."""
    data = json.loads(path.read_text())
    if not isinstance(data.get("statuses"), list) or not data.get("updated_at"):
        raise ValueError("Invalid legacy live monitor state")
    return {"ok": True, "evidence_source": "legacy-live-monitor-file",
            "state_path": str(path), "state_file_mtime": path.stat().st_mtime,
            "snapshot": {**data, "stale_statuses": []}}


def installed_snapshot(timeout=2):
    try:
        from sidepulse.program_transport import daemon_command
    except ModuleNotFoundError as exc:
        if exc.name != "sidepulse.program_transport":
            raise
        from sidepulse.ipc import default_latest_state_path
        return legacy_snapshot(default_latest_state_path())
    return {**daemon_command({"type": "get_state"}, timeout=timeout),
            "evidence_source": "program-daemon-socket"}


def install_codex_package(archive, home):
    """Keep the vendor's layout: Codex resolves helper binaries beside itself."""
    packages = home / ".local/share/sidepulse-vm-agents"
    packages.mkdir(parents=True, exist_ok=True)
    destination = Path(tempfile.mkdtemp(prefix="codex-", dir=packages))
    with tarfile.open(archive) as tar:
        members = tar.getmembers()
        if any(Path(m.name).is_absolute() or ".." in Path(m.name).parts
               or not (m.isfile() or m.isdir()) for m in members):
            raise RuntimeError("Unsafe Codex package archive")
        names = {m.name for m in members if m.isfile()}
        if not {"bin/codex", "bin/codex-code-mode-host", "codex-package.json"} <= names:
            raise RuntimeError("Incomplete Codex package: CLI and code-mode helper are required")
        tar.extractall(destination, members=members, filter="data")
    bindir = home / ".local/bin"
    bindir.mkdir(parents=True, exist_ok=True)
    for name in ("codex", "codex-code-mode-host"):
        executable = destination / "bin" / name
        executable.chmod(0o755)
        link = bindir / (name + ".vm-new")
        link.symlink_to(executable)
        link.replace(bindir / name)
    return destination


def live_evidence(provider, records, snapshots, cwd, token, output):
    """Only this run's workspace/session can satisfy the live assertions."""
    sessions = {r["session_id"] for r in records if r.get("cwd") == str(cwd) and r.get("session_id")}
    rows = [r for r in records if r.get("session_id") in sessions]
    events = {r.get("hook_event_name") for r in rows}
    missing = {"UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"} - events
    if missing:
        raise AssertionError(f"{provider} did not emit installed hooks: {sorted(missing)}")
    if token not in output:
        raise AssertionError("Agent did not return the fixture token; task execution is unverified")
    seen = [r for reply in snapshots for r in state_rows(reply)
            if r.get("provider") == provider and r.get("session_id") in sessions]
    modes = {r["mode"] for r in seen}
    if "completed" not in modes or not modes.intersection({"working", "tool_running"}):
        raise AssertionError(f"Daemon did not observe working and completed for this run: {sorted(modes)}")
    return {"events": sorted(events), "modes": sorted(modes), "sessions": sorted(sessions)}


class Harness:
    def __init__(self, output):
        self.output = output
        output.mkdir(parents=True, exist_ok=False)
        self.results = []
        self.home = Path.home()
        self.cli = str(self.home / ".local/bin/sidepulse")
        self.state = self.home / ".local/state/sidepulse/agent-monitor"
        manifest = Path(__file__).resolve().parents[2] / "vm-source-manifest.json"
        if manifest.exists():
            shutil.copyfile(manifest, output / "source-manifest.json")

    def check(self, name, callback):
        try:
            detail = callback()
            result = {"name": name, "status": "passed", "detail": detail}
        except Exception as exc:
            result = {"name": name, "status": "failed", "detail": str(exc)}
        self.results.append(result)
        print(f"{result['status'].upper()}: {name}: {result['detail']}", flush=True)

    def command(self, name, argv, timeout=120):
        path = self.output / f"{name}.log"
        with path.open("w") as log:
            run(argv, stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
        return path.read_text().strip()

    def snapshot(self):
        return installed_snapshot()

    def wait_mode(self, provider, session, mode):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                reply = self.snapshot()
            except (FileNotFoundError, json.JSONDecodeError):
                # Main creates its legacy state file asynchronously after the
                # first hook; a missing or in-progress write is not a result.
                time.sleep(.15)
                continue
            if any(r.get("provider") == provider and r.get("session_id") == session
                   and r["mode"] == mode for r in state_rows(reply)):
                return reply
            time.sleep(.15)
        raise AssertionError(f"Daemon did not reach {provider}/{session}/{mode}")

    def replay(self, provider):
        commands = installed_commands(provider, self.home)
        session = "vm-replay-" + uuid.uuid4().hex
        evidence = []
        log = self.state / f"{provider}.jsonl"
        offset = log.stat().st_size if log.exists() else 0
        try:
            for event, mode in EVENTS:
                payload = {"hook_event_name": event, "session_id": session,
                           "cwd": str(self.output), "prompt": "SidePulse VM smoke check",
                           "tool_name": "Bash", "tool_use_id": "vm-tool",
                           "tool_input": {"command": "printf sidepulse"}}
                # Execute the command actually installed into the agent config.
                with subprocess.Popen(["/bin/sh", "-c", commands[event]], stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      text=True, start_new_session=True) as process:
                    try:
                        out, err = process.communicate(json.dumps(payload), timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.communicate()
                        raise AssertionError(f"Hook hung: {event}") from None
                if process.returncode != 0 or out:
                    raise AssertionError(f"Hook must exit 0 with empty stdout: {event}")
                reply = self.wait_mode(provider, session, mode)
                evidence.append({"event": event, "expected": mode, "reply": reply})
            logged = {r.get("hook_event_name") for r in read_records(log, offset)
                      if r.get("session_id") == session}
            if not {e for e, _ in EVENTS}.issubset(logged):
                raise AssertionError("Fail-open hooks returned success but did not log every event")
            return [e for e, _ in EVENTS]
        finally:
            (self.output / f"replay-{provider}.json").write_text(json.dumps(evidence, indent=2))
            try:
                from sidepulse.program_transport import submit_report
            except ModuleNotFoundError as exc:
                if exc.name != "sidepulse.program_transport":
                    raise
                # Main has no session-clear API. Unique session IDs isolate
                # each replay; retain its completed state as original evidence.
            else:
                scope = provider + "-" + hashlib.sha256(session.encode()).hexdigest()[:32]
                submit_report({"v": 1, "type": "program_status", "scope": scope, "status": {"state": "clear"}})

    def device(self):
        path = Path("/Volumes/SidePulse")
        if not (path / "LEDS.LED").is_file():
            raise AssertionError("Missing /Volumes/SidePulse/LEDS.LED; run mount-sidepulse.sh")
        # Both main and the branch expose discovery here; main predates the
        # branch's `sidepulse list --json` command.
        from sidepulse.device_writer import discover_devices
        devices = list(discover_devices())
        (self.output / "devices.json").write_text(json.dumps(
            [{"root": str(d.root), "target": str(d.target)} for d in devices], indent=2))
        if not any(d.root.resolve() == path.resolve() for d in devices):
            raise AssertionError("Installed SidePulse did not discover the shared device")
        if not os.access(path / "LEDS.LED", os.W_OK):
            raise AssertionError("SidePulse share is read-only")
        # Observe only; do not create probe files on the firmware volume.
        return {"path": str(path), "resolved": str(path.resolve()), "writable": os.access(path / "LEDS.LED", os.W_OK),
                "coverage": "shared filesystem; USB, READ2ME and eject are not tested"}

    def install_agent(self, provider):
        bindir = self.home / ".local/bin"
        bindir.mkdir(parents=True, exist_ok=True)
        if not shutil.which(provider) or (provider == "codex" and not shutil.which("codex-code-mode-host")):
            if provider == "claude":
                installer = self.output / "claude-install.sh"
                run(["curl", "-fLSs", "--retry", "3", "https://claude.ai/install.sh", "-o", str(installer)], timeout=120)
                self.command("claude-install", ["/bin/bash", str(installer)], timeout=600)
            else:
                arch = "aarch64" if platform.machine() == "arm64" else "x86_64"
                name = f"codex-package-{arch}-apple-darwin"
                archive = self.output / "codex.tar.gz"
                url = f"https://github.com/openai/codex/releases/latest/download/{name}.tar.gz"
                run(["curl", "-fLSs", "--retry", "3", url, "-o", str(archive)], timeout=300)
                install_codex_package(archive, self.home)
        return self.command(f"{provider}-version", [provider, "--version"])

    def live(self, provider):
        if not shutil.which(provider):
            raise RuntimeError(f"{provider} is not installed")
        cwd = self.output / f"live-{provider}"
        cwd.mkdir()
        token = "SIDEPULSE_" + uuid.uuid4().hex
        (cwd / "fixture.txt").write_text(token + "\n")
        prompt = ("Use your shell tool to run exactly `sleep 2; cat fixture.txt` in the current directory. "
                  "Then reply with only the file contents. Do not change any files.")
        argv = (["claude", "-p", prompt, "--allowedTools", "Bash(sleep 2; cat fixture.txt)",
                 "--max-turns", "3", "--output-format", "json"] if provider == "claude" else
                ["codex", "exec", "--skip-git-repo-check", "--sandbox", "read-only", "--json", prompt])
        log = self.state / f"{provider}.jsonl"
        offset = log.stat().st_size if log.exists() else 0
        snapshots = []
        output = cwd / "agent.log"
        with output.open("w") as stream, subprocess.Popen(
                argv, cwd=cwd, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True) as process:
            deadline = time.monotonic() + 180
            try:
                while process.poll() is None:
                    if time.monotonic() > deadline:
                        raise RuntimeError("Agent timed out after 180 seconds")
                    snapshots.append(self.snapshot())
                    time.sleep(.2)
                # Completion hooks may finish just after the main process.
                for _ in range(10):
                    snapshots.append(self.snapshot())
                    time.sleep(.2)
                if process.returncode:
                    raise RuntimeError(f"Agent exited {process.returncode}; authenticate in the guest, then retry. See {output}")
                return live_evidence(provider, read_records(log, offset), snapshots, cwd, token, output.read_text())
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                (cwd / "daemon.json").write_text(json.dumps(snapshots, indent=2))
                (cwd / "hooks.json").write_text(json.dumps(read_records(log, offset), indent=2))

    def save(self, live):
        failures = sum(r["status"] == "failed" for r in self.results)
        report = {"schema": 1, "scope": "live" if live else "installation-and-replay",
                  "live_agents": "attempted" if live else "not_run",
                  "platform": platform.platform(), "results": self.results, "failures": failures}
        (self.output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        suite = ET.Element("testsuite", name="sidepulse-macos-vm", tests=str(len(self.results)), failures=str(failures))
        for row in self.results:
            case = ET.SubElement(suite, "testcase", name=row["name"])
            if row["status"] == "failed":
                ET.SubElement(case, "failure", message=str(row["detail"])).text = str(row["detail"])
        ET.ElementTree(suite).write(self.output / "junit.xml", encoding="unicode")
        print(f"Report: {self.output / 'report.json'}")
        return int(bool(failures))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--install-agents", action="store_true")
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if platform.system() != "Darwin" or os.getuid() == 0:
        parser.error("Run as a logged-in macOS VM user")
    model = subprocess.check_output(["sysctl", "-n", "hw.model"], text=True).strip()
    if not model.startswith("VirtualMac"):
        parser.error("Refusing to run against a physical Mac")
    harness = Harness(args.output / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]))
    try:
        harness.check("sidepulse-version", lambda: harness.command("version", [harness.cli, "--version"]))
        if args.install_agents:
            for provider in PROVIDERS:
                harness.check(f"install-{provider}", lambda p=provider: harness.install_agent(p))
            # Codex must exist before setup can calculate hook trust hashes.
            harness.check("refresh-hooks", lambda: harness.command("setup", [harness.cli, "setup"]))
        harness.check("service", lambda: harness.command("service", [harness.cli, "service", "status"]))
        harness.check("menu-bar", lambda: harness.command("menu-bar", ["launchctl", "print", f"gui/{os.getuid()}/io.sidepulse.agentstatus"]))
        harness.check("shared-device", harness.device)
        for provider in PROVIDERS:
            harness.check(f"replay-{provider}", lambda p=provider: harness.replay(p))
        # Legacy main has no state-query socket and may not persist an empty
        # snapshot. Observe it after the replays have created real session state.
        harness.check("daemon", lambda: harness.snapshot()["ok"])
        if args.live:
            for provider in PROVIDERS:
                harness.check(f"live-{provider}", lambda p=provider: harness.live(p))
        else:
            print("Live agent tests NOT RUN. Log into claude and codex inside the guest, then run guest.sh live --disposable-vm.")
    finally:
        result = harness.save(args.live)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
