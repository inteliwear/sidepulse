from __future__ import annotations

import json
import re
from pathlib import Path

from ..providers import HERMES_EVENTS, default_hermes_config_path, detect_log_path
from ._common import (
    MANAGED_END,
    MANAGED_START,
    InstallResult,
    _ensure_trailing_newline,
    backup_file,
    hook_command,
)

# Hermes runs hooks with shell=False and a hard timeout; the hook entry point
# only appends a JSONL line, so this is generous.
HERMES_HOOK_TIMEOUT = 5


class HermesHookConflict(RuntimeError):
    """Raised when config.yaml already hand-defines an event we manage.

    Emitting our own key alongside it would produce a duplicate YAML mapping
    key, and PyYAML silently keeps only the last — which would drop the user's
    existing hook.
    """

    def __init__(self, config_path: Path, events: tuple[str, ...]) -> None:
        self.config_path = config_path
        self.events = events
        super().__init__(
            f"{config_path} already defines hooks for: {', '.join(events)}. "
            "Remove or rename those entries, then re-run install."
        )


def install_hermes_hooks(
    log_path: Path | None = None,
    config_path: Path | None = None,
    dry_run: bool = False,
    python_executable: str | None = None,
) -> InstallResult:
    config = config_path or default_hermes_config_path()
    target_log = (log_path or detect_log_path("hermes")).expanduser()
    original = config.read_text() if config.exists() else ""

    text = remove_hermes_managed_block(original)
    conflicts = hermes_conflicting_events(text)
    if conflicts:
        raise HermesHookConflict(config, conflicts)

    block = hermes_hook_block(target_log, python_executable)
    new_text = insert_hermes_hook_block(text, block)
    changed = new_text != original

    backup = None
    if changed and not dry_run:
        config.parent.mkdir(parents=True, exist_ok=True)
        backup = backup_file(config)
        config.write_text(new_text)
        target_log.parent.mkdir(parents=True, exist_ok=True)
        target_log.touch(exist_ok=True)

    return InstallResult("hermes", config, target_log, changed, backup, dry_run)


def uninstall_hermes_hooks(
    log_path: Path | None = None,
    config_path: Path | None = None,
    dry_run: bool = False,
) -> InstallResult:
    config = config_path or default_hermes_config_path()
    target_log = (log_path or detect_log_path("hermes")).expanduser()
    original = config.read_text() if config.exists() else ""

    new_text = drop_empty_hermes_hooks_key(remove_hermes_managed_block(original))
    changed = new_text != original

    backup = None
    if changed and not dry_run:
        backup = backup_file(config)
        config.write_text(new_text)

    return InstallResult("hermes", config, target_log, changed, backup, dry_run)


def hermes_hook_block(
    log_path: Path,
    python_executable: str | None = None,
    indent: str = "  ",
) -> str:
    command = hook_command("hermes", log_path, python_executable)
    lines = [
        f"{indent}{MANAGED_START}",
        f"{indent}# Provider-neutral status collection. Do not edit inside this block.",
    ]
    for event_name in HERMES_EVENTS:
        lines.extend(
            [
                f"{indent}{event_name}:",
                f"{indent}  - command: {yaml_double_quote(command)}",
                f"{indent}    timeout: {HERMES_HOOK_TIMEOUT}",
            ]
        )
    lines.append(f"{indent}{MANAGED_END}")
    return "\n".join(lines) + "\n"


def yaml_double_quote(value: str) -> str:
    # JSON strings are valid YAML double-quoted scalars.
    return json.dumps(value, ensure_ascii=False)


def find_hermes_hooks_key(lines: list[str]) -> int | None:
    for index, line in enumerate(lines):
        if re.match(r"^hooks:\s*(#.*)?$", line):
            return index
    return None


def hermes_hooks_region(lines: list[str]) -> tuple[int, int] | None:
    """Return the [start, end) line span nested under a top-level ``hooks:``."""
    header = find_hermes_hooks_key(lines)
    if header is None:
        return None

    end = header + 1
    for index in range(header + 1, len(lines)):
        line = lines[index]
        if line.strip() and not line.startswith((" ", "\t")):
            # Column-0 comments don't end the block, but only count toward the
            # region if indented content follows them.
            if line.startswith("#"):
                continue
            break
        end = index + 1
    return header + 1, end


def hermes_conflicting_events(text: str) -> tuple[str, ...]:
    region = hermes_hooks_region(text.splitlines())
    if region is None:
        return ()

    lines = text.splitlines()
    found: list[str] = []
    for line in lines[region[0]:region[1]]:
        match = re.match(r"^[ \t]{1,4}([A-Za-z_][A-Za-z0-9_]*):\s*(#.*)?$", line)
        if match and match.group(1) in HERMES_EVENTS:
            found.append(match.group(1))
    return tuple(sorted(set(found)))


def insert_hermes_hook_block(text: str, block: str) -> str:
    lines = text.splitlines(keepends=True)
    header = find_hermes_hooks_key([line.rstrip("\n") for line in lines])

    if header is None:
        return _ensure_trailing_newline(text) + "\nhooks:\n" + block

    if not lines[header].endswith("\n"):
        lines[header] += "\n"
    lines.insert(header + 1, block)
    return "".join(lines)


def remove_hermes_managed_block(text: str) -> str:
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    inside = False

    for line in lines:
        stripped = line.strip()
        if stripped == MANAGED_START:
            inside = True
            continue
        if stripped == MANAGED_END:
            inside = False
            continue
        if not inside:
            out.append(line)

    return "".join(out)


def drop_empty_hermes_hooks_key(text: str) -> str:
    lines = text.splitlines(keepends=True)
    region = hermes_hooks_region([line.rstrip("\n") for line in lines])
    if region is None:
        return text

    start, end = region
    if any(line.strip() and not line.strip().startswith("#") for line in lines[start:end]):
        return text

    header = start - 1
    # Also drop the blank separator line install_hermes_hooks added ahead of
    # the key, so uninstall is a byte-exact inverse of install.
    if header > 0 and not lines[header - 1].strip():
        header -= 1
    return "".join(lines[:header] + lines[end:])
