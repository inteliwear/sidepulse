from __future__ import annotations

import json
import os
import queue
import re
import shutil
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .providers import (
    CLAUDE_EVENTS,
    CODEX_EVENTS,
    CURSOR_EVENTS,
    GROK_EVENTS,
    default_cursor_hook_config_path,
    default_grok_hook_config_path,
    detect_log_path,
)

MANAGED_START = "# >>> agent-monitor hooks >>>"
MANAGED_END = "# <<< agent-monitor hooks <<<"


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


def install_codex_hooks(
    log_path: Path | None = None,
    config_path: Path | None = None,
    dry_run: bool = False,
    python_executable: str | None = None,
) -> InstallResult:
    config = config_path or Path.home() / ".codex" / "config.toml"
    target_log = (log_path or detect_log_path("codex")).expanduser()
    original = config.read_text() if config.exists() else ""

    text = strip_managed_block(original)
    text = remove_codex_hook_blocks_for_log(text, target_log)
    text = ensure_codex_hooks_feature(text)
    block = codex_hook_block(target_log, python_executable)
    new_text = _ensure_trailing_newline(text) + "\n" + block
    changed = new_text != original

    backup = None
    if not dry_run:
        config.parent.mkdir(parents=True, exist_ok=True)
        if changed:
            backup = backup_file(config)
            config.write_text(new_text)

        if should_refresh_codex_hook_trust(config, config_path):
            trusted_hashes = resolve_codex_hook_hashes(config)
            if trusted_hashes:
                current_text = config.read_text() if config.exists() else ""
                trusted_text = update_codex_trusted_hashes(current_text, trusted_hashes)
                if trusted_text != current_text:
                    if backup is None:
                        backup = backup_file(config)
                    config.write_text(trusted_text)
                    changed = True

        target_log.parent.mkdir(parents=True, exist_ok=True)
        target_log.touch(exist_ok=True)

    return InstallResult("codex", config, target_log, changed, backup, dry_run)


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
    scoped_hooks = claude_hook_settings(
        target_log,
        python_executable=python_executable,
    )["hooks"]

    for event_name, managed_entries in scoped_hooks.items():
        entries = hooks.get(event_name, [])
        if not isinstance(entries, list):
            entries = []
        cleaned = remove_claude_hooks_for_log(entries, target_log)
        cleaned.extend(managed_entries)
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


def claude_hook_settings(
    log_path: Path | None = None,
    *,
    python_executable: str | None = None,
) -> dict[str, Any]:
    """Build invocation-scoped Claude settings without touching user config."""
    target_log = (log_path or detect_log_path("claude")).expanduser()
    command = hook_command("claude", target_log, python_executable)
    return {
        "hooks": {
            event_name: [
                {
                    "matcher": "*",
                    "hooks": [{"type": "command", "command": command}],
                }
            ]
            for event_name in CLAUDE_EVENTS
        }
    }


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


def uninstall_codex_hooks(
    log_path: Path | None = None,
    config_path: Path | None = None,
    dry_run: bool = False,
) -> InstallResult:
    config = config_path or Path.home() / ".codex" / "config.toml"
    target_log = (log_path or detect_log_path("codex")).expanduser()
    original = config.read_text() if config.exists() else ""

    text = strip_managed_block(original)
    text = remove_codex_hook_blocks_for_log(text, target_log)
    new_text = _normalize_config_text(text) if text != original else original
    changed = new_text != original

    backup = None
    if changed and not dry_run:
        config.parent.mkdir(parents=True, exist_ok=True)
        backup = backup_file(config)
        config.write_text(new_text)

    return InstallResult("codex", config, target_log, changed, backup, dry_run)


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


def install_cursor_hooks(
    log_path: Path | None = None,
    config_path: Path | None = None,
    dry_run: bool = False,
    python_executable: str | None = None,
) -> InstallResult:
    config = config_path or default_cursor_hook_config_path()
    target_log = (log_path or detect_log_path("cursor")).expanduser()
    data = read_json_config(config)
    original = json.dumps(data, sort_keys=True)
    data.setdefault("version", 1)
    hooks = data.setdefault("hooks", {})

    for event_name in CURSOR_EVENTS:
        entries = hooks.get(event_name, [])
        if not isinstance(entries, list):
            entries = []
        # Anything we did not write -- including entries in a shape a future
        # Cursor release introduces -- is preserved untouched.
        cleaned = remove_cursor_hook_entries(entries)
        cleaned.append(
            {
                "command": cursor_hook_command(
                    event_name,
                    target_log,
                    python_executable,
                )
            }
        )
        hooks[event_name] = cleaned

    changed = json.dumps(data, sort_keys=True) != original
    backup = None
    if changed and not dry_run:
        config.parent.mkdir(parents=True, exist_ok=True)
        backup = backup_file(config)
        config.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n")
        target_log.parent.mkdir(parents=True, exist_ok=True)
        target_log.touch(exist_ok=True)

    return InstallResult("cursor", config, target_log, changed, backup, dry_run)


