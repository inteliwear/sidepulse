from __future__ import annotations

import json
import hashlib
import os
import secrets
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from .links import (
    DEFAULT_BRIDGE_SERVER,
    LinkError,
    bridge_server,
    iter_sse_messages,
    normalize_server,
)
from .providers import HOOK_PROVIDERS, candidate_state_dirs, default_state_dir
from .settings import default_config_dir


RELAY_CONFIG_VERSION = 1
RELAY_MESSAGE_VERSION = 1
RELAY_CHANNEL_BYTES = 16


@dataclass(frozen=True)
class RelayConfig:
    server: str
    receiver_channel: str = ""
    outbound_channel: str = ""
    machine_name: str = ""

    @property
    def receives_events(self) -> bool:
        return bool(self.receiver_channel)

    @property
    def publishes_events(self) -> bool:
        return bool(self.outbound_channel)


def default_relay_config_path() -> Path:
    return default_config_dir() / "relay.json"


def default_relay_event_socket_path() -> Path:
    return relay_socket_path_for_state_dir(default_state_dir())


def candidate_relay_event_socket_paths() -> tuple[Path, ...]:
    return tuple(relay_socket_path_for_state_dir(path) for path in candidate_state_dirs())


def relay_socket_path_for_state_dir(state_dir: Path) -> Path:
    path = state_dir / "relay-events.sock"
    if len(str(path).encode("utf-8")) <= 96:
        return path
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]
    return Path("/tmp") / f"sidepulse-relay-{os.getuid()}-{digest}.sock"


def new_relay_channel() -> str:
    return secrets.token_urlsafe(RELAY_CHANNEL_BYTES)


def load_relay_config(path: Path | None = None) -> RelayConfig:
    target = (path or default_relay_config_path()).expanduser()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return RelayConfig(server=bridge_server(), machine_name=socket.gethostname())
    if not isinstance(data, dict) or data.get("version") != RELAY_CONFIG_VERSION:
        return RelayConfig(server=bridge_server(), machine_name=socket.gethostname())
    try:
        server = normalize_server(str(data.get("server") or bridge_server()))
    except LinkError:
        server = bridge_server()
    return RelayConfig(
        server=server,
        receiver_channel=clean_channel(data.get("receiver_channel")),
        outbound_channel=clean_channel(data.get("outbound_channel")),
        machine_name=str(data.get("machine_name") or socket.gethostname()).strip()[:80],
    )


def save_relay_config(config: RelayConfig, path: Path | None = None) -> Path:
    target = (path or default_relay_config_path()).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": RELAY_CONFIG_VERSION,
        "server": normalize_server(config.server),
        "receiver_channel": clean_channel(config.receiver_channel),
        "outbound_channel": clean_channel(config.outbound_channel),
        "machine_name": config.machine_name.strip()[:80] or socket.gethostname(),
    }
    temp = target.with_suffix(".json.tmp")
    temp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temp, 0o600)
    temp.replace(target)
    return target


def ensure_receiver_config(path: Path | None = None) -> RelayConfig:
    config = load_relay_config(path)
    if config.receiver_channel:
        return config
    updated = replace(config, receiver_channel=new_relay_channel())
    save_relay_config(updated, path)
    return updated


def configure_outbound_channel(
    channel: str,
    *,
    server: str | None = None,
    path: Path | None = None,
) -> RelayConfig:
    config = load_relay_config(path)
    updated = replace(
        config,
        server=normalize_server(server or config.server),
        outbound_channel=clean_channel(channel, required=True),
    )
    save_relay_config(updated, path)
    return updated


def relay_link_command(config: RelayConfig) -> str:
    command = f"sidepulse link {config.receiver_channel}"
    if normalize_server(config.server) != normalize_server(DEFAULT_BRIDGE_SERVER):
        return f"SIDEPULSE_SERVER={config.server} {command}"
    return command


def clean_channel(value: object, *, required: bool = False) -> str:
    channel = str(value or "").strip()
    if channel and (
        len(channel) < 11
        or len(channel) > 128
        or any(
            char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
            for char in channel
        )
    ):
        raise LinkError("Relay code must be 11-128 Base64URL characters.")
    if required and not channel:
        raise LinkError("Relay code is required.")
    return channel


def relay_event_message(provider: str, line: dict) -> dict[str, object]:
    return {
        "v": RELAY_MESSAGE_VERSION,
        "type": "agent_event",
        "event_id": str(uuid.uuid4()),
        "source": {"name": socket.gethostname()},
        "provider": provider,
        "line": line,
    }


def parse_relay_event(value: str) -> tuple[str, dict, str] | None:
    try:
        message = json.loads(value)
    except json.JSONDecodeError:
        return None
    if not isinstance(message, dict):
        return None
    if message.get("v") != RELAY_MESSAGE_VERSION or message.get("type") != "agent_event":
        return None
    provider = message.get("provider")
    line = message.get("line")
    event_id = message.get("event_id")
    source = message.get("source")
    if (
        provider not in HOOK_PROVIDERS
        or not isinstance(line, dict)
        or not isinstance(event_id, str)
    ):
        return None
    source_name = "Remote computer"
    if isinstance(source, dict):
        source_name = str(source.get("name") or source_name).strip()[:80] or source_name
    return provider, annotate_remote_line(provider, line, source_name), event_id


def annotate_remote_line(provider: str, line: dict, source_name: str) -> dict:
    annotated = dict(line)
    if provider == "codex" and isinstance(line.get("event"), dict):
        raw = dict(line["event"])
        annotated["event"] = raw
    else:
        raw = annotated
    raw["sidepulse_relay_source"] = source_name
    origin = str(raw.get("agent_origin") or raw.get("agentOrigin") or "").strip()
    raw["agent_origin"] = f"{origin} · {source_name}" if origin else source_name
    identity = str(
        raw.get("agent_id")
        or raw.get("agentId")
        or raw.get("session_id")
        or raw.get("sessionId")
        or "default"
    )
    raw["agent_id"] = f"relay:{source_name}:{identity}"
    return annotated


def publish_relay_event(config: RelayConfig, provider: str, line: dict) -> None:
    if not config.outbound_channel:
        return
    body = json.dumps(
        relay_event_message(provider, line),
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    channel = urllib.parse.quote(clean_channel(config.outbound_channel, required=True), safe="")
    request = urllib.request.Request(
        f"{normalize_server(config.server)}/api/leds/{channel}",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5.0):
            return
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise LinkError(detail or f"Bridge returned HTTP {exc.code}.") from exc
    except (OSError, urllib.error.URLError) as exc:
        raise LinkError(f"Could not reach the bridge: {exc}") from exc


def listen_for_relay_events(
    config: RelayConfig,
    stop: threading.Event,
    on_event: Callable[[str, dict, str], None],
) -> None:
    channel = urllib.parse.quote(clean_channel(config.receiver_channel, required=True), safe="")
    url = f"{normalize_server(config.server)}/api/leds/{channel}"
    while not stop.is_set():
        request = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
        try:
            with urllib.request.urlopen(request, timeout=30.0) as response:
                for value in iter_sse_messages(response):
                    if stop.is_set():
                        return
                    parsed = parse_relay_event(value)
                    if parsed is not None:
                        on_event(*parsed)
        except (OSError, urllib.error.URLError):
            stop.wait(1.0)
