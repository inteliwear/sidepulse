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

import hashlib
import json
import os
import queue
import shutil
import socket
import subprocess
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
MAX_FINISHED_ROWS = 3
RECENT_FINISHED_SECONDS = 30 * 60.0
TERMINAL_MODES = {"completed", "idle_ready"}
MAX_NAME_CHARS = 44
MAX_DETAIL_CHARS = 32
PUSH_MIN_INTERVAL_SECONDS = 3.0
PUSH_HEARTBEAT_SECONDS = 300.0
PUSH_TO_START_COOLDOWN_SECONDS = 60.0
SSE_HEARTBEAT_SECONDS = 10.0
ATTRIBUTES_TYPE = "AgentActivityAttributes"

# Modes worth interrupting the user for, and their notification titles.
ALERT_MODES = {
    "waiting_for_input": "Needs your input",
    "blocked_error": "Blocked",
    "completed": "Finished",
}
ALERT_COOLDOWN_SECONDS = 90.0


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
    summaries_enabled: bool = True
    summary_model: str = "claude-haiku-4-5-20251001"

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
            "device": {},
        }
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        for kind in ("push_to_start", "update", "device"):
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

    def contains(self, kind: str, token: str) -> bool:
        with self._lock:
            return token in self._data[kind]

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


def status_row(status: AgentStatus) -> dict[str, Any]:
    return {
        "id": status.agent_id,
        "name": _truncate(status.display_name, MAX_NAME_CHARS),
        "mode": status.mode.value,
        "detail": _truncate(status.tool_name, MAX_DETAIL_CHARS) if status.tool_name else None,
        "provider": status.provider,
        "cwd": _truncate(Path(status.cwd).name, MAX_DETAIL_CHARS) if status.cwd else None,
    }


