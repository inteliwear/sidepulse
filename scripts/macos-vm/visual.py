#!/usr/bin/env python3
"""Record a real interactive agent and review its screen against SidePulse."""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import platform
import select
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid

MODES = ("idle_ready", "working", "tool_running", "waiting_for_input", "blocked_error", "completed")


def append(path, row):
    with path.open("a") as stream:
        stream.write(json.dumps(row) + "\n")


def records(path):
    if not path.exists():
        return []
    result = []
    for line in path.read_text().splitlines():
        try:
            result.append(json.loads(line))
        except ValueError:
            continue
    return result


def session_ids(hooks, cwd):
    payloads = [r.get("event", r) for r in hooks]
    return {r["session_id"] for r in payloads if isinstance(r, dict)
            and r.get("session_id") and r.get("cwd") == str(cwd)}


def detected_modes(frame, provider, sessions):
    modes = set()
    for name in ("before", "after"):
        snapshot = frame.get(name, {}).get("snapshot", {})
        for row in snapshot.get("statuses", []) + snapshot.get("stale_statuses", []):
            if row.get("provider") == provider and row.get("session_id") in sessions:
                modes.add(row["mode"])
    return sorted(modes)


def comparison(data, review):
    if review.get("run_id") != data["run_id"]:
        raise ValueError("Review belongs to another recording")
    labels = review.get("labels", {})
    if not isinstance(labels, dict):
        raise ValueError("Review labels must be an object")
    frames = {str(f["id"]): f for f in data["frames"]}
    if set(labels) - set(frames):
        raise ValueError("Review refers to unknown frames")
    rows = []
    for key, expected in labels.items():
        if expected not in MODES:
            raise ValueError(f"Unknown visible state: {expected}")
        frame = frames[key]
        observed = frame["detected"]
        rows.append({"frame": key, "expected_from_screen": expected, "detected": observed,
                     "matches": bool(frame.get("screenshot")) and expected in observed,
                     "capture_error": frame.get("capture_error")})
    covered = sorted({r["expected_from_screen"] for r in rows})
    missing = sorted({"working", "waiting_for_input", "completed"} - set(covered))
    # Capture alone never passes visual verification. Require independent labels
    # and coverage of a real permission/question wait, not only a happy path.
    status = ("failed" if any(not r["matches"] for r in rows) else
              "incomplete" if missing or not rows or data.get("recording_error") else "passed")
    return {"run_id": data["run_id"], "status": status, "covered": covered,
            "missing_required_states": missing, "comparisons": rows,
            "unreviewed_frames": len(frames) - len(labels),
            "recording_error": data.get("recording_error"),
            "physical_leds": "not visually verified; LEDS.LED bytes are recorded"}


def render_report(data, target):
    template = Path(__file__).with_name("visual-report.html").read_text()
    encoded = json.dumps(data).replace("<", "\\u003c").replace("&", "\\u0026")
    target.write_text(template.replace("/* RECORDING_DATA */", encoded))


class Capture:
    def __init__(self, output, start):
        self.output, self.start = output, start
        self.stop = threading.Event()
        self.frames = []
        self.error = None
        (output / "screens").mkdir()

    def snapshot(self):
        try:
            from check import installed_snapshot
            return installed_snapshot(timeout=.5)
        except Exception as exc:
            return {"error": str(exc)}

    def frame(self):
        index = len(self.frames)
        image = self.output / "screens" / f"{index:05}.jpg"
        frame = {"id": index, "t": round(time.monotonic() - self.start, 3),
                 "wall_time": time.time(), "before": self.snapshot()}
        try:
            # This is intentionally the actual guest display, not a rendering of logs.
            subprocess.run(["/usr/sbin/screencapture", "-x", "-m", "-t", "jpg", str(image)],
                           check=True, capture_output=True, timeout=5)
            if not image.exists() or image.stat().st_size == 0:
                raise RuntimeError("No screenshot; enable Screen Recording for the guest Terminal")
            frame["screenshot"] = str(image.relative_to(self.output))
            frame["screenshot_sha256"] = hashlib.sha256(image.read_bytes()).hexdigest()
        except Exception as exc:
            frame["capture_error"] = str(exc)
        frame["after"] = self.snapshot()
        frame["end_t"] = round(time.monotonic() - self.start, 3)
        try:
            led = Path("/Volumes/SidePulse/LEDS.LED").read_bytes()
            frame["led"] = {"text": led.decode(errors="replace"), "sha256": hashlib.sha256(led).hexdigest()}
        except OSError as exc:
            frame["led"] = {"error": str(exc)}
        self.frames.append(frame)
        append(self.output / "frames.jsonl", frame)

    def loop(self):
        try:
            while not self.stop.is_set():
                self.frame()
                self.stop.wait(1)
        except Exception as exc:
            self.error = str(exc)


