from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..providers import GROK_EVENTS, default_grok_hook_config_path, detect_log_path
from ._common import (
    InstallResult,
    backup_file,
    hook_command,
    read_json_config,
    remove_json_command_hooks_for_log,
)


def install_grok_hooks(
    log_path: Path | None = None,
    config_path: Path | None = None,
    dry_run: bool = False,
    python_executable: str | None = None,
) -> InstallResult:
    config = config_path or default_grok_hook_config_path()
    target_log = (log_path or detect_log_path("grok")).expanduser()
    data = read_json_config(config)

    original = json.dumps(data, sort_keys=True)
    hooks = data.setdefault("hooks", {})
    command = hook_command("grok", target_log, python_executable)

    for event_name in GROK_EVENTS:
        entries = hooks.get(event_name, [])
        if not isinstance(entries, list):
            entries = []
        cleaned = remove_json_command_hooks_for_log(entries, target_log)
        cleaned.append(grok_hook_entry(event_name, command))
        hooks[event_name] = cleaned

    legacy_changed = any(
        grok_legacy_hook_file_would_change(path, target_log)
        for path in grok_legacy_hook_config_paths(config)
    )
    backup_changed = grok_live_backup_hook_files_would_change(config)
    changed = json.dumps(data, sort_keys=True) != original or legacy_changed or backup_changed
    backup = None
    if changed and not dry_run:
        config.parent.mkdir(parents=True, exist_ok=True)
        backup = backup_file(config, grok_hook_backup_dir(config)) if config.exists() else None
        config.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n")
        clean_grok_legacy_hook_files(config, target_log)
        clean_grok_live_backup_hook_files(config)
        target_log.parent.mkdir(parents=True, exist_ok=True)
        target_log.touch(exist_ok=True)

    return InstallResult("grok", config, target_log, changed, backup, dry_run)


def uninstall_grok_hooks(
    log_path: Path | None = None,
    config_path: Path | None = None,
    dry_run: bool = False,
) -> InstallResult:
    config = config_path or default_grok_hook_config_path()
    target_log = (log_path or detect_log_path("grok")).expanduser()
    data = read_json_config(config)

    original = json.dumps(data, sort_keys=True)
    hooks = data.get("hooks")
    if isinstance(hooks, dict):
        for event_name in list(hooks):
            entries = hooks.get(event_name)
            if event_name not in GROK_EVENTS or not isinstance(entries, list):
                continue

            cleaned = remove_json_command_hooks_for_log(entries, target_log)
            if cleaned:
                hooks[event_name] = cleaned
            else:
                hooks.pop(event_name, None)

        if not hooks:
            data.pop("hooks", None)

    changed = json.dumps(data, sort_keys=True) != original
    legacy_changed = any(
        grok_legacy_hook_file_would_change(path, target_log)
        for path in grok_legacy_hook_config_paths(config)
    )
    backup_changed = grok_live_backup_hook_files_would_change(config)
    changed = changed or legacy_changed or backup_changed
    backup = None
    if changed and not dry_run:
        config.parent.mkdir(parents=True, exist_ok=True)
        backup = backup_file(config, grok_hook_backup_dir(config)) if config.exists() else None
        if data:
            config.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n")
        else:
            try:
                config.unlink()
            except FileNotFoundError:
                pass
        clean_grok_legacy_hook_files(config, target_log)
        clean_grok_live_backup_hook_files(config)

    return InstallResult("grok", config, target_log, changed, backup, dry_run)


def grok_legacy_hook_config_paths(config: Path) -> tuple[Path, ...]:
    return (
        config.parent / "sidepulse-agent-monitor.json",
        config.parent / "sidepulse-cli.json",
    )


def grok_hook_backup_dir(config: Path) -> Path:
    return config.parent.parent / "sidepulse-hook-backups"


def grok_live_backup_hook_paths(config: Path) -> tuple[Path, ...]:
    hooks_dir = config.parent
    if not hooks_dir.exists():
        return ()
    return tuple(
        sorted(
            path
            for path in hooks_dir.iterdir()
            if path.is_file()
            and path.name.startswith("sidepulse")
            and ".json.bak." in path.name
        )
    )


def grok_live_backup_hook_files_would_change(config: Path) -> bool:
    return bool(grok_live_backup_hook_paths(config))


def clean_grok_live_backup_hook_files(config: Path) -> None:
    backup_dir = grok_hook_backup_dir(config)
    for path in grok_live_backup_hook_paths(config):
        backup_dir.mkdir(parents=True, exist_ok=True)
        destination = backup_dir / path.name
        if destination.exists():
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            destination = backup_dir / f"{path.name}.{stamp}"
        path.replace(destination)


def grok_legacy_hook_file_would_change(path: Path, log_path: Path) -> bool:
    if not path.exists():
        return False
    try:
        data = read_json_config(path)
    except Exception:
        return False
    cleaned = clean_json_hook_data(data, log_path, GROK_EVENTS)
    return json.dumps(cleaned, sort_keys=True) != json.dumps(data, sort_keys=True)


def clean_grok_legacy_hook_files(config: Path, log_path: Path) -> None:
    for path in grok_legacy_hook_config_paths(config):
        if not path.exists():
            continue
        try:
            data = read_json_config(path)
        except Exception:
            continue
        cleaned = clean_json_hook_data(data, log_path, GROK_EVENTS)
        if json.dumps(cleaned, sort_keys=True) == json.dumps(data, sort_keys=True):
            continue
        backup_file(path, grok_hook_backup_dir(config))
        if cleaned:
            path.write_text(json.dumps(cleaned, indent=2, sort_keys=False) + "\n")
        else:
            path.unlink()


def clean_json_hook_data(
    data: dict[str, Any],
    log_path: Path,
    event_names: tuple[str, ...],
) -> dict[str, Any]:
    cleaned_data = dict(data)
    hooks = cleaned_data.get("hooks")
    if not isinstance(hooks, dict):
        return cleaned_data

    cleaned_hooks = dict(hooks)
    for event_name in list(cleaned_hooks):
        entries = cleaned_hooks.get(event_name)
        if event_name not in event_names or not isinstance(entries, list):
            continue
        cleaned = remove_json_command_hooks_for_log(entries, log_path)
        if cleaned:
            cleaned_hooks[event_name] = cleaned
        else:
            cleaned_hooks.pop(event_name, None)

    if cleaned_hooks:
        cleaned_data["hooks"] = cleaned_hooks
    else:
        cleaned_data.pop("hooks", None)
    return cleaned_data


def grok_hook_entry(event_name: str, command: str) -> dict[str, Any]:
    entry: dict[str, Any] = {"hooks": [{"type": "command", "command": command}]}
    if event_name in {"PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionDenied", "Notification"}:
        entry["matcher"] = "*"
    return entry
