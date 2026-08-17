from __future__ import annotations

import plistlib
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .providers import default_state_dir
from .status_bar_launch import default_user_data_dir, launch_agent_path_env, launch_domain


LAUNCH_AGENT_LABEL = "io.sidepulse.remotehosts"
LAUNCH_AGENT_FILENAME = f"{LAUNCH_AGENT_LABEL}.plist"


@dataclass(frozen=True)
class RemoteLaunchResult:
    plist_path: Path
    changed: bool
    started: bool = False
    stopped: bool = False


def remote_launch_agent_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / "Library" / "LaunchAgents" / LAUNCH_AGENT_FILENAME


def remote_launcher_path(home: Path | None = None) -> Path:
    return default_user_data_dir(home) / "sidepulse" / "remote-hosts" / "SidePulse Remote Hosts"


def build_remote_launcher_script(python_executable: Path | str | None = None) -> str:
    executable = str(python_executable or sys.executable or "python3")
    command = [executable, "-m", "sidepulse", "remote", "monitor"]
    return "\n".join(
        [
            "#!/bin/sh",
            "export PYTHONUNBUFFERED=1",
            f"exec {' '.join(shlex.quote(part) for part in command)}",
            "",
        ]
    )


def build_remote_launch_agent_plist(
    *,
    python_executable: Path | str | None = None,
    launcher_path: Path | None = None,
) -> dict[str, Any]:
    executable = str(python_executable or sys.executable or "python3")
    launcher = launcher_path or remote_launcher_path()
    state_dir = default_state_dir()
    return {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": [str(launcher)],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "StandardOutPath": str(state_dir / "remote-hosts.out.log"),
        "StandardErrorPath": str(state_dir / "remote-hosts.err.log"),
        "WorkingDirectory": str(Path.home()),
        "EnvironmentVariables": {
            "PYTHONUNBUFFERED": "1",
            "PATH": launch_agent_path_env(executable),
        },
    }


def install_remote_launch_agent(
    *,
    start: bool = True,
    plist_path: Path | None = None,
    launcher_path: Path | None = None,
    python_executable: Path | str | None = None,
) -> RemoteLaunchResult:
    target = plist_path or remote_launch_agent_path()
    launcher = launcher_path or remote_launcher_path()
    script = build_remote_launcher_script(python_executable)
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher_changed = not launcher.exists() or launcher.read_text() != script
    if launcher_changed:
        launcher.write_text(script)
    launcher.chmod(0o755)

    data = plistlib.dumps(
        build_remote_launch_agent_plist(
            python_executable=python_executable,
            launcher_path=launcher,
        ),
        sort_keys=False,
    )
    changed = not target.exists() or target.read_bytes() != data
    target.parent.mkdir(parents=True, exist_ok=True)
    default_state_dir().mkdir(parents=True, exist_ok=True)
    if changed:
        target.write_bytes(data)

    if start:
        _bootout(target)
        subprocess.run(["launchctl", "bootstrap", launch_domain(), str(target)], check=True)
        subprocess.run(
            ["launchctl", "kickstart", "-k", f"{launch_domain()}/{LAUNCH_AGENT_LABEL}"],
            check=False,
        )
    return RemoteLaunchResult(target, changed or launcher_changed, started=start)


def uninstall_remote_launch_agent(plist_path: Path | None = None) -> RemoteLaunchResult:
    target = plist_path or remote_launch_agent_path()
    _bootout(target)
    changed = target.exists()
    if target.exists():
        target.unlink()
    return RemoteLaunchResult(target, changed, stopped=True)


def _bootout(path: Path) -> None:
    subprocess.run(
        ["launchctl", "bootout", launch_domain(), str(path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
