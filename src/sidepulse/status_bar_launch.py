from __future__ import annotations

import os
import plistlib
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .providers import default_state_dir

LAUNCH_AGENT_LABEL = "io.sidepulse.agentstatus"
LAUNCH_AGENT_FILENAME = f"{LAUNCH_AGENT_LABEL}.plist"
LEGACY_LAUNCH_AGENT_LABEL = "com.sidepulse.agentstatus"
LEGACY_LAUNCH_AGENT_FILENAME = f"{LEGACY_LAUNCH_AGENT_LABEL}.plist"
PIXIEPULSE_LEGACY_LAUNCH_AGENT_LABEL = "com.pixiepulse.agentstatus"
PIXIEPULSE_LEGACY_LAUNCH_AGENT_FILENAME = f"{PIXIEPULSE_LEGACY_LAUNCH_AGENT_LABEL}.plist"
STATUS_BAR_DISPLAY_NAME = "SidePulse"
STATUS_BAR_BUNDLE_ID = "io.sidepulse.statusbar"


@dataclass(frozen=True)
class LaunchAgentResult:
    label: str
    plist_path: Path
    changed: bool
    started: bool = False
    stopped: bool = False


def launch_agent_path(home: Path | None = None) -> Path:
    base = home or Path.home()
    return base / "Library" / "LaunchAgents" / LAUNCH_AGENT_FILENAME


def legacy_launch_agent_path(home: Path | None = None) -> Path:
    base = home or Path.home()
    return base / "Library" / "LaunchAgents" / LEGACY_LAUNCH_AGENT_FILENAME


def pixiepulse_legacy_launch_agent_path(home: Path | None = None) -> Path:
    base = home or Path.home()
    return base / "Library" / "LaunchAgents" / PIXIEPULSE_LEGACY_LAUNCH_AGENT_FILENAME


def status_bar_launcher_path(home: Path | None = None) -> Path:
    return (
        default_user_data_dir(home)
        / "sidepulse"
        / "status-bar"
        / f"{STATUS_BAR_DISPLAY_NAME}.app"
        / "Contents"
        / "MacOS"
        / STATUS_BAR_DISPLAY_NAME
    )


def launch_agent_installed(plist_path: Path | None = None) -> bool:
    target = plist_path or launch_agent_path()
    return target.exists()


def build_launch_agent_plist(
    python_executable: Path | str | None = None,
    launcher_path: Path | None = None,
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
) -> dict[str, Any]:
    executable = str(python_executable or sys.executable or "python3")
    launcher = str(launcher_path or status_bar_launcher_path())
    state_dir = default_state_dir()
    stdout = stdout_path or state_dir / "status-bar.out.log"
    stderr = stderr_path or state_dir / "status-bar.err.log"

    plist: dict[str, Any] = {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": [launcher],
        "RunAtLoad": True,
        "StandardOutPath": str(stdout),
        "StandardErrorPath": str(stderr),
        "WorkingDirectory": str(Path.home()),
        "EnvironmentVariables": {
            "PYTHONUNBUFFERED": "1",
            "PATH": launch_agent_path_env(executable),
        },
    }
    return plist


def install_launch_agent(
    *,
    start: bool = True,
    plist_path: Path | None = None,
    python_executable: Path | str | None = None,
    legacy_plist_path: Path | None = None,
    pixiepulse_legacy_plist_path: Path | None = None,
    launcher_path: Path | None = None,
) -> LaunchAgentResult:
    target = plist_path or launch_agent_path()
    legacy_target = legacy_plist_path if legacy_plist_path is not None else (
        legacy_launch_agent_path() if plist_path is None else None
    )
    pixiepulse_legacy_target = (
        pixiepulse_legacy_plist_path
        if pixiepulse_legacy_plist_path is not None
        else (pixiepulse_legacy_launch_agent_path() if plist_path is None else None)
    )
    launcher = launcher_path or status_bar_launcher_path()
    launcher_changed = install_status_bar_launcher(
        launcher,
        python_executable=python_executable,
    )
    plist = build_launch_agent_plist(
        python_executable=python_executable,
        launcher_path=launcher,
    )
    data = plistlib.dumps(plist, sort_keys=False)
    existing = target.read_bytes() if target.exists() else None
    changed = existing != data

    target.parent.mkdir(parents=True, exist_ok=True)
    default_state_dir().mkdir(parents=True, exist_ok=True)
    if changed:
        target.write_bytes(data)
    legacy_removed = False
    if legacy_target is not None:
        legacy_removed = remove_legacy_launch_agent(legacy_target)
    pixiepulse_legacy_removed = False
    if pixiepulse_legacy_target is not None:
        pixiepulse_legacy_removed = remove_legacy_launch_agent(pixiepulse_legacy_target)
    changed = changed or legacy_removed or pixiepulse_legacy_removed or launcher_changed

    started = False
    if start:
        restart_launch_agent(target)
        started = True

    return LaunchAgentResult(
        label=LAUNCH_AGENT_LABEL,
        plist_path=target,
        changed=changed,
        started=started,
    )