def build_content_state(
    statuses: list[AgentStatus],
    aggregate_mode: str,
    recent_finished: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Wire format shared with AgentActivityAttributes.ContentState.

    Active sessions come first, then recently finished ones — either still
    reported as completed by the collector or remembered by the daemon after
    the session closed. ``activeCount`` counts only non-terminal sessions.
    """
    ordered = sorted(
        statuses,
        key=lambda status: (MODE_PRIORITY.get(status.mode, 99), -status.updated_at.timestamp()),
    )
    active_rows = [
        status_row(status)
        for status in ordered
        if status.mode.value not in TERMINAL_MODES
    ][:MAX_AGENT_ROWS]

    seen_ids = {row["id"] for row in active_rows}
    finished_rows = []
    for row in sorted(recent_finished or [], key=lambda r: -r.get("finishedAt", 0.0)):
        if row["id"] in seen_ids:
            continue
        seen_ids.add(row["id"])
        finished_rows.append(row)
        if len(finished_rows) >= MAX_FINISHED_ROWS:
            break

    return {
        "aggregateMode": aggregate_mode,
        "activeCount": sum(
            1 for status in statuses if status.mode.value not in TERMINAL_MODES
        ),
        "agents": active_rows + finished_rows,
        "updatedAt": round(time.time(), 1),
    }


def compute_alerts(
    previous_modes: dict[str, str],
    statuses: list[AgentStatus],
    now: float,
    last_alerts: dict[tuple[str, str], float],
) -> tuple[list[dict[str, str]], dict[str, str]]:
    """Alerts for agents that TRANSITIONED into an alertable mode.

    Needs-input and blocked alert per agent, immediately. Finished alerts
    per SESSION GROUP: a main session reports completed while its subagents
    are still running, so "Finished" only fires once every member of the
    session is terminal.

    Returns (alerts, new_modes). ``last_alerts`` is mutated with sent
    timestamps so repeated flapping stays inside ALERT_COOLDOWN_SECONDS.
    An empty ``previous_modes`` produces no alerts — the first tick after a
    daemon restart must not replay every current state as news.
    """
    new_modes = {status.agent_id: status.mode.value for status in statuses}

    groups: dict[str, list[AgentStatus]] = {}
    for status in statuses:
        key = f"group:{status.provider}:{status.session_id or status.agent_id}"
        groups.setdefault(key, []).append(status)
    for group_key, members in groups.items():
        all_done = all(member.mode.value in TERMINAL_MODES for member in members)
        new_modes[group_key] = "completed" if all_done else "active"

    alerts: list[dict[str, str]] = []
    if not previous_modes:
        return alerts, new_modes

    def fire(key: tuple[str, str], title: str, body: str, thread_id: str) -> None:
        last_sent = last_alerts.get(key)
        if last_sent is not None and now - last_sent < ALERT_COOLDOWN_SECONDS:
            return
        last_alerts[key] = now
        alerts.append({"title": title, "body": body, "thread_id": thread_id})

    for status in statuses:
        mode = status.mode.value
        if mode not in ("waiting_for_input", "blocked_error"):
            continue
        if previous_modes.get(status.agent_id) == mode:
            continue
        fire(
            (status.agent_id, mode),
            f"{ALERT_MODES[mode]}: {_truncate(status.display_name, MAX_NAME_CHARS)}",
            status.tool_name or status.message or status.mode_label,
            status.agent_id,
        )

    for group_key, members in groups.items():
        if new_modes[group_key] != "completed":
            continue
        was_active = previous_modes.get(group_key) == "active" or any(
            previous_modes.get(member.agent_id) not in (None, *TERMINAL_MODES)
            for member in members
        )
        if not was_active:
            continue
        main = next(
            (member for member in members if ":session:" in member.agent_id),
            members[0],
        )
        fire(
            (group_key, "completed"),
            f"{ALERT_MODES['completed']}: {_truncate(main.display_name, MAX_NAME_CHARS)}",
            main.mode_label,
            group_key,
        )
    return alerts, new_modes


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

    def send(
        self,
        token: str,
        payload: dict[str, Any],
        priority: int = 10,
        push_type: str = "liveactivity",
        topic: str | None = None,
    ) -> tuple[int, str]:
        import httpx

        if self._client is None:
            self._client = httpx.Client(http2=True, timeout=10.0)
        url = f"https://{self.config.apns_host}/3/device/{token}"
        headers = {
            "authorization": f"bearer {self._token()}",
            "apns-topic": topic or self.config.liveactivity_topic,
            "apns-push-type": push_type,
            "apns-priority": str(priority),
            "apns-expiration": "0",
        }
        try:
            response = self._client.post(url, json=payload, headers=headers)
            return response.status_code, response.text
        except httpx.HTTPError as exc:
            return 0, str(exc)


SUMMARY_MAX_CHARS = 60


class SessionSummarizer:
    """Turns a session's last assistant message into a tiny outcome line
    ("TestFlight build deployed") via `claude -p` on a fast model.

    Runs the CLI with an isolated cwd whose path contains an ignored
    directory name and a private MOONSIDE_RUNTIME_DIR, so the summary
    sessions never appear in any monitor or on the lamp.
    """

    def __init__(self, model: str) -> None:
        self.model = model
        self.claude = shutil.which("claude") or "/opt/homebrew/bin/claude"
        self._results: dict[str, tuple[str, str]] = {}  # session -> (source_hash, summary)
        self._pending: set[str] = set()
        self._lock = threading.Lock()
        self._queue: "queue.Queue[tuple[str, str, str, str]]" = queue.Queue()
        base = default_state_dir() / "summarizer"
        # "memories" is on the ignored-directory list, hiding these runs
        # from every sidepulse consumer.
        self.workdir = base / "memories"
        self.moonside_dir = base / "moonside"
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.moonside_dir.mkdir(parents=True, exist_ok=True)
        worker = threading.Thread(target=self._worker, daemon=True)
        worker.start()

    def summary_for(
        self, session_id: str, message: str | None, context: str = ""
    ) -> str | None:
        if not message:
            with self._lock:
                cached = self._results.get(session_id)
            return cached[1] if cached else None
        source_hash = hashlib.sha256(message.encode()).hexdigest()[:16]
        with self._lock:
            cached = self._results.get(session_id)
            if cached and cached[0] == source_hash:
                return cached[1]
            if session_id not in self._pending:
                self._pending.add(session_id)
                self._queue.put((session_id, source_hash, message, context))
            return cached[1] if cached else None

    def _worker(self) -> None:
        while True:
            session_id, source_hash, message, context = self._queue.get()
            summary = self._generate(message, context)
            with self._lock:
                self._pending.discard(session_id)
                if summary:
                    self._results[session_id] = (source_hash, summary)

    def _generate(self, message: str, context: str) -> str | None:
        prompt = (
            "Summarize the state or outcome this AI assistant message "
            "describes, in at most six words, starting with the project or "
            "app name — like 'sidepulse: TestFlight build deployed' or "
            "'kleido: waiting for API key decision'. Infer the project from "
            "the MESSAGE CONTENT first; generic directory names like 'Git' "
            "are never project names. If there is no project, use a one-word "
            "topic instead (e.g. 'weather: ...'). Respond with only that "
            "phrase.\n\n"
            f"Context: {context[:300]}\n\n"
            f"Message:\n{message[:3000]}"
        )
        env = dict(os.environ)
        env["MOONSIDE_RUNTIME_DIR"] = str(self.moonside_dir)
        try:
            result = subprocess.run(
                [self.claude, "-p", prompt, "--model", self.model],
                capture_output=True,
                text=True,
                timeout=120,
                cwd=self.workdir,
                env=env,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"live-activity: summary generation failed: {exc}")
            return None
        if result.returncode != 0:
            print(f"live-activity: claude -p exited {result.returncode}: {result.stderr[:120]}")
            return None
        line = result.stdout.strip().splitlines()
        text = line[0].strip().strip("\"'") if line else ""
        return _truncate(text, SUMMARY_MAX_CHARS) if text else None


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
        self._agent_modes: dict[str, str] = {}
        self._last_alerts: dict[tuple[str, str], float] = {}
        self._last_rows: dict[str, dict[str, Any]] = {}
        self._recent_finished: dict[str, dict[str, Any]] = {}
        self._bg_holding: set[str] = set()
        self.summarizer = (
            SessionSummarizer(config.summary_model) if config.summaries_enabled else None
        )

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
        now_ts = time.time()
        statuses = list(snapshot.statuses)
        self._sync_background_tasks(statuses, now_ts)
        if self.summarizer is not None:
            statuses = [self._apply_summary(status) for status in statuses]
        self._remember_finished(statuses, now_ts)
        content_state = build_content_state(
            statuses,
            snapshot.aggregate.mode.value,
            recent_finished=list(self._recent_finished.values()),
        )

        with self._condition:
            changed = self._meaningfully_changed(content_state)
            self._latest = content_state
            if changed:
                self._condition.notify_all()

        now = time.time()
        active = content_state["activeCount"] > 0

        alerts, self._agent_modes = compute_alerts(
            self._agent_modes, statuses, now, self._last_alerts
        )
        if alerts and self.tokens.tokens("update"):
            # Alerting Live Activity update: buzzes and highlights the
            # activity without posting a separate notification banner.
            self._push_update(content_state, now, alert=alerts[0])
        elif self.tokens.tokens("update") and (
            (changed and now - self._last_push_at >= PUSH_MIN_INTERVAL_SECONDS)
            or (active and now - self._last_push_at >= PUSH_HEARTBEAT_SECONDS)
        ):
            self._push_update(content_state, now)

        if active:
            self._idle_since = None
            if not self._activity_live:
                self._maybe_push_to_start(content_state, now)
        else:
            if self._idle_since is None:
                self._idle_since = now
            elif (
                self._activity_live
                and now - self._idle_since >= self.config.idle_end_minutes * 60
            ):
                self._push_end(content_state, now)

    def _sync_background_tasks(self, statuses, now: float) -> None:
        """Mirror held-open sessions onto the Moonside lamp markers.

        The collector already classifies a Stop that reports running
        background tasks as long-task progress (the harness includes the
        list in the hook payload), so every sidepulse consumer agrees by
        itself. Moonside has its own marker files whose Stop hook writes
        idle immediately; flip them to working while the harness holds the
        session open, and restore when it truly finishes.
        """
        holding_now: set[str] = set()
        for status in statuses:
            if status.provider != "claude" or not status.session_id:
                continue
            if status.mode.value == "long_task_progress" and status.event_name == "Stop":
                holding_now.add(status.session_id)

        for session_id in holding_now - self._bg_holding:
            self._moonside_marker(session_id, "working", expect=("idle", None))
            print(f"live-activity: {session_id[:8]} has background tasks; holding busy")
        for session_id in self._bg_holding - holding_now:
            # Finished or resumed; real hook writes win, this only cleans up
            # a marker still showing our flip.
            self._moonside_marker(session_id, "idle", expect=("working", "Stop"))
        self._bg_holding = holding_now

    def _moonside_marker(
        self, session_id: str, state: str, expect: tuple[str, str | None]
    ) -> None:
        """Flip line 1 of the Moonside session marker, only when it still
        looks the way we left it (or the way Stop left it)."""
        runtime_dir = Path(os.environ.get("MOONSIDE_RUNTIME_DIR", "/tmp"))
        marker = runtime_dir / "moonside_sessions" / session_id
        try:
            lines = marker.read_text().splitlines()
        except OSError:
            return
        if not lines or lines[0] != expect[0]:
            return
        if expect[1] is not None and (len(lines) < 2 or lines[1] != expect[1]):
            return
        lines[0] = state
        tmp = marker.with_name(f".{marker.name}.la")
        try:
            tmp.write_text("\n".join(lines) + "\n")
            tmp.replace(marker)
        except OSError:
            tmp.unlink(missing_ok=True)

    def _apply_summary(self, status: AgentStatus) -> AgentStatus:
        """Once a turn has ended, the display name's prompt text is stale;
        show what actually happened instead."""
        from dataclasses import replace as dataclass_replace

        if status.provider not in {"claude", "codex"} or not status.session_id:
            return status
        settled = status.event_name in {"Stop", "SubagentStop"} and status.mode.value in {
            "completed",
            "waiting_for_input",
            "long_task_progress",
        }
        if not settled:
            return status
        context = f"working directory: {status.cwd or 'unknown'}; session title: {status.display_name}"
        summary = self.summarizer.summary_for(status.session_id, status.message, context)
        if not summary:
            return status
        return dataclass_replace(status, display_name=summary)

    def _remember_finished(self, statuses: list[AgentStatus], now: float) -> None:
        current = {status.agent_id: status for status in statuses}

        # Sessions that vanished while doing something count as finished:
        # a closed session emits SessionEnd and drops out of the collector
        # before its completed state becomes visible anywhere.
        for agent_id, prev_mode in self._agent_modes.items():
            if agent_id in current or prev_mode in TERMINAL_MODES:
                continue
            row = self._last_rows.get(agent_id)
            if row:
                self._recent_finished[agent_id] = {
                    **row,
                    "mode": "completed",
                    "detail": None,
                    "finishedAt": now,
                }

        for status in current.values():
            if status.mode.value == "completed":
                previous = self._recent_finished.get(status.agent_id, {})
                self._recent_finished[status.agent_id] = {
                    **status_row(status),
                    "detail": None,
                    "finishedAt": previous.get("finishedAt", now),
                }
            elif status.mode.value not in TERMINAL_MODES:
                # Reactivated: it is no longer "recently finished".
                self._recent_finished.pop(status.agent_id, None)

        for agent_id in list(self._recent_finished):
            if now - self._recent_finished[agent_id].get("finishedAt", 0.0) > RECENT_FINISHED_SECONDS:
                del self._recent_finished[agent_id]

        self._last_rows = {status.agent_id: status_row(status) for status in current.values()}

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
            if status == 410 or (status == 400 and "BadDeviceToken" in body):
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

    def _push_update(
        self,
        content_state: dict[str, Any],
        now: float,
        alert: dict[str, str] | None = None,
    ) -> None:
        self._last_push_at = now
        aps: dict[str, Any] = {
            "timestamp": int(now),
            "event": "update",
            "content-state": content_state,
        }
        if alert:
            print(f"live-activity: alerting update -> {alert['title']}")
            aps["alert"] = {
                "title": alert["title"],
                "body": alert["body"],
                "sound": "default",
            }
        self._apns_fanout("update", {"aps": aps})
        self._activity_live = True

    def _end_stale_activity(self, reason: str) -> None:
        with self._condition:
            latest = self._latest
        if self.tokens.tokens("update"):
            print(f"live-activity: {reason}; ending stale activity")
            payload = {
                "aps": {
                    "timestamp": int(time.time()),
                    "event": "end",
                    "dismissal-date": int(time.time()),
                    "content-state": latest or {
                        "aggregateMode": "idle_ready",
                        "activeCount": 0,
                        "agents": [],
                        "updatedAt": round(time.time(), 1),
                    },
                }
            }
            self._apns_fanout("update", payload)
        else:
            print(f"live-activity: {reason}; will restart")
        self.tokens.clear("update")
        self._activity_live = False
        self._last_start_push_at = 0.0

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
                if kind == "reset":
                    # The app launched and found no live activity on the
                    # phone — whatever update tokens we hold are dead.
                    daemon._end_stale_activity("app reports no activity")
                    self._json(200, {"ok": True, "tokens": daemon.tokens.summary()})
                    return
                if (
                    kind not in ("push_to_start", "update", "device")
                    or not isinstance(token, str)
                    or not token
                ):
                    self._json(400, {"error": "kind must be push_to_start|update|device with a token"})
                    return
                meta = {
                    "device": str(body.get("device", "")),
                    "activity_id": str(body.get("activity_id", "")),
                }
                is_new = not daemon.tokens.contains(kind, token)
                daemon.tokens.register(kind, token, meta)
                if kind == "update":
                    daemon._activity_live = True
                elif kind == "push_to_start" and is_new:
                    # Fresh install or token rotation: any previously known
                    # activity is stale. End it, forget its tokens, and allow
                    # an immediate restart on the next tick.
                    daemon._end_stale_activity("new push-to-start token")
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
