from __future__ import annotations

import os
import plistlib
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .providers import default_state_dir


SERVICE_LABEL = "io.sidepulse.service"
SERVICE_DISPLAY_NAME = "SidePulse Background Service"


@dataclass(frozen=True)
class ServiceInstallResult:
    path: Path
    changed: bool
    started: bool
    detail: str = ""


def service_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "service", "run"]
    return [sys.executable, "-m", "sidepulse", "service", "run"]


def service_launcher_path() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME")
    base = Path(data_home).expanduser() if data_home else Path.home() / ".local" / "share"
    return base / "sidepulse" / "service" / SERVICE_DISPLAY_NAME


def build_service_launcher_script() -> str:
    command = " ".join(shlex.quote(part) for part in service_command())
    return f"#!/bin/sh\nexport PYTHONUNBUFFERED=1\nexec {command}\n"


def install_service(*, start: bool = True, dry_run: bool = False) -> ServiceInstallResult:
    if sys.platform == 'win32':
        from .windows_service import install
        return install(start=start, dry_run=dry_run)
    if sys.platform == "darwin":
        return install_launch_agent(start=start, dry_run=dry_run)
    if sys.platform.startswith("linux"):
        return install_systemd_user_service(start=start, dry_run=dry_run)
    return ServiceInstallResult(Path(""), False, False, "unsupported platform")


