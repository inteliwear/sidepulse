import importlib.util
import json
import os
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/macos-vm/visual.py"
spec = importlib.util.spec_from_file_location("vm_visual", SCRIPT)
visual = importlib.util.module_from_spec(spec)
spec.loader.exec_module(visual)


def recording():
    return {"run_id": "one", "frames": [
        {"id": i, "screenshot": f"screens/{i}.png", "detected": [state]}
        for i, state in enumerate(("working", "waiting_for_input", "completed"))]}


def test_capture_without_independent_labels_is_incomplete():
    result = visual.comparison(recording(), {"run_id": "one", "labels": {}})
    assert result["status"] == "incomplete"
    assert result["missing_required_states"] == ["completed", "waiting_for_input", "working"]


def test_visual_mismatch_and_missing_screen_fail():
    data = recording()
    result = visual.comparison(data, {"run_id": "one", "labels": {"1": "completed"}})
    assert result["status"] == "failed"
    data["frames"][1].pop("screenshot")
    assert visual.comparison(data, {"run_id": "one", "labels": {"1": "waiting_for_input"}})["status"] == "failed"


def test_complete_review_requires_real_wait_coverage():
    labels = {"0": "working", "2": "completed"}
    assert visual.comparison(recording(), {"run_id": "one", "labels": labels})["status"] == "incomplete"
    labels["1"] = "waiting_for_input"
    assert visual.comparison(recording(), {"run_id": "one", "labels": labels})["status"] == "passed"


def test_old_run_and_unknown_frames_cannot_satisfy_review():
    with pytest.raises(ValueError, match="another recording"):
        visual.comparison(recording(), {"run_id": "old", "labels": {}})
    with pytest.raises(ValueError, match="unknown frames"):
        visual.comparison(recording(), {"run_id": "one", "labels": {"99": "completed"}})


def test_only_current_provider_and_workspace_sessions_are_compared(tmp_path):
    hooks = [{"event": {"cwd": str(tmp_path), "session_id": "ours"}},
             {"cwd": "/elsewhere", "session_id": "old"}]
    sessions = visual.session_ids(hooks, tmp_path)
    assert sessions == {"ours"}
    frame = {"before": {"snapshot": {"statuses": [
        {"provider": "claude", "session_id": "ours", "mode": "working"},
        {"provider": "claude", "session_id": "old", "mode": "completed"},
        {"provider": "codex", "session_id": "ours", "mode": "completed"}]}}}
    assert visual.detected_modes(frame, "claude", sessions) == ["working"]


def test_report_escapes_untrusted_agent_output(tmp_path):
    target = tmp_path / "review.html"
    visual.render_report({"hooks": [{"output": "</script><script>alert(1)</script>"}]}, target)
    source = target.read_text()
    assert "</script><script>alert(1)" not in source
    assert "\\u003c/script>" in source


def test_recording_error_cannot_pass_review():
    data = recording()
    data["recording_error"] = "screenshot unavailable"
    labels = {str(f["id"]): f["detected"][0] for f in data["frames"]}
    assert visual.comparison(data, {"run_id": "one", "labels": labels})["status"] == "incomplete"


@pytest.mark.skipif(os.name != 'posix', reason='Real relay uses POSIX pseudo-terminals')
def test_interactive_relay_preserves_real_terminal_output(tmp_path):
    import os
    import pty
    import select
    import subprocess
    import sys
    import time
    helper = tmp_path / "relay_probe.py"
    helper.write_text('''import runpy, sys, threading, time
from pathlib import Path
module = runpy.run_path(sys.argv[1])
class Observer:
    def __init__(self):
        self.start = time.monotonic()
        self.stop = threading.Event()
    def loop(self):
        self.stop.wait()
output = Path(sys.argv[2])
code, error = module['interactive'](
    [sys.executable, '-c', 'print("\\\\x1b[32mREAL_PTY_OUTPUT\\\\x1b[0m", flush=True)'],
    output, output, Observer(), 5)
print('RELAY_RESULT', code, error, flush=True)
''')
    master, slave = pty.openpty()
    process = subprocess.Popen([sys.executable, str(helper), str(SCRIPT), str(tmp_path)],
                               stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
    os.close(slave)
    output = bytearray()
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            readable, _, _ = select.select([master], [], [], .1)
            if readable:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                output.extend(chunk)
            elif process.poll() is not None:
                break
        assert process.wait(timeout=2) == 0, output.decode(errors="replace")
        assert b"RELAY_RESULT 0 None" in output
        assert b"REAL_PTY_OUTPUT" in (tmp_path / "terminal.raw").read_bytes()
        assert "REAL_PTY_OUTPUT" in (tmp_path / "terminal.jsonl").read_text()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)
