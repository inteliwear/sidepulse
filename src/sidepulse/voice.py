"""Temporary, local voice display for the eight LEDs on SidePulse Pro."""
from __future__ import annotations

import hashlib
import json
import math
import os
import plistlib
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from .device_writer import DeviceWriteError, write_led_program

VOICE_POLL_SECONDS = 0.05
VOICE_STALE_SECONDS = 0.5
RAINBOW = ((255, 32, 0), (255, 112, 0), (255, 220, 0), (32, 255, 40),
           (0, 224, 176), (0, 152, 255), (64, 48, 255), (192, 32, 255))


@dataclass(frozen=True)
class VoiceSnapshot:
    active: bool = False
    level: float = 0.0
    app: str = ""
    error: str = ""
    tone: float = 0.5


class VoiceRibbon:
    """Syllable-triggered ripples, shaped by vocal timbre, over a breathing rainbow."""
    def __init__(self) -> None:
        self.started: float | None = None
        self.previous_time = 0.0
        self.envelope = 0.0
        self.tone = 0.5
        self.last_onset = -math.inf
        self.pulses: list[tuple[float, float, float, float]] = []

    def colors(self, snapshot: VoiceSnapshot, now: float) -> tuple[tuple[int, int, int], ...]:
        if self.started is None:
            self.started = now
            self.previous_time = now - VOICE_POLL_SECONDS
            self.pulses.append((now, 0.18, 7.0, 0.8))
        dt = max(0.001, min(0.2, now - self.previous_time))
        level = snapshot.level if math.isfinite(snapshot.level) else 0.0
        level = max(0.0, min(1.0, level))
        # Novelty against a short envelope detects attacks rather than mapping level to length.
        if level > 0.12 and level - self.envelope > 0.06 and now - self.last_onset > 0.14:
            self.pulses.append((now, 0.38 + 0.48 * level, 5.5 + 5 * self.tone,
                                1.1 - 0.45 * self.tone))
            self.last_onset = now
        alpha = 1 - math.exp(-dt / (0.075 if level > self.envelope else 0.18))
        self.envelope += (level - self.envelope) * alpha
        tone = snapshot.tone if math.isfinite(snapshot.tone) else 0.5
        self.tone += (max(0, min(1, tone)) - self.tone) * (1 - math.exp(-dt / 0.25))
        self.pulses = [pulse for pulse in self.pulses if now - pulse[0] < 0.95]
        elapsed = now - self.started
        breath = 0.035 + 0.018 * math.sin(elapsed * 2.1)
        colors = []
        for index, rgb in enumerate(RAINBOW):
            distance = abs(index - 3.5)
            wave = sum(amplitude * math.exp(-((distance - (now - born) * speed) / width) ** 2)
                       * math.exp(-(now - born) * 1.1)
                       for born, amplitude, speed, width in self.pulses)
            texture = 0.86 + 0.14 * math.sin(elapsed * (2 + self.tone * 3) + index * 0.9)
            intensity = min(1, breath + 0.26 * self.envelope * texture + wave)
            # Bright crests have a small pearlescent tint, while preserving the rainbow.
            pearl = min(0.1, wave * self.tone * 0.12)
            colors.append(tuple(round(min(255, channel * intensity + (255 - channel) * pearl)) for channel in rgb))
        self.previous_time = now
        return tuple(colors)

    def program(self, snapshot: VoiceSnapshot, brightness: int, now: float) -> str:
        colors = ["#" + "".join(f"{channel:02x}" for channel in rgb) for rgb in self.colors(snapshot, now)]
        return f"brightness {max(0, min(255, int(brightness)))}\n" + " ".join(colors) + " 50ms linear\n"


def program_for_voice(level: float, brightness: int = 255) -> str:
    return VoiceRibbon().program(VoiceSnapshot(True, float(level)), brightness, 0.0)


