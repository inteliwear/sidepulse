from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, TextIO

from .hook import write_hook_line
from .ipc import send_hook_event
from .models import provider_label
from .providers import HOOK_PROVIDERS, default_state_dir, detect_log_path


REMOTE_CONFIG_VERSION = 1
DEFAULT_REMOTE_PROVIDERS = ("codex", "claude")
REMOTE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class RemoteHost:
    name: str
    ssh_target: str
    providers: tuple[str, ...] = DEFAULT_REMOTE_PROVIDERS

    def __post_init__(self) -> None:
        if not REMOTE_NAME_PATTERN.fullmatch(self.name):
            raise ValueError(
                "Remote host name must start with a letter or number and contain "
                "only letters, numbers, dots, underscores, or dashes."
            )
        if (
            not self.ssh_target.strip()
            or self.ssh_target != self.ssh_target.strip()
            or self.ssh_target.startswith("-")
            or any(char in self.ssh_target for char in "\r\n\0")
        ):
            raise ValueError("SSH target must be a non-empty host or SSH config alias.")
        if not self.providers:
            raise ValueError("At least one remote provider is required.")
        invalid = tuple(provider for provider in self.providers if provider not in HOOK_PROVIDERS)
        if invalid:
            raise ValueError(f"Unsupported remote provider: {invalid[0]}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ssh_target": self.ssh_target,
            "providers": list(self.providers),
        }


def default_remote_config_path(home: Path | None = None) -> Path:
    base = home or Path.home()
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", base / ".config")).expanduser()
    return config_home / "sidepulse" / "remote-hosts.json"


def load_remote_hosts(path: Path | None = None) -> tuple[RemoteHost, ...]:
    target = (path or default_remote_config_path()).expanduser()
    if not target.exists():
        return ()
    try:
        data = json.loads(target.read_text())
    except (OSError, json.JSONDecodeError):
        return ()
    if not isinstance(data, dict) or not isinstance(data.get("hosts"), list):
        return ()

    hosts: list[RemoteHost] = []
    for item in data["hosts"]:
        if not isinstance(item, dict):
            continue
        providers = item.get("providers", DEFAULT_REMOTE_PROVIDERS)
        if not isinstance(providers, list) or not all(isinstance(value, str) for value in providers):
            continue
        try:
            host = RemoteHost(
                name=str(item.get("name") or ""),
                ssh_target=str(item.get("ssh_target") or ""),
                providers=tuple(dict.fromkeys(providers)),
            )
        except ValueError:
            continue
        hosts.append(host)
    return tuple(hosts)