def install_launch_agent(*, start: bool, dry_run: bool) -> ServiceInstallResult:
    path = Path.home() / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"
    state = default_state_dir()
    launcher = service_launcher_path()
    launcher_data = build_service_launcher_script().encode()
    payload = {
        "Label": SERVICE_LABEL,
        # macOS names unbundled background items after the registered program.
        # Register our launcher rather than the shared Python interpreter.
        "ProgramArguments": [str(launcher)],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(state / "service.out.log"),
        "StandardErrorPath": str(state / "service.err.log"),
        "WorkingDirectory": str(Path.home()),
        "EnvironmentVariables": {
            "PATH": os.environ.get(
                "PATH",
                "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            ),
        },
    }
    data = plistlib.dumps(payload, sort_keys=False)
    existing = path.read_bytes() if path.exists() else None
    existing_launcher = launcher.read_bytes() if launcher.exists() else None
    launcher_changed = existing_launcher != launcher_data or not os.access(launcher, os.X_OK)
    changed = existing != data or launcher_changed
    if dry_run:
        return ServiceInstallResult(path, changed, False, "dry run")
    path.parent.mkdir(parents=True, exist_ok=True)
    state.mkdir(parents=True, exist_ok=True)
    launcher.parent.mkdir(parents=True, exist_ok=True)
    if launcher_changed:
        launcher.write_bytes(launcher_data)
        launcher.chmod(0o755)
    if existing != data:
        path.write_bytes(data)
    started = False
    if start:
        domain = f"gui/{subprocess.check_output(['id', '-u'], text=True).strip()}"
        subprocess.run(
            ["launchctl", "bootout", domain, str(path)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        result = subprocess.run(
            ["launchctl", "bootstrap", domain, str(path)],
            check=False,
            capture_output=True,
            text=True,
        )
        started = result.returncode == 0
        detail = result.stderr.strip() if not started else ""
    else:
        detail = ""
    return ServiceInstallResult(path, changed, started, detail)


def install_systemd_user_service(*, start: bool, dry_run: bool) -> ServiceInstallResult:
    path = Path.home() / ".config" / "systemd" / "user" / "sidepulse.service"
    command = " ".join(systemd_quote(part) for part in service_command())
    text = (
        "[Unit]\n"
        "Description=SidePulse background agent\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        f"ExecStart={command}\n"
        "Restart=on-failure\n"
        "RestartSec=2\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    existing = path.read_text(encoding="utf-8") if path.exists() else None
    changed = existing != text
    if dry_run:
        return ServiceInstallResult(path, changed, False, "dry run")
    path.parent.mkdir(parents=True, exist_ok=True)
    if changed:
        path.write_text(text, encoding="utf-8")
    systemctl = shutil.which("systemctl")
    if not start or systemctl is None:
        detail = "systemd user manager unavailable" if start else ""
        return ServiceInstallResult(path, changed, False, detail)
    reload_result = subprocess.run(
        [systemctl, "--user", "daemon-reload"],
        check=False,
        capture_output=True,
        text=True,
    )
    if reload_result.returncode != 0:
        return ServiceInstallResult(
            path,
            changed,
            False,
            reload_result.stderr.strip() or "systemd user manager unavailable",
        )
    enable_result = subprocess.run(
        [systemctl, "--user", "enable", "sidepulse.service"],
        check=False,
        capture_output=True,
        text=True,
    )
    # Restart rather than `enable --now`. An upgrade replaces the files the
    # running service loaded at start, and `--now` leaves an already-running
    # unit alone, so the old code would keep driving the LEDs until the next
    # reboot. Restart starts a stopped unit and replaces a running one.
    result = subprocess.run(
        [systemctl, "--user", "restart", "sidepulse.service"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return ServiceInstallResult(path, changed, True, "")
    detail = result.stderr.strip() or enable_result.stderr.strip()
    return ServiceInstallResult(path, changed, False, detail)


def restart_service() -> ServiceInstallResult:
    """Recover the installed service without registering the caller's Python/app."""
    if sys.platform == 'win32':
        from .windows_service import restart
        return restart()
    if sys.platform == 'darwin':
        path = Path.home() / 'Library' / 'LaunchAgents' / f'{SERVICE_LABEL}.plist'
        if not path.is_file():
            return ServiceInstallResult(path, False, False, 'Service is not installed; run sidepulse setup')
        domain = f'gui/{os.getuid()}'
        result = subprocess.run(['launchctl', 'kickstart', '-k', f'{domain}/{SERVICE_LABEL}'],
                                check=False, capture_output=True, text=True)
        if result.returncode != 0:
            # An installed plist may not be loaded, for example after a stop.
            result = subprocess.run(['launchctl', 'bootstrap', domain, str(path)],
                                    check=False, capture_output=True, text=True)
        return ServiceInstallResult(path, False, result.returncode == 0, result.stderr.strip())
    if sys.platform.startswith('linux'):
        path = Path.home() / '.config' / 'systemd' / 'user' / 'sidepulse.service'
        systemctl = shutil.which('systemctl')
        if not path.is_file() or systemctl is None:
            return ServiceInstallResult(path, False, False, 'Service is not installed or systemd is unavailable')
        result = subprocess.run([systemctl, '--user', 'restart', 'sidepulse.service'],
                                check=False, capture_output=True, text=True)
        return ServiceInstallResult(path, False, result.returncode == 0, result.stderr.strip())
    return ServiceInstallResult(Path(), False, False, 'unsupported platform')


def stop_service() -> bool:
    if sys.platform == 'win32':
        from .windows_service import stop
        return stop()
    if sys.platform == "darwin":
        path = Path.home() / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"
        domain = f"gui/{subprocess.check_output(['id', '-u'], text=True).strip()}"
        result = subprocess.run(
            ["launchctl", "bootout", domain, str(path)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return result.returncode == 0
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        return False
    result = subprocess.run(
        [systemctl, "--user", "stop", "sidepulse.service"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def service_is_running() -> bool:
    if sys.platform == 'win32':
        from .windows_service import alive
        return alive()
    if sys.platform == "darwin":
        domain = f"gui/{subprocess.check_output(['id', '-u'], text=True).strip()}"
        result = subprocess.run(
            ["launchctl", "print", f"{domain}/{SERVICE_LABEL}"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return result.returncode == 0
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        return False
    result = subprocess.run(
        [systemctl, "--user", "is-active", "--quiet", "sidepulse.service"],
        check=False,
    )
    return result.returncode == 0


def systemd_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
