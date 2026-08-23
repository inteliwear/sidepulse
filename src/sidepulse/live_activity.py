"""Mirror agent statuses to an iOS Live Activity and an in-app live stream.

The daemon has two delivery paths that share one snapshot loop:

- An HTTP server (``/register``, ``/snapshot``, ``/stream``, ``/health``).
  ``/stream`` is Server-Sent Events and feeds the iOS app's realtime view
  over LAN or Tailscale while the app is in the foreground.
- APNs ``liveactivity`` pushes keep a Lock Screen / Dynamic Island Live
  Activity current while the phone is locked. With an iOS 17.2+
  push-to-start token the daemon also *starts* the activity whenever
  agents become active, and ends it when the host goes idle.

APNs needs ``httpx[http2]`` and ``cryptography`` (the ``live-activity``
extra); the HTTP server and SSE stream are stdlib-only.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .collector import AgentMonitor
from .models import MODE_PRIORITY, AgentStatus
from .providers import default_state_dir

MAX_AGENT_ROWS = 6
MAX_NAME_CHARS = 44
MAX_DETAIL_CHARS = 32
PUSH_MIN_INTERVAL_SECONDS = 3.0
PUSH_HEARTBEAT_SECONDS = 300.0
PUSH_TO_START_COOLDOWN_SECONDS = 300.0
SSE_HEARTBEAT_SECONDS = 10.0
ATTRIBUTES_TYPE = "AgentActivityAttributes"


@dataclass(frozen=True)
class LiveActivityConfig:
    apns_key_path: Path
    apns_key_id: str
    apns_team_id: str
    bundle_id: str = "io.sidepulse.app"
    apns_environment: str = "production"
    host_label: str = field(default_factory=lambda: socket.gethostname().split(".")[0])
    port: int = 8787
    poll_seconds: float = 2.0
    idle_end_minutes: float = 10.0

    @property
    def apns_host(self) -> str:
        if self.apns_environment.lower() in {"prod", "production"}:
            return "api.push.apple.com"
        return "api.sandbox.push.apple.com"

    @property
    def liveactivity_topic(self) -> str:
        return f"{self.bundle_id}.push-type.liveactivity"


def default_token_store_path() -> Path:
    return default_state_dir() / "live_activity_tokens.json"


class TokenStore:
    """Registered APNs tokens, persisted so restarts keep the phone linked."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_token_store_path()
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, dict[str, Any]]] = {
            "push_to_start": {},
            "update": {},
        }
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        for kind in ("push_to_start", "update"):
            entries = raw.get(kind)
            if isinstance(entries, dict):
                self._data[kind] = {
                    str(token): dict(meta)
                    for token, meta in entries.items()
                    if isinstance(meta, dict)
                }

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._data, indent=2, sort_keys=True))
        except OSError:
            pass

    def register(self, kind: str, token: str, meta: dict[str, Any]) -> None:
        with self._lock:
            meta = dict(meta)
            meta["registered_at"] = datetime.now(timezone.utc).isoformat()
            self._data[kind][token] = meta
            self._save()

    def tokens(self, kind: str) -> list[str]:
        with self._lock:
            return list(self._data[kind])

    def drop(self, kind: str, token: str) -> None:
        with self._lock:
            if self._data[kind].pop(token, None) is not None:
                self._save()

    def clear(self, kind: str) -> None:
        with self._lock:
            if self._data[kind]:
                self._data[kind] = {}
                self._save()

    def summary(self) -> dict[str, int]:
        with self._lock:
            return {kind: len(entries) for kind, entries in self._data.items()}


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def build_content_state(statuses: list[AgentStatus], aggregate_mode: str) -> dict[str, Any]:
    """Wire format shared with AgentActivityAttributes.ContentState."""
    ordered = sorted(
        statuses,
        key=lambda status: (MODE_PRIORITY.get(status.mode, 99), -status.updated_at.timestamp()),
    )
    agents = [
        {
            "id": status.agent_id,
            "name": _truncate(status.display_name, MAX_NAME_CHARS),
            "mode": status.mode.value,
            "detail": _truncate(status.tool_name, MAX_DETAIL_CHARS) if status.tool_name else None,
        }
        for status in ordered[:MAX_AGENT_ROWS]
    ]
    return {
        "aggregateMode": aggregate_mode,
        "activeCount": len(statuses),
        "agents": agents,
        "updatedAt": round(time.time(), 1),
    }


