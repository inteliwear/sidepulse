from __future__ import annotations

import json
import shlex
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MANAGED_START = "# >>> agent-monitor hooks >>>"
MANAGED_END = "# <<< agent-monitor hooks <<<"

_PACKAGE_DIR = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class InstallResult:
    provider: str
    config_path: Path
    log_path: Path
    changed: bool
    backup_path: Path | None = None
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "config_path": str(self.config_path),
            "log_path": str(self.log_path),
            "changed": self.changed,
            "backup_path": str(self.backup_path) if self.backup_path else None,
            "dry_run": self.dry_run,
        }


def hook_command(
    provider: str,
    log_path: Path,
    python_executable: str | None = None,
) -> str:
    executable = python_executable or sys.executable or "python3"
    if getattr(sys, "frozen", False) and python_executable is None:
        command = " ".join(
            [
                shlex.quote(executable),
                "agent-monitor",
                "hook-log",
                "--provider",
                shlex.quote(provider),
                "--log",
                shlex.quote(str(log_path.expanduser())),
            ]
        )
        return fail_open_command(command)
    entry_point = _PACKAGE_DIR / "hook_entry.py"
    command = " ".join(
        [
            shlex.quote(executable),
            shlex.quote(str(entry_point)),
            "--provider",
            shlex.quote(provider),
            "--log",
            shlex.quote(str(log_path.expanduser())),
        ]
    )
    return fail_open_command(command)


def fail_open_command(command: str) -> str:
    return f"{command} ; true"


def hook_pythonpath_assignment() -> str:
    package_root = _PACKAGE_DIR.parent
    if not package_root.exists():
        return ""
    return f"PYTHONPATH={shlex.quote(str(package_root))} "


def read_json_config(config: Path) -> dict[str, Any]:
    if not config.exists():
        return {}
    data = json.loads(config.read_text())
    return data if isinstance(data, dict) else {}


def remove_json_command_hooks_for_log(entries: list[Any], log_path: Path) -> list[dict[str, Any]]:
    target = str(log_path)
    cleaned_entries: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        hooks = entry.get("hooks")
        if not isinstance(hooks, list):
            continue
        cleaned_hooks = []
        for hook in hooks:
            if not isinstance(hook, dict):
                continue
            command = hook.get("command", "")
            if target in command or "sidepulse hook-log" in command or "hook_entry.py" in command:
                continue
            cleaned_hooks.append(hook)
        if cleaned_hooks:
            kept = dict(entry)
            kept["hooks"] = cleaned_hooks
            cleaned_entries.append(kept)
    return cleaned_entries


def backup_file(path: Path, backup_dir: Path | None = None) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target_dir = backup_dir or path.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    backup = target_dir / f"{path.name}.bak.{stamp}"
    backup.write_bytes(path.read_bytes())
    return backup


def _ensure_trailing_newline(text: str) -> str:
    return text if not text or text.endswith("\n") else text + "\n"


def _normalize_config_text(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return ""
    return stripped + "\n"