def uninstall_cursor_hooks(
    log_path: Path | None = None,
    config_path: Path | None = None,
    dry_run: bool = False,
) -> InstallResult:
    config = config_path or default_cursor_hook_config_path()
    target_log = (log_path or detect_log_path("cursor")).expanduser()
    data = read_json_config(config)
    original = json.dumps(data, sort_keys=True)
    hooks = data.get("hooks")
    if isinstance(hooks, dict):
        for event_name in list(hooks):
            entries = hooks.get(event_name)
            if not isinstance(entries, list):
                continue
            cleaned = remove_cursor_hook_entries(entries)
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
        # Only remove the file if nothing of the user's is left in it.
        if data:
            config.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n")
        else:
            try:
                config.unlink()
            except FileNotFoundError:
                pass

    return InstallResult("cursor", config, target_log, changed, backup, dry_run)


def cursor_hook_command(
    event_name: str,
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
                "cursor",
                "--event",
                shlex.quote(event_name),
                "--log",
                shlex.quote(str(log_path.expanduser())),
            ]
        )
        return fail_open_command(command)
    entry_point = Path(__file__).with_name("hook_entry.py")
    command = " ".join(
        [
            shlex.quote(executable),
            shlex.quote(str(entry_point)),
            "--provider",
            "cursor",
            "--event",
            shlex.quote(event_name),
            "--log",
            shlex.quote(str(log_path.expanduser())),
        ]
    )
    return fail_open_command(command)


def remove_cursor_hook_entries(entries: list[Any]) -> list[Any]:
    """Drop entries this installer wrote, in any generation's spelling."""
    return [
        entry
        for entry in entries
        if not (
            isinstance(entry, dict)
            and (
                "sidepulse.cursor_hook" in str(entry.get("command") or "")
                or "agent-monitor hook-log" in str(entry.get("command") or "")
                or "hook_entry.py" in str(entry.get("command") or "")
            )
        )
    ]


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
    entry_point = Path(__file__).with_name("hook_entry.py")
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


def read_json_config(config: Path) -> dict[str, Any]:
    if not config.exists():
        return {}
    data = json.loads(config.read_text())
    return data if isinstance(data, dict) else {}


def grok_hook_entry(event_name: str, command: str) -> dict[str, Any]:
    entry: dict[str, Any] = {"hooks": [{"type": "command", "command": command}]}
    if event_name in {"PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionDenied", "Notification"}:
        entry["matcher"] = "*"
    return entry


def hook_pythonpath_assignment() -> str:
    package_root = Path(__file__).resolve().parents[1]
    if not package_root.exists():
        return ""
    return f"PYTHONPATH={shlex.quote(str(package_root))} "


def codex_hook_block(
    log_path: Path,
    python_executable: str | None = None,
) -> str:
    command = hook_command("codex", log_path, python_executable)
    lines = [
        MANAGED_START,
        "# Provider-neutral status collection. Do not edit inside this block.",
    ]
    for event_name in CODEX_EVENTS:
        lines.extend(
            [
                f"[[hooks.{event_name}]]",
                'matcher = "*"',
                f"[[hooks.{event_name}.hooks]]",
                'type = "command"',
                f"command = '''{command}'''",
                "",
            ]
        )
    lines.append(MANAGED_END)
    return "\n".join(lines) + "\n"


def should_refresh_codex_hook_trust(config: Path, explicit_config: Path | None) -> bool:
    default_config = Path.home() / ".codex" / "config.toml"
    try:
        return config.expanduser().resolve() == default_config.expanduser().resolve()
    except OSError:
        return explicit_config is None


def resolve_codex_hook_hashes(
    config_path: Path,
    cwd: Path | None = None,
    timeout_seconds: float = 8.0,
) -> dict[str, str]:
    codex = codex_cli_path()
    if codex is None:
        return {}

    try:
        process = subprocess.Popen(
            [str(codex), "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=str(cwd or Path.cwd()),
        )
    except OSError:
        return {}

    messages: queue.Queue[tuple[str, str]] = queue.Queue()

    def read_stream(name: str, stream: Any) -> None:
        for line in stream:
            messages.put((name, line.rstrip("\n")))

    for name, stream in (("out", process.stdout), ("err", process.stderr)):
        if stream is not None:
            threading.Thread(target=read_stream, args=(name, stream), daemon=True).start()

    def send(payload: dict[str, Any]) -> bool:
        if process.stdin is None:
            return False
        try:
            process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
            process.stdin.flush()
        except OSError:
            return False
        return True

    def wait_for_id(message_id: int) -> dict[str, Any] | None:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            try:
                name, line = messages.get(timeout=0.1)
            except queue.Empty:
                continue
            if name != "out":
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if payload.get("id") == message_id:
                return payload
        return None

    try:
        if not send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "sidepulse", "version": "0"},
                    "capabilities": None,
                },
            }
        ):
            return {}
        wait_for_id(1)
        if not send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "hooks/list",
                "params": {"cwds": [str(cwd or Path.cwd())]},
            }
        ):
            return {}
        response = wait_for_id(2)
    finally:
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()

    if not response:
        return {}

    try:
        hooks = response["result"]["data"][0]["hooks"]
    except (KeyError, IndexError, TypeError):
        return {}

    source_path = str(config_path.expanduser())
    trusted_hashes: dict[str, str] = {}
    for hook in hooks:
        if not isinstance(hook, dict):
            continue
        command = hook.get("command")
        current_hash = hook.get("currentHash")
        key = hook.get("key")
        if hook.get("sourcePath") != source_path:
            continue
        if not isinstance(command, str) or "hook_entry.py" not in command:
            continue
        if not isinstance(current_hash, str) or not isinstance(key, str):
            continue
        trusted_hashes[key] = current_hash
    return trusted_hashes