def interactive(argv, cwd, output, capture, seconds):
    """Transparent PTY: retain the real TUI and default permission prompts."""
    import fcntl
    import pty
    import termios
    import tty

    original = termios.tcgetattr(sys.stdin.fileno())
    child, master = pty.fork()
    if child == 0:
        os.chdir(cwd)
        os.execvp(argv[0], argv)
    def resize(*_):
        size = fcntl.ioctl(sys.stdin.fileno(), termios.TIOCGWINSZ, b"\0" * 8)
        fcntl.ioctl(master, termios.TIOCSWINSZ, size)
    old_handler = signal.signal(signal.SIGWINCH, resize)
    resize()
    worker = threading.Thread(target=capture.loop, daemon=True)
    worker.start()
    status = None
    error = None
    deadline = time.monotonic() + seconds
    try:
        tty.setraw(sys.stdin.fileno())
        with (output / "terminal.raw").open("wb") as raw:
            while True:
                if time.monotonic() > deadline:
                    error = "Interactive run reached its time limit"
                    break
                ready, _, _ = select.select([master, sys.stdin.fileno()], [], [], .2)
                if master in ready:
                    try:
                        data = os.read(master, 65536)
                    except OSError as exc:
                        if exc.errno == errno.EIO:
                            break
                        raise
                    if not data:
                        break
                    raw.write(data)
                    raw.flush()
                    append(output / "terminal.jsonl", {"t": time.monotonic() - capture.start,
                                                       "output": data.decode(errors="replace")})
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()
                if sys.stdin.fileno() in ready:
                    data = os.read(sys.stdin.fileno(), 4096)
                    if not data:
                        break
                    os.write(master, data)  # Input is not separately recorded.
                if status is None:
                    pid, child_status = os.waitpid(child, os.WNOHANG)
                    if pid:
                        status = child_status  # Drain remaining PTY output to EOF.
    finally:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, original)
        signal.signal(signal.SIGWINCH, old_handler)
        if status is None:
            pid, child_status = os.waitpid(child, os.WNOHANG)
            if not pid:
                os.killpg(child, signal.SIGTERM)
                for _ in range(20):
                    pid, child_status = os.waitpid(child, os.WNOHANG)
                    if pid:
                        break
                    time.sleep(.1)
                if not pid:
                    os.killpg(child, signal.SIGKILL)
                    _, child_status = os.waitpid(child, 0)
            status = child_status
        os.close(master)
        capture.stop.set()
        worker.join(timeout=8)
    return os.waitstatus_to_exitcode(status), error