def save_remote_hosts(
    hosts: Iterable[RemoteHost],
    path: Path | None = None,
) -> Path:
    target = (path or default_remote_config_path()).expanduser()
    ordered = sorted(hosts, key=lambda host: host.name.lower())
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(f"{target.suffix}.tmp")
    temporary.write_text(
        json.dumps(
            {"version": REMOTE_CONFIG_VERSION, "hosts": [host.to_dict() for host in ordered]},
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    temporary.replace(target)
    return target


def upsert_remote_host(host: RemoteHost, path: Path | None = None) -> Path:
    hosts = {existing.name: existing for existing in load_remote_hosts(path)}
    hosts[host.name] = host
    return save_remote_hosts(hosts.values(), path)


def remove_remote_host(name: str, path: Path | None = None) -> tuple[Path, bool]:
    target = (path or default_remote_config_path()).expanduser()
    hosts = list(load_remote_hosts(target))
    remaining = [host for host in hosts if host.name != name]
    changed = len(remaining) != len(hosts)
    if changed:
        save_remote_hosts(remaining, target)
    return target, changed


def remote_state_dir(home: Path | None = None) -> Path:
    return default_state_dir(home) / "remote"


def remote_log_path(host_name: str, provider: str, home: Path | None = None) -> Path:
    return remote_state_dir(home) / host_name / f"{provider}.jsonl"


def configured_remote_logs(
    *,
    config_path: Path | None = None,
    home: Path | None = None,
) -> tuple[tuple[str, Path], ...]:
    return tuple(
        (provider, remote_log_path(host.name, provider, home))
        for host in load_remote_hosts(config_path)
        for provider in host.providers
    )


def qualify_remote_line(provider: str, line: dict[str, Any], host_name: str) -> dict[str, Any]:
    qualified = dict(line)
    if provider == "codex" and isinstance(line.get("event"), dict):
        payload = dict(line["event"])
        qualified["event"] = payload
    else:
        payload = qualified

    prefix = f"remote:{host_name}:"
    for snake, camel in (
        ("session_id", "sessionId"),
        ("turn_id", "turnId"),
        ("agent_id", "agentId"),
    ):
        for key in (snake, camel):
            value = payload.get(key)
            if isinstance(value, str) and value and not value.startswith(prefix):
                payload[key] = f"{prefix}{value}"

    payload["agent_origin"] = f"{provider_label(provider)} on {host_name}"
    payload["agent_origin_kind"] = f"{provider}_ssh"
    payload["agent_origin_source"] = "sidepulse:remote-host"
    payload["agent_origin_confidence"] = "explicit"
    payload["sidepulse_remote_host"] = host_name
    return qualified


def ssh_stream_command(host: RemoteHost, *, replay_lines: int = 300) -> list[str]:
    providers = " ".join(
        f"--provider {shlex.quote(provider)}" for provider in host.providers
    )
    remote = (
        "exec ~/.local/bin/sidepulse remote-agent stream "
        f"--replay-lines {max(0, replay_lines)} {providers}"
    ).strip()
    return [
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        host.ssh_target,
        f"zsh -lic {shlex.quote(remote)}",
    ]


def consume_remote_envelope(host: RemoteHost, text: str) -> bool:
    try:
        envelope = json.loads(text)
    except json.JSONDecodeError:
        return False
    if not isinstance(envelope, dict):
        return False
    provider = envelope.get("provider")
    line = envelope.get("line")
    if provider not in host.providers or not isinstance(line, dict):
        return False

    qualified = qualify_remote_line(provider, line, host.name)
    send_hook_event(provider, qualified)
    write_hook_line(remote_log_path(host.name, provider), qualified)
    return True


def monitor_remote_host(
    host: RemoteHost,
    *,
    stop_event: threading.Event,
    replay_lines: int = 300,
) -> None:
    delay = 1.0
    while not stop_event.is_set():
        try:
            process = subprocess.Popen(
                ssh_stream_command(host, replay_lines=replay_lines),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            if process.stdout is not None:
                for line in process.stdout:
                    if stop_event.is_set():
                        break
                    if consume_remote_envelope(host, line):
                        delay = 1.0
            if stop_event.is_set() and process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
        except OSError:
            pass
        if stop_event.wait(delay):
            return
        delay = min(delay * 2, 30.0)


def run_remote_monitor(hosts: Iterable[RemoteHost] | None = None) -> int:
    active_hosts = tuple(load_remote_hosts() if hosts is None else hosts)
    if not active_hosts:
        return 0

    stop_event = threading.Event()
    threads = [
        threading.Thread(
            target=monitor_remote_host,
            kwargs={"host": host, "stop_event": stop_event},
            name=f"sidepulse-remote-{host.name}",
            daemon=True,
        )
        for host in active_hosts
    ]
    for thread in threads:
        thread.start()
    try:
        while any(thread.is_alive() for thread in threads):
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        for thread in threads:
            thread.join(timeout=3)
    return 0


def _emit_envelope(provider: str, line: str, output: TextIO) -> None:
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError:
        return
    if not isinstance(parsed, dict):
        return
    output.write(
        json.dumps(
            {"provider": provider, "line": parsed},
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n"
    )
    output.flush()


def stream_remote_events(
    providers: Iterable[str] = DEFAULT_REMOTE_PROVIDERS,
    *,
    replay_lines: int = 300,
    poll_interval: float = 0.25,
    output: TextIO | None = None,
) -> int:
    output = output or sys.stdout
    selected = tuple(dict.fromkeys(providers))
    paths = {provider: detect_log_path(provider) for provider in selected}
    offsets: dict[str, int] = {}

    for provider, path in paths.items():
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                recent: deque[str] = deque(maxlen=max(0, replay_lines))
                while True:
                    line = handle.readline()
                    if not line:
                        break
                    recent.append(line)
                offsets[provider] = handle.tell()
            for line in recent:
                _emit_envelope(provider, line, output)
        except BrokenPipeError:
            return 0
        except OSError:
            offsets[provider] = 0

    try:
        while True:
            for provider, path in paths.items():
                try:
                    size = path.stat().st_size
                    offset = offsets.get(provider, 0)
                    if size < offset:
                        offset = 0
                    if size == offset:
                        continue
                    with path.open("r", encoding="utf-8", errors="replace") as handle:
                        handle.seek(offset)
                        while True:
                            line = handle.readline()
                            if not line:
                                break
                            _emit_envelope(provider, line, output)
                        offsets[provider] = handle.tell()
                except BrokenPipeError:
                    return 0
                except OSError:
                    continue
            time.sleep(max(0.05, poll_interval))
    except (BrokenPipeError, KeyboardInterrupt):
        return 0