class APNsLiveActivityClient:
    """Minimal APNs client for liveactivity pushes (JWT auth, HTTP/2)."""

    def __init__(self, config: LiveActivityConfig) -> None:
        self.config = config
        self._jwt: str | None = None
        self._jwt_issued_at = 0.0
        self._client = None

    def _token(self) -> str:
        now = time.time()
        if self._jwt and now - self._jwt_issued_at < 50 * 60:
            return self._jwt
        import base64

        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec, utils

        def b64(data: bytes) -> str:
            return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

        private_key = serialization.load_pem_private_key(
            self.config.apns_key_path.read_bytes(), password=None
        )
        header = b64(json.dumps({"alg": "ES256", "kid": self.config.apns_key_id}).encode())
        claims = b64(json.dumps({"iss": self.config.apns_team_id, "iat": int(now)}).encode())
        signing_input = f"{header}.{claims}".encode()
        der_signature = private_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
        r, s = utils.decode_dss_signature(der_signature)
        raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        self._jwt = f"{header}.{claims}.{b64(raw)}"
        self._jwt_issued_at = now
        return self._jwt

    def send(self, token: str, payload: dict[str, Any], priority: int = 10) -> tuple[int, str]:
        import httpx

        if self._client is None:
            self._client = httpx.Client(http2=True, timeout=10.0)
        url = f"https://{self.config.apns_host}/3/device/{token}"
        headers = {
            "authorization": f"bearer {self._token()}",
            "apns-topic": self.config.liveactivity_topic,
            "apns-push-type": "liveactivity",
            "apns-priority": str(priority),
            "apns-expiration": "0",
        }
        try:
            response = self._client.post(url, json=payload, headers=headers)
            return response.status_code, response.text
        except httpx.HTTPError as exc:
            return 0, str(exc)