def uninstall_launch_agent(plist_path: Path | None = None) -> LaunchAgentResult:
    target = plist_path or launch_agent_path()
    bootout_launch_agent(target)
    changed = target.exists()
    if target.exists():
        target.unlink()
    return LaunchAgentResult(
        label=LAUNCH_AGENT_LABEL,
        plist_path=target,
        changed=changed,
        stopped=True,
    )


def install_status_bar_launcher(
    launcher_path: Path | None = None,
    *,
    python_executable: Path | str | None = None,
) -> bool:
    target = launcher_path or status_bar_launcher_path()
    script = build_status_bar_launcher_script(python_executable=python_executable)
    data = script.encode()
    existing = target.read_bytes() if target.exists() else None
    changed = existing != data
    target.parent.mkdir(parents=True, exist_ok=True)
    if changed:
        target.write_bytes(data)
    target.chmod(0o755)
    info_path = target.parent.parent / "Info.plist"
    info_data = plistlib.dumps(build_status_bar_bundle_info(), sort_keys=False)
    info_existing = info_path.read_bytes() if info_path.exists() else None
    if info_existing != info_data:
        info_path.write_bytes(info_data)
        changed = True
    return changed


def build_status_bar_bundle_info() -> dict[str, Any]:
    return {
        "CFBundleDevelopmentRegion": "en",
        "CFBundleDisplayName": STATUS_BAR_DISPLAY_NAME,
        "CFBundleExecutable": STATUS_BAR_DISPLAY_NAME,
        "CFBundleIdentifier": STATUS_BAR_BUNDLE_ID,
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": STATUS_BAR_DISPLAY_NAME,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "1",
        "LSMinimumSystemVersion": "13.0",
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
    }


def build_status_bar_launcher_script(
    python_executable: Path | str | None = None,
) -> str:
    executable = str(python_executable or sys.executable or "python3")
    if getattr(sys, "frozen", False) and python_executable is None:
        command = [executable, "status-bar", "start", "--foreground"]
    else:
        command = [executable, "-m", "sidepulse", "status-bar", "--foreground"]
    quoted = " ".join(shlex.quote(part) for part in command)
    return "\n".join(
        [
            "#!/bin/sh",
            "export PYTHONUNBUFFERED=1",
            f"exec {quoted}",
            "",
        ]
    )


def restart_launch_agent(plist_path: Path) -> None:
    bootout_launch_agent(plist_path)
    subprocess.run(
        ["launchctl", "bootstrap", launch_domain(), str(plist_path)],
        check=True,
    )
    subprocess.run(
        ["launchctl", "kickstart", "-k", f"{launch_domain()}/{LAUNCH_AGENT_LABEL}"],
        check=False,
    )


def bootout_launch_agent(plist_path: Path) -> None:
    subprocess.run(
        ["launchctl", "bootout", launch_domain(), str(plist_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def remove_legacy_launch_agent(plist_path: Path | None = None) -> bool:
    target = plist_path or legacy_launch_agent_path()
    if not target.exists():
        return False
    bootout_launch_agent(target)
    target.unlink()
    return True


def launch_domain() -> str:
    return f"gui/{os.getuid()}"


def default_user_data_dir(home: Path | None = None) -> Path:
    if home is None:
        xdg_data_home = os.environ.get("XDG_DATA_HOME")
        if xdg_data_home:
            return Path(xdg_data_home).expanduser()
    base = home or Path.home()
    return base / ".local" / "share"


def launch_agent_path_env(python_executable: str) -> str:
    candidates = [
        Path.home() / ".local" / "bin",
        executable_parent(python_executable),
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
        Path("/usr/bin"),
        Path("/bin"),
        Path("/usr/sbin"),
        Path("/sbin"),
        Path("/opt/anaconda3/bin"),
    ]
    seen: set[str] = set()
    result: list[str] = []
    for candidate in candidates:
        if candidate is None:
            continue
        text = str(candidate)
        if text in seen:
            continue
        seen.add(text)
        result.append(text)
    return ":".join(result)


def executable_parent(python_executable: str) -> Path | None:
    path = Path(python_executable)
    if not path.is_absolute():
        return None
    return path.parent
