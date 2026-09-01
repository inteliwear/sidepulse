from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .models import provider_label

T3_PROCESS_TOKENS = (
    "t3 code.app",
    "t3-code.app",
    "t3 code (alpha).app",
    "t3-code-desktop",
    "t3code",
    "node_modules/.bin/t3",
)
T3_ENV_VALUE_MARKERS = (
    "t3 code.app",
    "t3-code.app",
    "t3 code (alpha).app",
    "t3code.app",
)


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    ppid: int | None
    comm: str
    command: str


@dataclass(frozen=True)
class AgentOrigin:
    kind: str
    label: str
    source: str
    confidence: str = "inferred"

    def to_payload(self) -> dict[str, str]:
        return {
            "agent_origin": self.label,
            "agent_origin_kind": self.kind,
            "agent_origin_source": self.source,
            "agent_origin_confidence": self.confidence,
        }


def detect_agent_origin(
    provider: str,
    *,
    env: Mapping[str, str] | None = None,
    parent_pid: int | None = None,
) -> AgentOrigin:
    active_env = os.environ if env is None else env
    normalized_provider = provider.lower()

    explicit = explicit_origin_from_env(normalized_provider, active_env)
    if explicit is not None:
        return explicit

    env_origin = origin_from_environment(normalized_provider, active_env)
    if env_origin is not None:
        # T3 Code is a VS Code fork and sets VSCODE_* variables. Confirm the
        # host isn't T3 before treating those as a standalone VS Code session.
        if env_origin.kind.endswith("_vscode"):
            processes = process_ancestry(parent_pid or os.getppid())
            if t3_code_host_in_processes(processes):
                return surface_origin(
                    normalized_provider, "t3code", "process:T3 Code"
                )
        return env_origin

    processes = process_ancestry(parent_pid or os.getppid())
    process_origin = origin_from_processes(normalized_provider, processes)
    if process_origin is not None:
        return process_origin

    if terminal_environment(active_env):
        return surface_origin(normalized_provider, "cli", "env:TERM_PROGRAM")

    return AgentOrigin(
        kind=f"{normalized_provider}_unknown",
        label=provider_label(normalized_provider),
        source="fallback:provider",
        confidence="unknown",
    )


def explicit_origin_from_env(provider: str, env: Mapping[str, str]) -> AgentOrigin | None:
    label = clean_label(env.get("SIDEPULSE_AGENT_ORIGIN"))
    if not label:
        return None
    kind = clean_label(env.get("SIDEPULSE_AGENT_ORIGIN_KIND")) or normalize_kind(label)
    return AgentOrigin(
        kind=kind,
        label=label,
        source="env:SIDEPULSE_AGENT_ORIGIN",
        confidence="explicit",
    )


def origin_from_environment(provider: str, env: Mapping[str, str]) -> AgentOrigin | None:
    if t3_code_environment(env):
        return surface_origin(provider, "t3code", "env:T3_CODE")

    term_program = str(env.get("TERM_PROGRAM") or "").strip().lower()
    if term_program == "vscode" or any(key.startswith("VSCODE_") for key in env):
        return surface_origin(provider, "vscode", "env:VSCODE")

    bundle_id = str(env.get("__CFBundleIdentifier") or "").strip().lower()
    if bundle_id:
        if provider == "codex" and any(part in bundle_id for part in ("openai", "chatgpt", "codex")):
            return surface_origin(provider, "app", "env:__CFBundleIdentifier")
        if provider == "claude" and "anthropic" in bundle_id:
            return surface_origin(provider, "app", "env:__CFBundleIdentifier")
        if provider == "grok" and "grok" in bundle_id:
            return surface_origin(provider, "app", "env:__CFBundleIdentifier")
        if provider == "opencode" and "opencode" in bundle_id:
            return surface_origin(provider, "app", "env:__CFBundleIdentifier")

    return None