def record(args):
    if platform.system() != "Darwin" or os.getuid() == 0 or not sys.stdin.isatty():
        raise RuntimeError("Run in Terminal as the logged-in macOS VM user")
    model = subprocess.check_output(["sysctl", "-n", "hw.model"], text=True).strip()
    if not model.startswith("VirtualMac"):
        raise RuntimeError("Refusing to record the physical host")
    if not shutil.which(args.provider):
        raise RuntimeError(f"Install and authenticate {args.provider} first")
    auth = [args.provider, "auth", "status"] if args.provider == "claude" else [args.provider, "login", "status"]
    if subprocess.run(auth, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30).returncode:
        raise RuntimeError(f"Authenticate {args.provider} in the guest before recording")
    run_id = uuid.uuid4().hex
    output = args.output.expanduser() / (time.strftime("%Y%m%d-%H%M%S") + "-visual-" + args.provider + "-" + run_id[:6])
    output.mkdir(parents=True)
    output.chmod(0o700)
    workspace = output / "workspace"
    workspace.mkdir()
    (workspace / "fixture.txt").write_text("SIDEPULSE_" + uuid.uuid4().hex + "\n")
    prompt = ("Use your shell tool to run `sleep 5; cat fixture.txt`. "
              "Then report the exact file contents and say that the task is complete. Do not modify files.")
    argv = [args.provider, prompt]
    log = Path.home() / f".local/state/sidepulse/agent-monitor/{args.provider}.jsonl"
    offset = log.stat().st_size if log.exists() else 0
    print(f"Recording: {output}\nLeave the agent visible. Pause at approval/question screens for 3 seconds.\n"
          "After completion, leave it visible for 3 seconds; then exit the agent normally.\n"
          "Additional prompts and interrupts are allowed. Recording lasts at most 10 minutes.", flush=True)
    capture = Capture(output, time.monotonic())
    capture.frame()
    if not capture.frames[0].get("screenshot"):
        raise RuntimeError("Screen capture failed. Grant Screen Recording to Terminal inside the VM, then retry.")
    error = None
    code = None
    try:
        code, error = interactive(argv, workspace, output, capture, args.seconds)
    except Exception as exc:
        error = str(exc)
    finally:
        from check import read_records
        hooks = read_records(log, offset)
        sessions = session_ids(hooks, workspace)
        for frame in capture.frames:
            frame["detected"] = detected_modes(frame, args.provider, sessions)
        if code not in (None, 0):
            error = error or f"Agent exited {code}"
        if capture.error:
            error = error or capture.error
        if any(f.get("capture_error") for f in capture.frames):
            error = error or "One or more screenshots failed"
        data = {"run_id": run_id, "provider": args.provider, "workspace": str(workspace),
                "sessions": sorted(sessions), "frames": capture.frames, "hooks": hooks,
                "recording_error": error, "agent_exit_code": code, "visual_verification": "pending_review"}
        (output / "recording.json").write_text(json.dumps(data, indent=2))
        render_report(data, output / "review.html")
        print(f"\nVisual comparison: {output / 'review.html'}\nScreens require independent review; capture is not a pass.")
    return 1 if error else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    rec = sub.add_parser("record")
    rec.add_argument("provider", choices=("claude", "codex"))
    rec.add_argument("--output", type=Path, default=Path.home() / "sidepulse-vm-results")
    rec.add_argument("--seconds", type=int, default=600)
    review = sub.add_parser("review")
    review.add_argument("run", type=Path)
    review.add_argument("labels", type=Path)
    args = parser.parse_args()
    if args.command == "record":
        if not 10 <= args.seconds <= 600:
            parser.error("Recording duration must be 10–600 seconds")
        return record(args)
    data = json.loads((args.run / "recording.json").read_text())
    for frame in data["frames"]:
        name = frame.get("screenshot")
        if not name:
            continue
        image = (args.run / name).resolve()
        if args.run.resolve() not in image.parents or not image.is_file() or hashlib.sha256(image.read_bytes()).hexdigest() != frame.get("screenshot_sha256"):
            frame.pop("screenshot", None)
            frame["capture_error"] = "Screenshot missing or changed since recording"
            data["recording_error"] = frame["capture_error"]
    review_labels = json.loads(args.labels.read_text())
    result = comparison(data, review_labels)
    (args.run / "visual-result.json").write_text(json.dumps(result, indent=2))
    data["review_labels"] = review_labels["labels"]
    data["visual_verification"] = result["status"]
    render_report(data, args.run / "review.html")
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
