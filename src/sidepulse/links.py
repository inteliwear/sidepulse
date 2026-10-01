from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .settings import default_config_dir


DEFAULT_BRIDGE_SERVER = "https://bridge.sidepulse.io"
PAIRING_TIMEOUT_SECONDS = 5 * 60
IOS_BUNDLE_ID = "io.sidepulse.ios"
ANDROID_PACKAGE_ID = "io.sidepulse.android"
APNS_TOKEN_PATTERN = re.compile(r"(?:dev_)?[0-9a-f]{64}(?:_[A-Za-z0-9_-]{1,128})?")
PAIRING_CHANNEL_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")


class LinkError(RuntimeError):
    pass


@dataclass(frozen=True)
class PhoneLink:
    name: str
    token: str
    server: str = DEFAULT_BRIDGE_SERVER
    linked_at: str = ""

    @property
    def platform(self) -> str:
        return "android" if self.token.startswith("fcm_") else "ios"

    @property
    def device_token(self) -> str:
        if self.platform == "android":
            return self.token[:-33]
        return self.token[:68] if self.token.startswith("dev_") else self.token[:64]

    @property
    def link_id(self) -> str:
        if self.platform == "android":
            return hashlib.sha256(self.device_token.encode("ascii")).hexdigest()[:12]
        return self.token[:12]

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.link_id,
            "name": self.name,
            "token": self.token,
            "server": self.server,
            "linked_at": self.linked_at,
        }


# Preserve the existing public API and integrations while supporting both phones.
IOSLink = PhoneLink


def default_links_path() -> Path:
    return default_config_dir() / "links.json"


def bridge_server() -> str:
    return normalize_server(os.environ.get("SIDEPULSE_SERVER", DEFAULT_BRIDGE_SERVER))