def origin_from_processes(
    provider: str,
    processes: tuple[ProcessInfo, ...],
) -> AgentOrigin | None:
    haystack = "\n".join(f"{info.comm}\n{info.command}" for info in processes).lower()

    # T3 Code hosts provider CLIs (including Codex and OpenCode). Detect the
    # outer host first so a nested Codex process remains one status entry and
    # is presented as T3 Code instead of looking like a second standalone run.
    if t3_code_host_in_processes(processes):
        return surface_origin(provider, "t3code", "process:T3 Code")

    if any(token in haystack for token in ("visual studio code.app", "code helper", "vscode")):
        return surface_origin(provider, "vscode", "process:Visual Studio Code")
    if "cursor.app" in haystack or "cursor helper" in haystack:
        return surface_origin(provider, "cursor", "process:Cursor")
    if "windsurf.app" in haystack or "windsurf helper" in haystack:
        return surface_origin(provider, "windsurf", "process:Windsurf")

    if provider == "codex" and any(
        token in haystack for token in ("codex.app", "chatgpt.app")
    ):
        return surface_origin(provider, "app", "process:Codex.app")
    if provider == "claude" and "claude.app" in haystack:
        return surface_origin(provider, "app", "process:Claude.app")
    if provider == "grok" and "grok.app" in haystack:
        return surface_origin(provider, "app", "process:Grok.app")
    if provider == "opencode" and "opencode.app" in haystack:
        return surface_origin(provider, "app", "process:OpenCode.app")

    for info in processes:
        basename = process_basename(info)
        if provider == "codex" and basename == "codex":
            return surface_origin(provider, "cli", "process:codex")
        if provider == "claude" and basename in {"claude", "claude-code"}:
            return surface_origin(provider, "cli", "process:claude")
        if provider == "grok" and basename == "grok":
            return surface_origin(provider, "cli", "process:grok")
        if provider == "opencode" and basename == "opencode":
            return surface_origin(provider, "cli", "process:opencode")

    return None


def surface_origin(provider: str, surface: str, source: str) -> AgentOrigin:
    labels = {
        ("codex", "app"): "Codex UI",
        ("codex", "cli"): "Codex CLI",
        ("codex", "vscode"): "Codex in VS Code",
        ("codex", "cursor"): "Codex in Cursor",
        ("codex", "windsurf"): "Codex in Windsurf",
        ("codex", "t3code"): "T3 Code",
        ("codex", "transcript"): "Codex Transcript",
        ("claude", "app"): "Claude App",
        ("claude", "cli"): "Claude Code CLI",
        ("claude", "vscode"): "Claude in VS Code",
        ("claude", "cursor"): "Claude in Cursor",
        ("claude", "windsurf"): "Claude in Windsurf",
        ("claude", "t3code"): "T3 Code",
        ("claude", "transcript"): "Claude Transcript",
        ("grok", "app"): "Grok App",
        ("grok", "cli"): "Grok CLI",
        ("grok", "vscode"): "Grok in VS Code",
        ("grok", "cursor"): "Grok in Cursor",
        ("grok", "windsurf"): "Grok in Windsurf",
        ("grok", "t3code"): "T3 Code",
        ("grok", "transcript"): "Grok Transcript",
        ("opencode", "app"): "OpenCode",
        ("opencode", "cli"): "OpenCode CLI",
        ("opencode", "vscode"): "OpenCode in VS Code",
        ("opencode", "cursor"): "OpenCode in Cursor",
        ("opencode", "windsurf"): "OpenCode in Windsurf",
        ("opencode", "t3code"): "T3 Code",
        ("opencode", "transcript"): "OpenCode Transcript",
    }
    label = labels.get((provider, surface), provider_label(provider))
    return AgentOrigin(
        kind=f"{provider}_{surface}",
        label=label,
        source=source,
    )


def process_ancestry(pid: int, *, limit: int = 10) -> tuple[ProcessInfo, ...]:
    seen: set[int] = set()
    result: list[ProcessInfo] = []
    current = pid
    while current > 1 and current not in seen and len(result) < limit:
        seen.add(current)
        info = process_info(current)
        if info is None:
            break
        result.append(info)
        if info.ppid is None:
            break
        current = info.ppid
    return tuple(result)