class LiveActivityDaemon:
    def __init__(self, config: LiveActivityConfig, token_store: TokenStore | None = None) -> None:
        self.config = config
        self.tokens = token_store or TokenStore()
        self.apns = APNsLiveActivityClient(config)
        self.monitor = AgentMonitor.from_default_sources()
        self._condition = threading.Condition()
        self._latest: dict[str, Any] | None = None
        self._stop = threading.Event()
        self._last_push_at = 0.0
        self._last_push_state: str | None = None
        self._last_start_push_at = 0.0
        self._idle_since: float | None = None
        self._activity_live = False

    # -- snapshot loop -------------------------------------------------

    def run(self) -> None:
        server = self._build_server()
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        print(
            f"live-activity: serving on 0.0.0.0:{self.config.port}, "
            f"topic {self.config.liveactivity_topic}, tokens {self.tokens.summary()}"
        )
        try:
            while not self._stop.is_set():
                started = time.time()
                try:
                    self._tick()
                except Exception as exc:  # keep the loop alive
                    print(f"live-activity: tick failed: {exc}")
                elapsed = time.time() - started
                self._stop.wait(max(0.2, self.config.poll_seconds - elapsed))
        finally:
            server.shutdown()

    def stop(self) -> None:
        self._stop.set()
        with self._condition:
            self._condition.notify_all()

    def _tick(self) -> None:
        snapshot = self.monitor.snapshot(include_stale=False)
        statuses = list(snapshot.statuses)
        content_state = build_content_state(statuses, snapshot.aggregate.mode.value)

        with self._condition:
            changed = self._meaningfully_changed(content_state)
            self._latest = content_state
            if changed:
                self._condition.notify_all()

        now = time.time()
        active = content_state["activeCount"] > 0

        if active:
            self._idle_since = None
            if not self._activity_live:
                self._maybe_push_to_start(content_state, now)
            if self.tokens.tokens("update") and (
                (changed and now - self._last_push_at >= PUSH_MIN_INTERVAL_SECONDS)
                or now - self._last_push_at >= PUSH_HEARTBEAT_SECONDS
            ):
                self._push_update(content_state, now)
        else:
            if self._idle_since is None:
                self._idle_since = now
                if self.tokens.tokens("update") and changed:
                    self._push_update(content_state, now)
            elif (
                self._activity_live
                and now - self._idle_since >= self.config.idle_end_minutes * 60
            ):
                self._push_end(content_state, now)

    def _meaningfully_changed(self, content_state: dict[str, Any]) -> bool:
        if self._latest is None:
            return True
        old = {k: v for k, v in self._latest.items() if k != "updatedAt"}
        new = {k: v for k, v in content_state.items() if k != "updatedAt"}
        return old != new

    # -- APNs ----------------------------------------------------------

    def _apns_fanout(self, kind: str, payload: dict[str, Any], priority: int = 10) -> None:
        for token in self.tokens.tokens(kind):
            status, body = self.apns.send(token, payload, priority=priority)
            if status in (400, 410) and (
                "BadDeviceToken" in body or "Unregistered" in body or "ExpiredToken" in body
            ):
                print(f"live-activity: dropping dead {kind} token ({status})")
                self.tokens.drop(kind, token)
            elif status != 200:
                print(f"live-activity: APNs {kind} push -> {status} {body[:120]}")

    def _maybe_push_to_start(self, content_state: dict[str, Any], now: float) -> None:
        if not self.tokens.tokens("push_to_start"):
            return
        if now - self._last_start_push_at < PUSH_TO_START_COOLDOWN_SECONDS:
            return
        self._last_start_push_at = now
        payload = {
            "aps": {
                "timestamp": int(now),
                "event": "start",
                "content-state": content_state,
                "attributes-type": ATTRIBUTES_TYPE,
                "attributes": {"hostLabel": self.config.host_label},
                "alert": {
                    "title": f"Agents active on {self.config.host_label}",
                    "body": f"{content_state['activeCount']} agent(s) running",
                },
            }
        }
        print("live-activity: sending push-to-start")
        self._apns_fanout("push_to_start", payload)
        self._activity_live = True

    def _push_update(self, content_state: dict[str, Any], now: float) -> None:
        self._last_push_at = now
        payload = {
            "aps": {
                "timestamp": int(now),
                "event": "update",
                "content-state": content_state,
            }
        }
        self._apns_fanout("update", payload)
        self._activity_live = True

    def _push_end(self, content_state: dict[str, Any], now: float) -> None:
        payload = {
            "aps": {
                "timestamp": int(now),
                "event": "end",
                "content-state": content_state,
            }
        }
        print("live-activity: ending activity (idle)")
        self._apns_fanout("update", payload)
        self.tokens.clear("update")
        self._activity_live = False
        self._last_push_state = None

    # -- HTTP ----------------------------------------------------------

    def _build_server(self) -> ThreadingHTTPServer:
        daemon = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:
                pass

            def _json(self, status: int, body: dict[str, Any]) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                if self.path == "/health":
                    self._json(200, {"ok": True, "tokens": daemon.tokens.summary()})
                elif self.path == "/snapshot":
                    with daemon._condition:
                        latest = daemon._latest
                    self._json(200, latest or {})
                elif self.path == "/stream":
                    self._stream()
                else:
                    self._json(404, {"error": "not found"})

            def _stream(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                try:
                    while not daemon._stop.is_set():
                        with daemon._condition:
                            latest = daemon._latest
                            if latest is not None:
                                data = json.dumps(latest)
                            else:
                                data = "{}"
                        self.wfile.write(f"data: {data}\n\n".encode())
                        self.wfile.flush()
                        with daemon._condition:
                            daemon._condition.wait(SSE_HEARTBEAT_SECONDS)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return

            def do_POST(self) -> None:
                if self.path != "/register":
                    self._json(404, {"error": "not found"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    body = json.loads(self.rfile.read(length) or b"{}")
                except (ValueError, OSError):
                    self._json(400, {"error": "invalid JSON"})
                    return
                kind = body.get("kind")
                token = body.get("token", "")
                if kind not in ("push_to_start", "update") or not isinstance(token, str) or not token:
                    self._json(400, {"error": "kind must be push_to_start|update with a token"})
                    return
                meta = {
                    "device": str(body.get("device", "")),
                    "activity_id": str(body.get("activity_id", "")),
                }
                daemon.tokens.register(kind, token, meta)
                if kind == "update":
                    daemon._activity_live = True
                print(f"live-activity: registered {kind} token from {meta['device'] or 'unknown'}")
                self._json(200, {"ok": True, "tokens": daemon.tokens.summary()})

        class Server(ThreadingHTTPServer):
            daemon_threads = True

            def server_bind(self) -> None:
                # HTTPServer.server_bind calls socket.getfqdn(), which can
                # hang for tens of seconds on hosts with broken reverse DNS.
                import socketserver

                socketserver.TCPServer.server_bind(self)
                self.server_name = "sidepulse-live-activity"
                self.server_port = self.server_address[1]

        return Server(("0.0.0.0", self.config.port), Handler)
