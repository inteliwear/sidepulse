"""Launch the Antigravity sidebar helper under its own macOS permission identity."""

from __future__ import annotations

import hashlib
import json
import plistlib
import shutil
import subprocess
import tempfile
from pathlib import Path

NAME = "SidePulse Chat Opener"
BUNDLE_ID = "io.sidepulse.chat-opener"
RESOURCES = Path(__file__).with_name("resources")
SCRIPT = RESOURCES / "open_antigravity.js"
ICON = RESOURCES / "SidePulse.icns"


def install_opener(parent: Path | None = None) -> Path:
    import fcntl  # macOS only

    parent = parent or Path.home() / "Library/Application Support/SidePulse"
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    app = parent / f"{NAME}.app"
    digest = hashlib.sha256(SCRIPT.read_bytes() + ICON.read_bytes()).hexdigest()
    with (parent / ".chat-opener.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            with (app / "Contents/Info.plist").open("rb") as stream:
                if plistlib.load(stream).get("SidePulseResourceHash") == digest:
                    return app
        except (FileNotFoundError, plistlib.InvalidFileException):
            pass
        with tempfile.TemporaryDirectory(dir=parent) as directory:
            stage = Path(directory) / app.name
            subprocess.run(["/usr/bin/osacompile", "-l", "JavaScript", "-o", str(stage), str(SCRIPT)],
                           capture_output=True, check=True, timeout=20)
            plist = stage / "Contents/Info.plist"
            info = plistlib.loads(plist.read_bytes())
            # The generated applet also names its generic icon in Assets.car.
            info.pop("CFBundleIconName", None)
            shutil.copyfile(ICON, stage / "Contents/Resources" / ICON.name)
            info.update(CFBundleIconFile=ICON.name, CFBundleName=NAME, CFBundleDisplayName=NAME, CFBundleIdentifier=BUNDLE_ID,
                        LSUIElement=True, SidePulseResourceHash=digest)
            plist.write_bytes(plistlib.dumps(info))
            subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(stage)],
                           capture_output=True, check=True, timeout=20)
            previous = Path(directory) / "previous.app"
            if app.exists():
                app.rename(previous)
            try:
                stage.rename(app)
            except OSError:
                if previous.exists():
                    previous.rename(app)
                raise
    return app


def run_antigravity_helper(action: str, *args: str) -> str:
    app = install_opener()
    # LaunchServices gives the helper its own Accessibility identity. Running
    # its script as a child of the menu process would attribute access to the caller.
    with tempfile.TemporaryDirectory(prefix="sidepulse-chat-") as directory:
        output = Path(directory) / "result"
        subprocess.run(["/usr/bin/open", "-n", "-g", "-W", "-a", str(app),
                        "--stdout", str(output), "--args", action, *args],
                       capture_output=True, check=True, timeout=20)
        try:
            result = json.loads(output.read_text())
        except (FileNotFoundError, ValueError) as exc:
            raise OSError(f"{NAME} did not return a result") from exc
        if not isinstance(result, str):
            raise OSError(f"{NAME} returned an invalid result")
        return result