def process_info(pid: int) -> ProcessInfo | None:
    try:
        completed = subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "pid=", "-o", "ppid=", "-o", "comm=", "-o", "command="],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=0.4,
        )
    except Exception:
        return None
    line = completed.stdout.strip()
    if completed.returncode != 0 or not line:
        return None

    parts = line.split(None, 3)
    if len(parts) < 3:
        return None
    try:
        parsed_pid = int(parts[0])
        parsed_ppid = int(parts[1])
    except ValueError:
        return None
    comm = parts[2]
    command = parts[3] if len(parts) > 3 else comm
    return ProcessInfo(parsed_pid, parsed_ppid, comm, command)


def process_basename(info: ProcessInfo) -> str:
    candidates = [info.comm]
    try:
        split = shlex.split(info.command)
    except ValueError:
        split = []
    if split:
        candidates.append(split[0])

    for value in candidates:
        name = Path(value).name.strip().lower()
        if name and name not in {"sh", "bash", "zsh", "python", "python3", "env"}:
            return name
    return ""


def terminal_environment(env: Mapping[str, str]) -> bool:
    term_program = str(env.get("TERM_PROGRAM") or "").strip()
    return bool(term_program and term_program.lower() != "vscode")


def t3_code_host_in_processes(processes: tuple[ProcessInfo, ...]) -> bool:
    haystack = "\n".join(f"{info.comm}\n{info.command}" for info in processes).lower()
    return any(token in haystack for token in T3_PROCESS_TOKENS)


def t3_code_environment(env: Mapping[str, str]) -> bool:
    """Recognize explicit environment markers used by T3 Code launches."""
    for key in (
        "T3_CODE",
        "T3CODE",
        "T3_CODE_HOME",
        "T3CODE_HOME",
        "T3_MCP_BEARER_TOKEN",
    ):
        if str(env.get(key) or "").strip():
            return True

    bundle_id = str(env.get("__CFBundleIdentifier") or "").strip().lower()
    if any(marker in bundle_id.replace("_", " ") for marker in ("t3 code", "t3-code", "t3code")):
        return True

    term_program = str(env.get("TERM_PROGRAM") or "").strip().lower()
    if any(marker in term_program.replace("_", " ") for marker in ("t3 code", "t3-code", "t3code")):
        return True

    for key, value in env.items():
        if not str(key).startswith("VSCODE_"):
            continue
        lowered = str(value or "").lower()
        if any(marker in lowered for marker in T3_ENV_VALUE_MARKERS):
            return True
    return False


def annotate_payload_with_origin(
    provider: str,
    payload: dict[str, Any],
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    active_env = os.environ if env is None else env
    result = dict(payload)
    if "agent_origin" not in payload and "agentOrigin" not in payload:
        result.update(detect_agent_origin(provider, env=active_env).to_payload())
    operation = clean_label(active_env.get("T3CODE_TEXT_GENERATION_OPERATION"))
    if operation:
        result["agent_internal_operation"] = operation
    return result


def origin_label_from_payload(provider: str, raw: Mapping[str, Any]) -> str | None:
    for key in ("agent_origin", "agentOrigin", "agent_origin_label", "origin_label"):
        label = clean_label(raw.get(key))
        if label:
            return label

    structured = raw.get("sidepulse_origin") or raw.get("sidepulseOrigin")
    if isinstance(structured, Mapping):
        for key in ("label", "name", "origin"):
            label = clean_label(structured.get(key))
            if label:
                return label
    elif isinstance(structured, str):
        label = clean_label(structured)
        if label:
            return label

    source = clean_label(raw.get("source"))
    if source in {"codex-transcripts", "claude-transcripts"}:
        return surface_origin(provider, "transcript", f"source:{source}").label

    return None


def clean_label(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text or None


def normalize_kind(label: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_")
    return normalized or "custom"