def codex_cli_path() -> Path | None:
    env_path = os.environ.get("CODEX_CLI_PATH")
    candidates = [
        Path(env_path).expanduser() if env_path else None,
        Path("/Applications/ChatGPT.app/Contents/Resources/codex"),
        Path("/Applications/Codex.app/Contents/Resources/codex"),
        Path(shutil.which("codex")).expanduser() if shutil.which("codex") else None,
    ]
    for candidate in candidates:
        if candidate and candidate.exists():
            return candidate
    return None


def update_codex_trusted_hashes(text: str, trusted_hashes: dict[str, str]) -> str:
    if not trusted_hashes:
        return text

    result = _ensure_hooks_state_table(text)
    for key, trusted_hash in trusted_hashes.items():
        result = _set_codex_trusted_hash(result, key, trusted_hash)
    return result


def _ensure_hooks_state_table(text: str) -> str:
    if re.search(r"^\s*\[hooks\.state\]\s*$", text, re.MULTILINE):
        return text
    return _ensure_trailing_newline(text) + "\n[hooks.state]\n"


def _set_codex_trusted_hash(text: str, key: str, trusted_hash: str) -> str:
    header = f'[hooks.state."{toml_basic_string_escape(key)}"]'
    lines = text.splitlines(keepends=True)
    header_index = None
    for index, line in enumerate(lines):
        if line.strip() == header:
            header_index = index
            break

    if header_index is None:
        block = f'\n{header}\ntrusted_hash = "{toml_basic_string_escape(trusted_hash)}"\n'
        return _ensure_trailing_newline(text) + block

    end = len(lines)
    for index in range(header_index + 1, len(lines)):
        if re.match(r"\s*\[.*\]\s*$", lines[index]):
            end = index
            break

    trusted_line = f'trusted_hash = "{toml_basic_string_escape(trusted_hash)}"\n'
    for index in range(header_index + 1, end):
        if re.match(r"\s*trusted_hash\s*=", lines[index]):
            lines[index] = trusted_line
            return "".join(lines)

    lines.insert(header_index + 1, trusted_line)
    return "".join(lines)


def toml_basic_string_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def strip_managed_block(text: str) -> str:
    # Codex may append its own tables between these comments when it rewrites
    # config.toml.  Remove only the comments; hook tables are removed below.
    return "\n".join(
        line for line in text.splitlines() if line.strip() not in {MANAGED_START, MANAGED_END}
    ) + ("\n" if text.endswith("\n") else "")


def remove_codex_hook_blocks_for_log(text: str, log_path: Path) -> str:
    target = str(log_path)
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    index = 0

    while index < len(lines):
        event_match = re.match(r"\s*\[\[hooks\.([A-Za-z0-9_]+)\]\]\s*$", lines[index])
        if event_match and event_match.group(1) in CODEX_EVENTS:
            event_name = event_match.group(1)
            end = index + 1
            nested = re.compile(rf"\s*\[\[hooks\.{re.escape(event_name)}\.hooks\]\]\s*$")
            table = re.compile(r"\s*\[.*\]\s*$")
            while end < len(lines):
                if table.match(lines[end]) and not nested.match(lines[end]):
                    break
                end += 1
            block = "".join(lines[index:end])
            if target in block or "sidepulse hook-log" in block or "hook_entry.py" in block:
                index = end
                continue

        if "Event logging hooks:" in lines[index] and target in text:
            index += 1
            continue

        out.append(lines[index])
        index += 1

    return "".join(out)


def remove_claude_hooks_for_log(entries: list[Any], log_path: Path) -> list[dict[str, Any]]:
    return remove_json_command_hooks_for_log(entries, log_path)


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


def ensure_codex_hooks_feature(text: str) -> str:
    lines = text.splitlines(keepends=True)
    features_index = None
    for index, line in enumerate(lines):
        if re.match(r"\s*\[features\]\s*$", line):
            features_index = index
            break

    if features_index is None:
        return _ensure_trailing_newline(text) + "\n[features]\nhooks = true\n"

    end = len(lines)
    for index in range(features_index + 1, len(lines)):
        if re.match(r"\s*\[.*\]\s*$", lines[index]):
            end = index
            break

    for index in range(features_index + 1, end):
        if re.match(r"\s*hooks\s*=", lines[index]):
            lines[index] = "hooks = true\n"
            return "".join(lines)

    lines.insert(end, "hooks = true\n")
    return "".join(lines)


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
