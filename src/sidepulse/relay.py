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
from .settings import default_config_dir


RELAY_CONFIG_VERSION = 1
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


def listen_for_status_updates(
    config: RelayConfig,
    stop: threading.Event,
    on_update: Callable[[dict], None],
) -> None:
    """Receive only normalized OSC-style updates using the existing bridge SSE."""
    channel = urllib.parse.quote(clean_channel(config.receiver_channel, required=True), safe="")
    url = f"{normalize_server(config.server)}/api/leds/{channel}"
    while not stop.is_set():
        request = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
        try:
            with urllib.request.urlopen(request, timeout=30.0) as response:
                for value in iter_sse_messages(response):
                    if stop.is_set():
                        return
                    try:
                        message = json.loads(value)
                    except (ValueError, TypeError):
                        continue
                    if not isinstance(message, dict) or message.get('type') != 'program_status':
                        continue
                    try:
                        on_update(message)
                    except (ValueError, TypeError):
                        continue
                    if stop.is_set():
                        return
        except (OSError, urllib.error.URLError):
            stop.wait(1.0)
