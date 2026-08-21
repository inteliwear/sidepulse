from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..providers import CLAUDE_EVENTS, detect_log_path
from ._common import (
    InstallResult,
    backup_file,
    hook_command,
    remove_json_command_hooks_for_log,
)


def install_claude_hooks(
    log_path: Path | None = None,
    config_path: Path | None = None,
    dry_run: bool = False,
    python_executable: str | None = None,
) -> InstallResult:
    config = config_path or Path.home() / ".claude" / "settings.json"
    target_log = (log_path or detect_log_path("claude")).expanduser()

    if config.exists():
        data = json.loads(config.read_text())
    else:
        data = {}

    original = json.dumps(data, sort_keys=True)
    hooks = data.setdefault("hooks", {})
    command = hook_command("claude", target_log, python_executable)

    for event_name in CLAUDE_EVENTS:
        entries = hooks.get(event_name, [])
        if not isinstance(entries, list):
            entries = []
        cleaned = remove_claude_hooks_for_log(entries, target_log)
        cleaned.append({"matcher": "*", "hooks": [{"type": "command", "command": command}]})
        hooks[event_name] = cleaned

    changed = json.dumps(data, sort_keys=True) != original
    backup = None
    if changed and not dry_run:
        config.parent.mkdir(parents=True, exist_ok=True)
        backup = backup_file(config)
        config.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n")
        target_log.parent.mkdir(parents=True, exist_ok=True)
        target_log.touch(exist_ok=True)

    return InstallResult("claude", config, target_log, changed, backup, dry_run)


def uninstall_claude_hooks(
    log_path: Path | None = None,
    config_path: Path | None = None,
    dry_run: bool = False,
) -> InstallResult:
    config = config_path or Path.home() / ".claude" / "settings.json"
    target_log = (log_path or detect_log_path("claude")).expanduser()

    if config.exists():
        data = json.loads(config.read_text())
    else:
        data = {}

    original = json.dumps(data, sort_keys=True)
    hooks = data.get("hooks")
    if isinstance(hooks, dict):
        for event_name in list(hooks):
            entries = hooks.get(event_name)
            if event_name not in CLAUDE_EVENTS or not isinstance(entries, list):
                continue

            cleaned = remove_claude_hooks_for_log(entries, target_log)
            if cleaned:
                hooks[event_name] = cleaned
            else:
                hooks.pop(event_name, None)

        if not hooks:
            data.pop("hooks", None)

    changed = json.dumps(data, sort_keys=True) != original
    backup = None
    if changed and not dry_run:
        config.parent.mkdir(parents=True, exist_ok=True)
        backup = backup_file(config)
        config.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n")

    return InstallResult("claude", config, target_log, changed, backup, dry_run)


def remove_claude_hooks_for_log(entries: list[Any], log_path: Path) -> list[dict[str, Any]]:
    return remove_json_command_hooks_for_log(entries, log_path)