def normalize_server(value: str) -> str:
    text = value.strip().rstrip("/")
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise LinkError("Bridge server must be an HTTP or HTTPS origin.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise LinkError("Bridge server must not contain credentials, a query, or a fragment.")
    if parsed.path not in {"", "/"}:
        raise LinkError("Bridge server must be an origin without a path.")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def normalize_apns_token(value: str) -> str:
    text = value.strip()
    prefix = "dev_" if text[:4].lower() == "dev_" else ""
    raw = text[4:] if prefix else text
    device, separator, key = raw.partition("_")
    # Device-token hex is case-insensitive; shared keys are case-sensitive.
    token = prefix + "".join(device.split()).lower() + (separator + key if separator else "")
    if not APNS_TOKEN_PATTERN.fullmatch(token):
        raise LinkError(
            "Push token must be 64 hexadecimal characters, optionally prefixed with 'dev_' "
            "and suffixed with '_<shared-key>' (1–128 letters, digits, underscores, or hyphens)."
        )
    return token


def normalize_fcm_token(value: str) -> str:
    """Keep the opaque registration unchanged; only the final suffix is a key."""
    token = value.strip()
    if not token.startswith("fcm_") or not re.fullmatch(r"_[0-9a-f]{32}", token[-33:]):
        raise LinkError("Android push token must start with 'fcm_' and end with a 32-character sender key.")
    registration = token[4:-33]
    if not 1 <= len(registration) <= 4096 or any(
        not 33 <= ord(char) <= 126 or char in "/\\?#%" for char in registration
    ):
        raise LinkError("Android push token contains an invalid registration token.")
    return token


def normalize_phone_token(value: str) -> str:
    return normalize_fcm_token(value) if value.strip().startswith("fcm_") else normalize_apns_token(value)


def load_ios_links(path: Path | None = None) -> tuple[IOSLink, ...]:
    """Load all phones, including legacy version-1 iOS entries."""
    target = (path or default_links_path()).expanduser()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ()
    except Exception:
        return ()
    if not isinstance(data, dict) or data.get("version") != 1:
        return ()
    links: list[IOSLink] = []
    for platform, normalizer, default_name in (
        ("ios", normalize_apns_token, "iPhone"),
        ("android", normalize_fcm_token, "Android phone"),
    ):
        items = data.get(platform, [])
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                links.append(
                    PhoneLink(
                        name=str(item.get("name") or default_name),
                        token=normalizer(str(item["token"])),
                        server=normalize_server(str(item.get("server") or DEFAULT_BRIDGE_SERVER)),
                        linked_at=str(item.get("linked_at") or ""),
                    )
                )
            except (KeyError, LinkError):
                continue
    return tuple(links)


def save_ios_links(links: Iterable[IOSLink], path: Path | None = None) -> Path:
    target = (path or default_links_path()).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.parent.chmod(0o700)
    except OSError:
        pass
    phones = tuple(links)
    payload = {
        "version": 1,
        "ios": [link.to_dict() for link in phones if link.platform == "ios"],
        "android": [link.to_dict() for link in phones if link.platform == "android"],
    }
    temporary = target.with_suffix(target.suffix + ".tmp")
    # Create privately before writing credentials, including under a permissive umask.
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temporary.replace(target)
    target.chmod(0o600)
    return target


def store_ios_link(link: IOSLink, path: Path | None = None) -> tuple[IOSLink, ...]:
    current = list(load_ios_links(path))
    updated: list[IOSLink] = []
    replaced = False
    for existing in current:
        if (existing.platform, existing.device_token) == (link.platform, link.device_token):
            updated.append(link)
            replaced = True
        else:
            updated.append(existing)
    if not replaced:
        updated.append(link)
    save_ios_links(updated, path)
    return tuple(updated)


def remove_ios_link(token: str, path: Path | None = None) -> IOSLink | None:
    """Remove the phone matching *token* and return the removed link."""
    current = load_ios_links(path)
    removed = next((link for link in current if link.token == token), None)
    if removed is None:
        return None
    save_ios_links((link for link in current if link.token != token), path)
    return removed


def new_pairing_channel() -> str:
    return secrets.token_urlsafe(8)


def pairing_url(server: str, channel: str, sender: str | None = None) -> str:
    normalized_server = normalize_server(server)
    if not PAIRING_CHANNEL_PATTERN.fullmatch(channel):
        raise LinkError("Pairing channel must be an 11-character Base64URL token.")
    if normalized_server == DEFAULT_BRIDGE_SERVER:
        return f"sidepulse://p/{channel}"
    query = {
        "v": "1",
        "server": normalized_server,
        "channel": channel,
    }
    clean_sender = (sender or socket.gethostname()).strip()[:80]
    if clean_sender:
        query["sender"] = clean_sender
    return "sidepulse://pair?" + urllib.parse.urlencode(query)


def _qr_matrix(value: str, *, border: int) -> list[list[bool]]:
    try:
        import qrcode
        from qrcode.constants import ERROR_CORRECT_M
    except ImportError as exc:  # pragma: no cover - packaging installs it.
        raise LinkError("QR support is unavailable; reinstall SidePulse and try again.") from exc

    code = qrcode.QRCode(version=None, error_correction=ERROR_CORRECT_M, box_size=1, border=border)
    code.add_data(value)
    code.make(fit=True)
    return code.get_matrix()


def render_terminal_qr(value: str, *, ansi: bool = False) -> str:
    matrix = _qr_matrix(value, border=2)
    pixels = {False: "  ", True: "██"}
    ansi_pixels = {False: "\033[107m", True: "\033[40m"}
    lines: list[str] = []
    for row in matrix:
        if not ansi:
            lines.append("".join(pixels[cell] for cell in row))
            continue

        parts: list[str] = []
        active_style = ""
        for cell in row:
            style = ansi_pixels[cell]
            if style != active_style:
                parts.append(style)
                active_style = style
            parts.append("  ")
        parts.append("\033[0m")
        lines.append("".join(parts))
    return "\n".join(lines)


def parse_ios_registration(value: str, *, server: str) -> IOSLink:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise LinkError("Pairing response was not valid JSON.") from exc
    if not isinstance(payload, dict) or payload.get("v") != 1 or payload.get("type") != "ios_registration":
        raise LinkError("Pairing response has an unsupported format.")
    device = payload.get("device")
    if not isinstance(device, dict):
        raise LinkError("Pairing response did not include a device.")
    bundle_id = str(device.get("bundle_id") or "")
    if bundle_id != IOS_BUNDLE_ID:
        raise LinkError(f"The phone uses unsupported bundle ID {bundle_id or '(missing)' }.")
    name = str(device.get("name") or "iPhone").strip()[:80] or "iPhone"
    token = normalize_apns_token(str(device.get("push_token") or ""))
    return IOSLink(
        name=name,
        token=token,
        server=normalize_server(server),
        linked_at=datetime.now(timezone.utc).isoformat(),
    )


def iter_sse_messages(response) -> Iterable[str]:
    data_lines: list[str] = []
    for raw_line in response:
        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            if data_lines:
                yield "\n".join(data_lines)
                data_lines = []
            continue
        if line.startswith("data:"):
            value = line[5:]
            if value.startswith(" "):
                value = value[1:]
            data_lines.append(value)
    if data_lines:
        yield "\n".join(data_lines)


def parse_phone_registration(value: str, *, server: str) -> PhoneLink:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise LinkError("Pairing response was not valid JSON.") from exc
    if isinstance(payload, dict) and payload.get("type") == "ios_registration":
        return parse_ios_registration(value, server=server)
    if not isinstance(payload, dict) or payload.get("v") != 1 or payload.get("type") != "android_registration":
        raise LinkError("Pairing response has an unsupported format.")
    device = payload.get("device")
    if not isinstance(device, dict) or device.get("platform") != "android" or device.get("package_id") != ANDROID_PACKAGE_ID:
        raise LinkError("Pairing response did not include a supported SidePulse Android device.")
    return PhoneLink(
        name=str(device.get("name") or "Android phone").strip()[:80] or "Android phone",
        token=normalize_fcm_token(str(device.get("push_token") or "")),
        server=normalize_server(server),
        linked_at=datetime.now(timezone.utc).isoformat(),
    )


def listen_for_ios_registration(
    server: str,
    channel: str,
    results: queue.Queue[IOSLink],
    stop: threading.Event,
    *,
    deadline: float,
) -> None:
    url = f"{normalize_server(server)}/api/leds/{urllib.parse.quote(channel, safe='')}"
    while not stop.is_set() and time.monotonic() < deadline:
        remaining = max(1.0, deadline - time.monotonic())
        request = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
        try:
            with urllib.request.urlopen(request, timeout=min(20.0, remaining)) as response:
                for message in iter_sse_messages(response):
                    if stop.is_set():
                        return
                    try:
                        link = parse_phone_registration(message, server=server)
                    except LinkError:
                        continue
                    results.put(link)
                    return
        except (OSError, urllib.error.URLError):
            if not stop.wait(0.5):
                continue


def send_ios_program(
    link: IOSLink,
    program: str | None = None,
    *,
    event_id: str | None = None,
    title: str | None = None,
    message: str | None = None,
    data: dict[str, object] | None = None,
) -> str:
    clean_title = title.strip() if title else ""
    clean_message = message.strip() if message else ""
    if program is None and not clean_title and not clean_message:
        raise LinkError("A remote write must contain LEDs, a title, or a message.")

    custom_data = dict(data or {})
    custom_data["sidepulse_event_id"] = event_id or str(uuid.uuid4())
    aps: dict[str, object] = {"content-available": 1}
    if clean_title or clean_message:
        alert: dict[str, str] = {}
        if clean_title:
            alert["title"] = clean_title
        if clean_message:
            alert["body"] = clean_message
        aps["alert"] = alert

    payload: dict[str, object] = {"data": custom_data}
    if link.platform == "android":
        payload["sidepulse_event_id"] = custom_data["sidepulse_event_id"]
        channel = normalize_fcm_token(link.token)
    else:
        payload["aps"] = aps
        channel = "apns_" + normalize_apns_token(link.token)
    if program is not None:
        payload["leds"] = program
    if clean_title:
        payload["title"] = clean_title
    if clean_message:
        payload["body"] = clean_message
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    url = f"{normalize_server(link.server)}/api/leds/{urllib.parse.quote(channel, safe='')}"
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10.0) as response:
            return response.read().decode("utf-8", errors="replace").strip()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        for secret in (url, urllib.parse.quote(channel, safe=""), channel, link.token,
                       link.device_token, link.token.rsplit("_", 1)[-1]):
            detail = detail.replace(secret, "[redacted]")
        raise LinkError(detail or f"Bridge returned HTTP {exc.code}.") from exc
    except (OSError, urllib.error.URLError) as exc:
        raise LinkError("Could not reach the bridge. Check the connection and bridge server.") from exc


# Platform-neutral names for new callers; old names remain compatible.
load_phone_links = load_ios_links
save_phone_links = save_ios_links
store_phone_link = store_ios_link
remove_phone_link = remove_ios_link
listen_for_phone_registration = listen_for_ios_registration
send_phone_program = send_ios_program
