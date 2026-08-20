from __future__ import annotations

import getpass
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from .settings import (
    CLOSED_LID_AWAKE_AGENTS,
    CLOSED_LID_AWAKE_ALWAYS,
    CLOSED_LID_AWAKE_NEVER,
)


CAFFEINATE_CLOSED_LID_COMMAND = ("/usr/bin/caffeinate", "-imsu")
IOREG_CLAMSHELL_COMMAND = ("/usr/sbin/ioreg", "-r", "-k", "AppleClamshellState", "-d", "4")
IOREG_SLEEP_DISABLED_COMMAND = ("/usr/sbin/ioreg", "-r", "-k", "SleepDisabled", "-d", "4")
PMSET_ASSERTIONS_COMMAND = ("/usr/bin/pmset", "-g", "assertions")
SUDO_PMSET_DISABLE_SLEEP_COMMAND = (
    "/usr/bin/sudo",
    "-n",
    "/usr/bin/pmset",
    "-a",
    "disablesleep",
)
SLEEP_HELPER_SUDOERS_PATH = Path("/etc/sudoers.d/sidepulse-disablesleep")
LID_POLL_SECONDS = 1.0


CommandRunner = Callable[..., subprocess.CompletedProcess]


@dataclass(frozen=True)
class SleepHelperInstallResult:
    path: Path
    user: str
    changed: bool
    installed: bool
    dry_run: bool = False


class SleepHelperRequiredError(RuntimeError):
    pass


@dataclass(frozen=True)
class MacSleepSnapshot:
    sleep_disabled: bool | None = None
    prevent_system_sleep: bool | None = None
    prevent_user_idle_system_sleep: bool | None = None
    prevent_user_idle_display_sleep: bool | None = None
    user_is_active: bool | None = None
    assertions: dict[str, bool] | None = None
    error: str | None = None

    @property
    def sleep_prevented(self) -> bool | None:
        known = [
            self.sleep_disabled,
            self.prevent_system_sleep,
            self.prevent_user_idle_system_sleep,
        ]
        if any(value is True for value in known):
            return True
        if any(value is None for value in known):
            return None
        return False


def closed_lid_awake_should_hold(policy: str, *, agents_active: bool) -> bool:
    if policy == CLOSED_LID_AWAKE_ALWAYS:
        return True
    if policy == CLOSED_LID_AWAKE_AGENTS:
        return agents_active
    return False


def read_lid_closed(
    *,
    runner: CommandRunner = subprocess.run,
    command: Sequence[str] = IOREG_CLAMSHELL_COMMAND,
) -> bool | None:
    result = runner(
        list(command),
        check=True,
        capture_output=True,
        text=True,
        timeout=2,
    )
    return parse_bool_ioreg_property(result.stdout, "AppleClamshellState")


def read_sleep_disabled(
    *,
    runner: CommandRunner = subprocess.run,
    command: Sequence[str] = IOREG_SLEEP_DISABLED_COMMAND,
) -> bool | None:
    result = runner(
        list(command),
        check=True,
        capture_output=True,
        text=True,
        timeout=2,
    )
    return parse_bool_ioreg_property(result.stdout, "SleepDisabled")


def read_pmset_assertions(
    *,
    runner: CommandRunner = subprocess.run,
    command: Sequence[str] = PMSET_ASSERTIONS_COMMAND,
) -> dict[str, bool]:
    result = runner(
        list(command),
        check=True,
        capture_output=True,
        text=True,
        timeout=2,
    )
    return parse_pmset_assertions(result.stdout)


def read_mac_sleep_snapshot(
    *,
    runner: CommandRunner = subprocess.run,
) -> MacSleepSnapshot:
    errors: list[str] = []
    sleep_disabled: bool | None = None
    assertions: dict[str, bool] = {}

    try:
        sleep_disabled = read_sleep_disabled(runner=runner)
    except Exception as exc:
        errors.append(f"SleepDisabled: {exc}")

    try:
        assertions = read_pmset_assertions(runner=runner)
    except Exception as exc:
        errors.append(f"pmset assertions: {exc}")

    return MacSleepSnapshot(
        sleep_disabled=sleep_disabled,
        prevent_system_sleep=assertions.get("PreventSystemSleep"),
        prevent_user_idle_system_sleep=assertions.get("PreventUserIdleSystemSleep"),
        prevent_user_idle_display_sleep=assertions.get("PreventUserIdleDisplaySleep"),
        user_is_active=assertions.get("UserIsActive"),
        assertions=assertions,
        error="; ".join(errors) if errors else None,
    )