def build_voice_helper(destination: Path | None = None) -> Path:
    """Use the packaged helper, or compile a cached development helper once."""
    if sys.platform != "darwin":
        raise DeviceWriteError("Voice visualization requires macOS 14.2 or later.")
    resources = files("sidepulse.resources")
    bundled = Path(sys.executable).parent.parent / "Helpers" / "SidePulseVoice.app" / "Contents" / "MacOS" / "SidePulseVoice"
    if destination is None and bundled.is_file():
        return bundled
    packaged = resources.joinpath("SidePulseVoice.app", "Contents", "MacOS", "SidePulseVoice")
    if destination is None and packaged.is_file():
        return Path(str(packaged))
    source = resources.joinpath("voice_meter.swift").read_bytes()
    digest = hashlib.sha256(source).hexdigest()[:16]
    app = destination or (Path.home() / ".cache" / "sidepulse" / "voice" / digest / "SidePulseVoice.app")
    binary = app / "Contents" / "MacOS" / "SidePulseVoice"
    if binary.is_file():
        return binary
    app.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="voice-build-", dir=app.parent) as scratch:
        staged = Path(scratch) / "SidePulseVoice.app"
        executable = staged / "Contents" / "MacOS" / "SidePulseVoice"
        executable.parent.mkdir(parents=True)
        source_path = Path(scratch) / "voice_meter.swift"
        source_path.write_bytes(source)
        try:
            subprocess.run(["xcrun", "swiftc", "-O", "-swift-version", "5",
                            "-target", f"{os.uname().machine}-apple-macos14.2",
                            str(source_path), "-o", str(executable)],
                           check=True, capture_output=True, text=True, timeout=120)
            plist = {
                "CFBundleIdentifier": "io.sidepulse.voice", "CFBundleName": "SidePulse Voice",
                "CFBundleExecutable": "SidePulseVoice", "CFBundlePackageType": "APPL",
                "CFBundleVersion": "1", "LSUIElement": True,
                "LSMinimumSystemVersion": "14.2",
                "NSMicrophoneUsageDescription": "Animate SidePulse Pro with your voice while dictating. Audio is never saved or sent.",
            }
            (staged / "Contents" / "Info.plist").write_bytes(plistlib.dumps(plist))
            subprocess.run(["codesign", "--force", "--sign", "-", str(staged)],
                           check=True, capture_output=True, text=True, timeout=30)
            # Atomic publication keeps readers away from partially compiled helpers.
            try:
                staged.rename(app)
            except FileExistsError:
                if not binary.is_file():
                    raise
        except (OSError, subprocess.SubprocessError) as exc:
            detail = getattr(exc, "stderr", None) or str(exc)
            raise DeviceWriteError(f"Could not build voice helper. Install Xcode Command Line Tools or use the packaged app. {detail}") from exc
    return binary


class VoiceMonitor:
    """Read helper telemetry off the UI thread and fail closed on stale data."""
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot = VoiceSnapshot()
        self._updated = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._process: subprocess.Popen | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="sidepulse-voice", daemon=True)
        self._thread.start()

    def _accept(self, payload: dict) -> None:
        active = payload.get("active") is True
        level = float(payload.get("level", 0))
        tone = float(payload.get("tone", 0.5))
        if not math.isfinite(level) or not math.isfinite(tone):
            raise ValueError("Invalid voice level")
        with self._lock:
            self._snapshot = VoiceSnapshot(active, max(0, min(1, level)),
                                           str(payload.get("app", ""))[:80],
                                           str(payload.get("error", ""))[:500], max(0, min(1, tone)))
            self._updated = time.monotonic()

    def snapshot(self) -> VoiceSnapshot:
        with self._lock:
            if self._stop.is_set() or time.monotonic() - self._updated > VOICE_STALE_SECONDS:
                return VoiceSnapshot(error=self._snapshot.error)
            return self._snapshot

    def _run(self) -> None:
        process = None
        try:
            binary = build_voice_helper()
            if self._stop.is_set():
                return
            process = subprocess.Popen([str(binary)], stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, text=True, bufsize=1)
            self._process = process
            assert process.stdout is not None
            for line in process.stdout:
                if self._stop.is_set():
                    break
                try:
                    self._accept(json.loads(line))
                except (ValueError, TypeError, AttributeError):
                    self._accept({"active": False, "error": "Invalid voice helper response."})
            if not self._stop.is_set():
                self._accept({"active": False, "error": "Voice helper stopped. Toggle Voice Rainbow to retry."})
        except Exception as exc:
            self._accept({"active": False, "error": str(exc)})
        finally:
            if process is not None:
                self._terminate(process)
                if process.stdout is not None:
                    process.stdout.close()
            self._process = None

    @staticmethod
    def _terminate(process: subprocess.Popen) -> None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)

    def stop(self) -> None:
        self._stop.set()
        process = self._process
        if process is not None:
            self._terminate(process)
        if self._thread is not None:
            self._thread.join(timeout=3)
        with self._lock:
            self._snapshot = VoiceSnapshot()
            self._updated = 0.0


class VoiceLedOverlay:
    """Own only Pro's automatic display; return it to its live controller on release."""
    def __init__(self) -> None:
        self.programs: dict[Path, str] = {}
        self.ribbons: dict[Path, VoiceRibbon] = {}

    def sync(self, target: Path, snapshot: VoiceSnapshot, brightness: int) -> bool:
        if not snapshot.active:
            return False
        ribbon = self.ribbons.get(target) or VoiceRibbon()
        program = ribbon.program(snapshot, brightness, time.monotonic())
        if self.programs.get(target) != program:
            write_led_program(program, device_path=target)
            self.programs[target] = program
        self.ribbons[target] = ribbon
        return True

    def release(self, target: Path) -> bool:
        if target not in self.programs:
            return False
        self.programs.pop(target)
        self.ribbons.pop(target, None)
        return True

    def forget_disconnected(self, connected: set[Path]) -> None:
        for target in set(self.programs) - connected:
            self.release(target)


def probe_voice() -> VoiceSnapshot:
    result = subprocess.run([str(build_voice_helper()), "--probe"], capture_output=True,
                            text=True, timeout=5)
    if not result.stdout.strip():
        raise DeviceWriteError(result.stderr.strip() or "Voice helper could not start. macOS 14.2 or later is required.")
    payload = json.loads(result.stdout)
    return VoiceSnapshot(payload.get("active") is True, 0, str(payload.get("app", "")),
                         str(payload.get("error", "")))
