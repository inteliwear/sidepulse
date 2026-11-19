"""CPU budget for a real macOS menu-bar item with its menu closed."""

from contextlib import redirect_stdout
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch

import pytest


def test_status_bar_cpu_budget(tmp_path):
    if sys.platform != "darwin":
        pytest.skip("menu-bar CPU check requires macOS")

    # Other UI tests can leave timers queued on the main run loop. Start a
    # separate process so only this status item contributes to the measurement.
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path / ".config"),
        "XDG_DATA_HOME": str(tmp_path / ".local/share"),
        "XDG_STATE_HOME": str(tmp_path / ".local/state"),
    }
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve())],
        env=env, capture_output=True, text=True, timeout=45,
    )
    detail = f"{result.stdout}\n{result.stderr}".strip()
    if result.returncode == 77 and env.get("SIDEPULSE_REQUIRE_UI_TESTS") != "1":
        pytest.skip(detail)
    assert result.returncode == 0, detail
    samples = json.loads(result.stdout)
    # Percent of one logical core, as shown by Activity Monitor for a process.
    # Leave headroom for AppKit and CI variation while catching sustained ~35%.
    limits = {"idle": 5.0, "working": 15.0, "waiting for input": 15.0}
    for state, limit in limits.items():
        cpu = samples[state]["percent_of_core"]
        print(f"Menu-bar CPU, {state}: {cpu:.2f}% (limit {limit:g}%)")
        assert cpu < limit, f"{state}: {cpu:.2f}% of one core exceeds the {limit:g}% budget; {samples}"


def measure_status_bar_cpu():
    from AppKit import (
        NSApplication, NSApplicationActivationPolicyAccessory,
        NSSquareStatusItemLength, NSStatusBar,
    )
    from Foundation import NSDate, NSRunLoop, NSTimer

    from sidepulse import status_bar

    def layer_is_animating(layer):
        return layer is not None and not layer.isHidden() and (
            bool(layer.animationKeys())
            or any(layer_is_animating(child) for child in layer.sublayers() or ())
        )

    def view_is_animating(view):
        return not view.isHidden() and (
            layer_is_animating(view.layer())
            or any(view_is_animating(child) for child in view.subviews())
        )

    app = NSApplication.sharedApplication()
    if app is None:
        print("CPU check requires an AppKit session", file=sys.stderr)
        return 77
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    app.finishLaunching()
    bar = NSStatusBar.systemStatusBar()
    item = bar.statusItemWithLength_(NSSquareStatusItemLength)
    if item is None:
        print("AppKit could not create a status item", file=sys.stderr)
        return 77
    controller = status_bar.StatusBarController.alloc().init()
    controller.status_item = item
    item.setVisible_(True)

    run_loop = NSRunLoop.mainRunLoop()
    # Keep the run loop alive in the idle case without introducing periodic work.
    keepalive = NSTimer.scheduledTimerWithTimeInterval_repeats_block_(60, False, lambda _: None)
    samples = {}
    try:
        for name, state in (
            ("idle", status_bar.STATE_IDLE),
            ("working", status_bar.STATE_WORKING),
            ("waiting for input", status_bar.STATE_ASK),
        ):
            # Exercise animated icons regardless of the runner's accessibility
            # preference. AppKit still creates and animates the real status item.
            with patch.object(status_bar, "NSWorkspace") as workspace, redirect_stdout(sys.stderr):
                workspace.sharedWorkspace.return_value.accessibilityDisplayShouldReduceMotion.return_value = False
                controller.set_status(state)
            # Exclude the initial transition and measure sustained animation.
            run_loop.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(2.0))
            button = item.button()
            assert item.isVisible() and button is not None, f"{name}: status item not visible"
            if name == "idle":
                assert button.image() is not None, "idle icon missing"
            elif not view_is_animating(button):
                # The image-based renderer changes the button image, whereas
                # the native renderer animates a layer with a constant image.
                first_frame = button.image()
                run_loop.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.23))
                assert first_frame is not None and button.image() != first_frame, (
                    f"{name}: icon did not animate"
                )
            wall_start, cpu_start = time.perf_counter(), time.process_time()
            run_loop.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(3.0))
            cpu_seconds = time.process_time() - cpu_start
            elapsed = time.perf_counter() - wall_start
            assert elapsed >= 2.9, f"{name}: AppKit run loop stopped early ({elapsed:.2f}s)"
            samples[name] = {
                "percent_of_core": 100 * cpu_seconds / elapsed,
                "cpu_seconds": cpu_seconds,
                "wall_seconds": elapsed,
            }
    finally:
        keepalive.invalidate()
        controller.applicationWillTerminate_(None)
        bar.removeStatusItem_(item)
    print(json.dumps(samples))
    return 0


if __name__ == "__main__":
    raise SystemExit(measure_status_bar_cpu())