def parse_bool_ioreg_property(text: str | bytes, property_name: str) -> bool | None:
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    pattern = rf'"{re.escape(property_name)}"\s*=\s*(Yes|No|true|false|1|0)'
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if match is None:
        return None
    value = match.group(1).lower()
    if value in {"yes", "true", "1"}:
        return True
    if value in {"no", "false", "0"}:
        return False
    return None


def parse_pmset_assertions(text: str | bytes) -> dict[str, bool]:
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    assertions: dict[str, bool] = {}
    for line in text.splitlines():
        match = re.match(r"\s*([A-Za-z][A-Za-z0-9]+)\s+([01])\s*$", line)
        if match is None:
            continue
        assertions[match.group(1)] = match.group(2) == "1"
    return assertions


def run_sudo_pmset_disablesleep(
    enabled: bool,
    *,
    runner: CommandRunner = subprocess.run,
) -> None:
    value = "1" if enabled else "0"
    command = [*SUDO_PMSET_DISABLE_SLEEP_COMMAND, value]
    result = runner(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode == 0:
        return

    detail = (result.stderr or result.stdout or "").strip()
    if detail:
        detail = f" ({detail.splitlines()[0]})"
    raise SleepHelperRequiredError(
        "Lid-closed awake needs one-time setup: "
        f"{sleep_helper_install_command()}"
        f"{detail}"
    )


def run_privileged_pmset_disablesleep(
    enabled: bool,
    *,
    runner: CommandRunner = subprocess.run,
) -> None:
    """Compatibility wrapper for the old name.

    Runtime sleep control is intentionally non-interactive. Any admin prompt
    belongs to an explicit helper installation step, never a background refresh.
    """
    run_sudo_pmset_disablesleep(enabled, runner=runner)


def sleep_helper_sudoers_rule(user: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", user):
        raise ValueError(f"invalid sudoers user: {user!r}")
    return (
        f"{user} ALL=(root) NOPASSWD: "
        "/usr/bin/pmset -a disablesleep 0, "
        "/usr/bin/pmset -a disablesleep 1\n"
    )


def sleep_helper_target_user() -> str:
    for value in (os.environ.get("SUDO_USER"), os.environ.get("USER")):
        if value:
            return value
    try:
        return os.getlogin()
    except OSError:
        return getpass.getuser()


def sleep_helper_installed(path: Path = SLEEP_HELPER_SUDOERS_PATH) -> bool:
    return path.exists()


def sleep_helper_install_command(executable: str = "sidepulse") -> str:
    resolved = shutil.which(executable) or executable
    return f"sudo {shlex.quote(resolved)} status-bar install-sleep-helper"


def install_sleep_helper(
    *,
    user: str | None = None,
    path: Path = SLEEP_HELPER_SUDOERS_PATH,
    dry_run: bool = False,
    runner: CommandRunner = subprocess.run,
) -> SleepHelperInstallResult:
    target_user = user or sleep_helper_target_user()
    rule = sleep_helper_sudoers_rule(target_user)
    try:
        current = path.read_text(encoding="utf-8") if path.exists() else None
    except OSError:
        current = None
    changed = current != rule

    if dry_run:
        return SleepHelperInstallResult(
            path=path,
            user=target_user,
            changed=changed,
            installed=not changed,
            dry_run=True,
        )

    require_root_for_sleep_helper()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp_path.write_text(rule, encoding="utf-8")
        os.chown(tmp_path, 0, 0)
        os.chmod(tmp_path, 0o440)
        runner(
            ["/usr/sbin/visudo", "-cf", str(tmp_path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if changed:
            tmp_path.replace(path)
        else:
            tmp_path.unlink()
    except Exception:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise

    return SleepHelperInstallResult(
        path=path,
        user=target_user,
        changed=changed,
        installed=True,
    )


def uninstall_sleep_helper(
    *,
    path: Path = SLEEP_HELPER_SUDOERS_PATH,
    dry_run: bool = False,
) -> SleepHelperInstallResult:
    target_user = sleep_helper_target_user()
    changed = path.exists()
    if dry_run:
        return SleepHelperInstallResult(
            path=path,
            user=target_user,
            changed=changed,
            installed=changed,
            dry_run=True,
        )

    require_root_for_sleep_helper()
    if changed:
        path.unlink()
    return SleepHelperInstallResult(
        path=path,
        user=target_user,
        changed=changed,
        installed=False,
    )


def require_root_for_sleep_helper() -> None:
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        raise PermissionError(f"run once with sudo: {sleep_helper_install_command()}")


class ClosedLidAwakeController:
    def __init__(
        self,
        *,
        command: Sequence[str] = CAFFEINATE_CLOSED_LID_COMMAND,
        process_factory: Callable[..., object] | None = None,
        sleep_disabled_reader: Callable[[], bool | None] = read_sleep_disabled,
        sleep_disabled_setter: Callable[[bool], None] = run_sudo_pmset_disablesleep,
        watch_current_process: bool = True,
        use_system_disable: bool = False,
    ) -> None:
        self.command = tuple(command)
        self.process_factory = process_factory or subprocess.Popen
        self.sleep_disabled_reader = sleep_disabled_reader
        self.sleep_disabled_setter = sleep_disabled_setter
        self.watch_current_process = watch_current_process
        self.use_system_disable = use_system_disable
        self.process = None
        self.changed_system_disable = False
        self.system_disable_attempted = False
        self.last_system_disable_request: bool | None = None
        self.last_error: str | None = None
        self.last_policy = CLOSED_LID_AWAKE_NEVER
        self.last_requested = False

    def set_use_system_disable(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if self.use_system_disable == enabled:
            return
        if not enabled:
            self.release_system_disable()
            self.last_system_disable_request = None
        self.use_system_disable = enabled
        self.system_disable_attempted = False

    def update(self, policy: str, *, agents_active: bool) -> bool:
        should_hold = closed_lid_awake_should_hold(policy, agents_active=agents_active)
        if policy != self.last_policy:
            self.system_disable_attempted = False
        self.last_policy = policy
        self.last_requested = should_hold
        if should_hold:
            self.ensure_awake()
        else:
            self.release()
        return self.active()

    def ensure_awake(self) -> None:
        errors: list[str] = []
        if (
            self.use_system_disable
            and self.last_system_disable_request is not True
            and not self.system_disable_attempted
        ):
            self.system_disable_attempted = True
            try:
                self.sleep_disabled_setter(True)
                self.changed_system_disable = True
                self.last_system_disable_request = True
                self.system_disable_attempted = False
            except Exception as exc:
                errors.append(f"disablesleep: {exc}")

        if not self.process_running():
            try:
                self.process = self.process_factory(
                    self.caffeinate_command(),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception as exc:
                self.process = None
                errors.append(f"caffeinate: {exc}")

        self.last_error = "; ".join(errors) if errors else None

    def release(self) -> None:
        process = self.process
        self.process = None
        errors: list[str] = []
        if process is not None:
            try:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=1)
            except Exception:
                try:
                    process.kill()
                except Exception as exc:
                    errors.append(f"caffeinate: {exc}")

        self.release_system_disable(errors=errors)

        self.last_error = "; ".join(errors) if errors else None

    def release_system_disable(self, *, errors: list[str] | None = None) -> None:
        active_errors = errors if errors is not None else []
        should_apply = (
            self.use_system_disable
            or self.changed_system_disable
            or self.last_system_disable_request is True
        )
        if should_apply and self.last_system_disable_request is not False:
            try:
                self.sleep_disabled_setter(False)
                self.changed_system_disable = False
                self.last_system_disable_request = False
                self.system_disable_attempted = False
            except Exception as exc:
                active_errors.append(f"disablesleep: {exc}")
        else:
            self.system_disable_attempted = False

        if errors is None:
            self.last_error = "; ".join(active_errors) if active_errors else None

    def active(self) -> bool:
        return self.process_running() or self.changed_system_disable

    def process_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def caffeinate_command(self) -> list[str]:
        command = list(self.command)
        if self.watch_current_process:
            command.extend(["-w", str(os.getpid())])
        return command
