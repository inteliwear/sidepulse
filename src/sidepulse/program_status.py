"""OSC 7501 semantics carried as JSON; no provider heuristics in this module."""
from __future__ import annotations

import hashlib
import copy
import json
import re
import threading
import os
import time
from collections import OrderedDict
import uuid
import urllib.parse
from datetime import datetime, timezone

from .models import AgentMode, AgentStatus

STATES = {'idle', 'working', 'blocked', 'done', 'error', 'clear'}
KINDS = {'permission', 'question', 'auth'}
SEGMENT = re.compile(r'[A-Za-z0-9_.+-]{1,32}\Z')
MAX_BYTES = 65536
MAX_SESSIONS = 64
RECENT_SESSION_LIMIT = 10
CONTEXT_LIMITS = {'provider': 128, 'session_id': 512, 'agent_id': 512,
                  'cwd': 4096, 'origin': 512, 'display_name': 512, 'tool_name': 512,
                  'mode': 64, 'event_name': 128, 'source_name': 256, 'title': 512, 'title_source': 16,
                  't3_link': 1024}
# Leave room for push-provider framing, sender keys, and JSON string escaping.
MAX_PHONE_STATUS_BYTES = 1800
MAX_BRIDGE_MESSAGE_BYTES = 2048
MAX_OUTBOUND_MESSAGE_BYTES = 1024
HERDR_EVENT = 'HerdrStatus'
TERMINAL_EVENT = 'TerminalOSC'
SESSION_CLOSED_EVENT = 'SessionClosed'
T3_TITLE_SOURCE = 't3'
HERDR_SCOPE = re.compile(r'herdr-[0-9a-f]{32}-[0-9a-f]{32}\Z')
MODES = {'idle': AgentMode.IDLE_READY, 'working': AgentMode.WORKING,
         'blocked': AgentMode.WAITING_FOR_INPUT, 'done': AgentMode.COMPLETED,
         'error': AgentMode.BLOCKED_ERROR}


class StatusError(ValueError):
    pass


def text(value: object, limit: int, field: str) -> str:
    if not isinstance(value, str):
        raise StatusError(f'{field} must be a string')
    try:
        size = len(value.encode('utf-8'))
    except UnicodeError as exc:
        raise StatusError(f'{field} must be UTF-8') from exc
    if size > limit or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value):
        raise StatusError(f'{field} exceeds its limit or contains control characters')
    return value


def identifier(value: object, field: str = 'scope') -> str:
    result = text(value, 128, field)
    if not result:
        raise StatusError(f'{field} must not be empty')
    return result


def integer(value: object, field: str, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= 9007199254740991:
        raise StatusError(f'{field} must be a safe nonnegative integer')
    return value


def validate_status(value: object) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get('state'), str) or value.get('state') not in STATES:
        raise StatusError('Unknown or missing state')
    state = value['state']
    result = {'state': state}
    if 'id' in value:
        record_id = text(value['id'], 128, 'id')
        parts = record_id.split('/')
        if len(parts) > 8 or not all(SEGMENT.fullmatch(p) for p in parts):
            raise StatusError('Invalid record id')
        result['id'] = record_id
    for field, limit in [('msg', 2048), ('title', 192)]:
        if field in value:
            result[field] = text(value[field], limit, field)
    if 'app' in value:
        app = text(value['app'], 32, 'app')
        if SEGMENT.fullmatch(app):
            result['app'] = app
    if state == 'blocked' and isinstance(value.get('kind'), str) and value.get('kind') in KINDS:
        result['kind'] = value['kind']
    progress = value.get('progress')
    if state in {'working', 'blocked'} and type(progress) is int and 0 <= progress <= 100:
        result['progress'] = progress
    return result


def hook_scope(provider: str, identity: str) -> str:
    return provider + '-' + hashlib.sha256(identity.encode()).hexdigest()[:32]


def is_integrated_session(scope: str, context: dict) -> bool:
    if HERDR_SCOPE.fullmatch(scope) or context.get('t3_link'):
        return True
    provider = context.get('provider')
    session_id = context.get('session_id')
    if not provider or not session_id:
        return False
    return scope == hook_scope(provider, session_id) or (
        provider == 'opencode' and scope == f'opencode-{session_id}'
    )


def validate_context(value: object) -> dict:
    if not isinstance(value, dict):
        raise StatusError('Invalid presentation context')
    result = {key: text(value[key], limit, key) for key, limit in CONTEXT_LIMITS.items() if key in value}
    if 't3_link' in result:
        from .origin import valid_t3_link
        if not valid_t3_link(result['t3_link']):
            raise StatusError('Invalid T3 Code thread link')
    return result


def encoded(value: object) -> str:
    result = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False, sort_keys=True)
    if len(result.encode('utf-8')) > MAX_BYTES:
        raise StatusError('Status message exceeds 65536 bytes')
    return result


def fits_phone_push(update: dict) -> bool:
    # FCM transports the nested object as a JSON string; budget that larger form.
    return len(json.dumps({'data': encoded(update)}, ensure_ascii=False).encode('utf-8')) <= MAX_PHONE_STATUS_BYTES


def validate_update(value: object) -> dict:
    if not isinstance(value, dict) or type(value.get('v')) is not int or value['v'] != 1:
        raise StatusError('Unsupported status update')
    encoded(value)
    if value.get('type') != 'program_status':
        raise StatusError('Unsupported status update')
    update = {'v': 1, 'type': 'program_status', 'publisher': identifier(value.get('publisher'), 'publisher'),
              'scope': identifier(value.get('scope')), 'seq': integer(value.get('seq'), 'seq', 1),
              'updated_at': integer(value.get('updated_at'), 'updated_at')}
    if update['updated_at'] > 253402300799999:
        raise StatusError('updated_at out of range')
    update['status'] = validate_status(value.get('status'))
    if 'context' in value:
        update['context'] = validate_context(value['context'])
    return update


def bridge_update(update: dict) -> dict:
    """Clip outgoing message text without changing the source or OSC validation."""
    result = validate_update(update)
    if 'msg' in result['status']:
        result['status']['msg'] = result['status']['msg'].encode('utf-8')[:MAX_OUTBOUND_MESSAGE_BYTES].decode('utf-8', errors='ignore')
    context = result.pop('context', {})
    title = context.get('title')
    if title and result['status']['state'] != 'clear':
        title = title.encode('utf-8')[:192].decode('utf-8', errors='ignore')
        if context.get('title_source') == T3_TITLE_SOURCE:
            result['status']['title'] = title
        else:
            result['status'].setdefault('title', title)
    # Keep native session identity for future provider links, not bulky paths or labels.
    identity = {key: context[key] for key in ('provider', 'session_id') if key in context}
    if identity:
        result['context'] = identity
    return result


def phone_update(update: dict) -> dict:
    """Bound one push, never the combined session ledger. IDs are never clipped."""
    result = {k: copy.deepcopy(v) for k, v in bridge_update(update).items() if k != 'context'}
    status = result.get('status', {})
    title = update.get('context', {}).get('title')
    if title and status and status['state'] != 'clear':
        status.setdefault('title', title.encode('utf-8')[:192].decode('utf-8', errors='ignore'))
    # Account for JSON escaping and FCM's nested JSON string as well as UTF-8.
    while not fits_phone_push(result) and status.get('msg'):
        status['msg'] = status['msg'][:-1]
    if not fits_phone_push(result):
        raise StatusError('Status update exceeds phone push budget')
    return result


def session_link_identity(row: dict) -> tuple[str, str] | None:
    link = urllib.parse.urlsplit(row.get('session_link') or '')
    if link.scheme != 'sidepulse' or link.netloc != 'sessions':
        return None
    query = urllib.parse.parse_qs(link.query)
    if len(query.get('publisher', [])) != 1 or len(query.get('scope', [])) != 1:
        return None
    return query['publisher'][0], query['scope'][0]


def _same_pane_agent(previous: dict, update: dict) -> bool:
    def agent(report):
        context = report.get('context', {})
        provider = context.get('provider') or report['status'].get('app')
        return 'claude' if provider == 'claude-code' else provider, context.get('session_id')

    return all(not left or not right or left == right
               for left, right in zip(agent(previous), agent(update)))


def _ignore_pane_update(previous: dict, update: dict) -> bool:
    old_context = previous.get('context', {})
    new_context = update.get('context', {})
    source = new_context.get('event_name')
    old_source = old_context.get('event_name')
    if source == HERDR_EVENT:
        if old_source != HERDR_EVENT:
            return True
        # Snapshot reconciliation may repeat events from other panes. Repeating
        # the same report would renew Done and re-trigger phone LEDs.
        return (previous['status'] == update['status']
                and old_context.get('session_id') == new_context.get('session_id')
                and all(old_context.get(field) == new_context[field]
                        for field in ('provider', 'cwd', 'title', 'origin') if field in new_context))
    return source == TERMINAL_EVENT and old_source not in {HERDR_EVENT, TERMINAL_EVENT}


def installation_publisher(path=None) -> str:
    """An installation identity survives hostname and network-interface changes."""
    from .settings import default_config_dir
    import tempfile
    path = path or default_config_dir() / 'publisher-id'
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        with tempfile.NamedTemporaryFile(dir=path.parent, mode='w', delete=False) as temporary:
            # Seed once from the previous identity so upgrading does not duplicate
            # existing phone sessions. Later host/network changes no longer affect it.
            import socket
            if os.name == 'nt':
                from .windows_support import user_sid
                user = user_sid()
            else:
                user = os.getuid()
            temporary.write(str(uuid.uuid5(uuid.NAMESPACE_OID,
                f"sidepulse:{socket.gethostname()}:{uuid.getnode()}:{user}")))
        try:
            os.link(temporary.name, path)
        except FileExistsError:
            pass
        finally:
            os.unlink(temporary.name)
    return str(uuid.UUID(path.read_text().strip()))


class ProgramState:
    """One current update per source/session, owned in memory by the daemon."""
    def __init__(self, *, publisher: str | None = None):
        self.publisher = publisher or installation_publisher()
        self.lock = threading.RLock()
        self._sessions = OrderedDict()
        self._closed_sessions = {}
        # Clear records keep a replay guard within the same bounded latest-session set.
        self._sequences = OrderedDict()
        self._suppressed = set()
        self._revision = 0
        self._receipts = OrderedDict()
        self._events = OrderedDict()
        self._cursors = {}
        self._event_serial = time.time_ns() // 1000

    def submit(
        self, report: dict, *, observed_at: int | None = None,
        wait_cleared_at: int | None = None, t3_scan: bool = False,
    ) -> dict:
        """Apply a local report, preserving input waits newer than a log's last release."""
        if not isinstance(report, dict):
            raise StatusError('Report must be an object')
        if report.get('type') != 'program_status':
            raise StatusError('Unsupported message type')
        request = encoded(report)
        guard_kind = report.get('unless_kind')
        if 'unless_kind' in report and (not isinstance(guard_kind, str) or guard_kind not in KINDS):
            raise StatusError('Unknown blocked kind guard')
        receipt = report.get('request_id')
        if receipt is not None:
            identifier(receipt, 'request_id')
        with self.lock:
            if receipt in self._receipts:
                old_request, ack = self._receipts[receipt]
                if old_request != request:
                    raise StatusError('request_id reused for a different report')
                return copy.deepcopy(ack)
            now = int(datetime.now(timezone.utc).timestamp() * 1000)
            scope = identifier(report.get('scope'))
            key = ('local', self.publisher, scope)
            if key not in self._sessions and 'context' in report:
                incoming_context = validate_context(report['context'])
                # A log may identify the thread before it knows the native session ID.
                scope = self._scope_for_thread(scope, incoming_context)
                key = ('local', self.publisher, scope)
            suppressed = report.get('_hook_suppressed') is True or scope in self._suppressed
            update = validate_update({**report, 'publisher': self.publisher, 'scope': scope,
                'seq': self._sequences.get(key, 0) + 1, 'updated_at': now if observed_at is None else observed_at,
                **({'status': {'state': 'clear'}} if suppressed else {})})
            previous = self._sessions.get(key)
            closed = self._closed_sessions.get(key)
            if observed_at is not None and closed and observed_at <= closed['updated_at']:
                return {'ok': True, 'seq': self._sequences.get(key, 0), 'revision': self._revision}
            if observed_at is not None and previous and observed_at < previous['updated_at']:
                if update['status']['state'] == 'clear':
                    return {'ok': True, 'seq': self._sequences[key], 'revision': self._revision}
                # Delayed log events may identify a live session but cannot rewind it.
                context = dict(previous.get('context', {}))
                incoming = update.get('context', {})
                fields = ('origin', 't3_link')
                if incoming.get('title_source') == T3_TITLE_SOURCE and incoming.get('title'):
                    fields += ('title', 'title_source')
                for field in fields:
                    if field in incoming:
                        context[field] = incoming[field]
                if context != previous.get('context', {}):
                    self._apply_update('local', {**previous, 'seq': update['seq'], 'context': context})
                    self._revision += 1
                return {'ok': True, 'seq': self._sequences[key], 'revision': self._revision}
            herdr_pane = HERDR_SCOPE.fullmatch(scope)
            if herdr_pane and previous and not _same_pane_agent(previous, update):
                previous = None
            ignored = (report.get('unless_state') == 'done' and previous is not None
                       and previous['status']['state'] == 'done' and now - previous['updated_at'] <= 1200000)
            if previous and '_waiting_tool' in previous and report.get('_finished_tool'):
                # A parallel tool may return while the monitored call is still open.
                ignored = ignored or report['_finished_tool'] != previous['_waiting_tool']
            if previous is not None and previous['status']['state'] == 'blocked':
                ignored = ignored or (
                    guard_kind is not None and previous['status'].get('kind') == guard_kind
                ) or (
                    not suppressed and wait_cleared_at is not None
                    and previous['updated_at'] > wait_cleared_at
                )
            if herdr_pane:
                hook_scope = report.get('_hook_scope')
                if isinstance(hook_scope, str) and hook_scope in self._suppressed:
                    ignored = True
                if previous and not (previous['status']['state'] == 'done' and now - previous['updated_at'] > 1200000):
                    ignored = ignored or _ignore_pane_update(previous, update)
            if not ignored:
                sequence = report.get('seq', update['seq'])
                if type(sequence) is not int or sequence != update['seq']:
                    raise StatusError('Sequence gap or duplicate; submit without seq locally')
                if t3_scan and (previous is None or previous.get('_t3_scan')):
                    update['_t3_scan'] = True
                wait = report.get('_tool_wait')
                if isinstance(wait, dict) and update['status']['state'] == 'working':
                    update['_waiting_tool'] = identifier(wait.get('call'), 'tool call')
                    update['_wait_deadline'] = time.monotonic() + integer(wait.get('seconds'), 'seconds', 1)
                    update['_wait_message'] = text(wait.get('message'), MAX_OUTBOUND_MESSAGE_BYTES, 'message')
                self._apply_update('local', update)
                if suppressed:
                    self._suppressed.add(scope)
                self._revision += 1
            ack = {'ok': True, 'seq': self._sequences.get(key, 0), 'revision': self._revision}
            if receipt:
                self._receipts[receipt] = (request, ack)
                while len(self._receipts) > MAX_SESSIONS:
                    self._receipts.popitem(last=False)
            return copy.deepcopy(ack)

    def check_tool_waits(self) -> None:
        # Keep the deadline on the current local row. Replacing or removing that
        # row cancels it, even if the session ends on the same tick.
        with self.lock:
            now = time.monotonic()
            for row in list(self._sessions.values()):
                if '_wait_deadline' not in row or now < row['_wait_deadline']:
                    continue
                update = dict(row)
                del update['_wait_deadline']
                update['status'] = {**row['status'], 'state': 'blocked', 'kind': 'question',
                                    'msg': update.pop('_wait_message')}
                update['context'] = {**row.get('context', {}), 'mode': 'waiting_for_input'}
                # Preserve the hook time so a delayed real event can supersede it.
                update['seq'] += 1
                self._apply_update('local', update)
                self._revision += 1

    def clear_t3_scan_sessions(self) -> None:
        with self.lock:
            for (_, _, scope), update in list({**self._closed_sessions, **self._sessions}.items()):
                if update.get('_t3_scan'):
                    self.submit({'v': 1, 'type': 'program_status', 'scope': scope,
                                 'status': {'state': 'clear'}})

    def _scope_for_thread(self, scope, context):
        provider = context.get('provider')
        link = context.get('t3_link')
        session_id = context.get('session_id')
        is_hook_scope = (isinstance(provider, str) and isinstance(session_id, str)
                         and scope == hook_scope(provider, session_id))
        if not link and not is_hook_scope:
            return scope
        for (sender, publisher, existing_scope), row in self._sessions.items():
            if (sender, publisher) != ('local', self.publisher):
                continue
            existing = row.get('context', {})
            if existing.get('provider') != provider or not existing.get('t3_link'):
                continue
            if existing['t3_link'] == link or (
                not link and is_hook_scope and existing.get('session_id') == session_id
            ):
                return existing_scope
        return scope

    def _apply_update(self, sender, update):
        key = (sender, update['publisher'], update['scope'])
        current = self._sessions.get(key)
        previous = current
        if sender == 'local' and previous and previous['status']['state'] == 'done' and update['updated_at'] - previous['updated_at'] > 1200000:
            previous = None
        herdr_pane = sender == 'local' and HERDR_SCOPE.fullmatch(update['scope'])
        if herdr_pane and previous and not _same_pane_agent(previous, update):
            previous = None
        if update['status']['state'] != 'clear' and 'context' in update:
            context = dict(update['context'])
            previous_context = previous.get('context', {}) if previous else {}
            if previous_context.get('t3_link'):
                if previous_context.get('origin') and 't3_link' not in context:
                    context['origin'] = previous_context['origin']
                if previous_context.get('session_id'):
                    context.setdefault('session_id', previous_context['session_id'])
            title_source = context.get('title_source')
            previous_source = previous_context.get('title_source')
            if previous_context.get('title') and (
                title_source not in {'session', T3_TITLE_SOURCE}
                or (previous_source == T3_TITLE_SOURCE and title_source != T3_TITLE_SOURCE)
            ):
                context['title'] = previous_context['title']
                if previous_source == T3_TITLE_SOURCE:
                    context['title_source'] = previous_source
            inherited = ('cwd', 'origin', 'title', 't3_link')
            if herdr_pane and context.get('event_name') == TERMINAL_EVENT:
                inherited += ('provider', 'session_id')
            for field in inherited:
                if field not in context and field in previous_context:
                    context[field] = previous_context[field]
            if context.get('title') or context.get('cwd'):
                from .collector import display_name_from_parts, project_name
                short_id = (context.get('agent_id') or context.get('session_id') or '')[:8]
                context['display_name'] = display_name_from_parts(project_name(context.get('cwd')), context.get('title'), short_id, context.get('display_name') or '')
            update = {**update, 'context': validate_context(context)}
        # Prepare and validate the complete forwarded value before changing state.
        self._append_event(sender, update)
        self._sequences.pop(key, None)
        self._sequences[key] = update['seq']
        self._sessions.pop(key, None)
        if update['status']['state'] != 'clear':
            self._closed_sessions.pop(key, None)
            self._sessions[key] = update
        elif sender == 'local' and update.get('context', {}).get('event_name') == SESSION_CLOSED_EVENT:
            if current is not None:
                # A normal exit still forwards clear. Keep its title and link for the desktop menu.
                status = {field: current['status'][field] for field in ('app', 'title')
                          if field in current['status']}
                self._closed_sessions[key] = {
                    **current, 'updated_at': update['updated_at'], 'seq': update['seq'],
                    'status': {**status, 'state': 'done'},
                    'context': {**current.get('context', {}), 'event_name': SESSION_CLOSED_EVENT},
                }
                if len(self._closed_sessions) > RECENT_SESSION_LIMIT:
                    oldest = min(self._closed_sessions, key=lambda key: self._closed_sessions[key]['updated_at'])
                    del self._closed_sessions[oldest]
        else:
            self._closed_sessions.pop(key, None)
        while len(self._sessions) > MAX_SESSIONS:
            self._sessions.popitem(last=False)
        while len(self._sequences) > MAX_SESSIONS * 2:
            oldest, _ = self._sequences.popitem(last=False)
            self._sessions.pop(oldest, None)
            if oldest[0] == 'local':
                self._suppressed.discard(oldest[2])

    def _append_event(self, sender, update):
        outgoing = dict(update)
        outgoing['publisher'] = self.forward_publisher(sender, update['publisher'])
        # Epoch-based sequences keep increasing across daemon restarts while
        # matching the safe-integer range accepted by mobile clients.
        serial = max(self._event_serial + 1, time.time_ns() // 1000)
        outgoing['seq'] = serial
        outgoing = validate_update(outgoing)
        self._event_serial = serial
        key = (sender, update['publisher'], update['scope'])
        self._events.pop(key, None)
        self._events[key] = (serial, sender, outgoing)
        while len(self._events) > MAX_SESSIONS:
            self._events.popitem(last=False)

    def forward_publisher(self, sender, publisher):
        if sender == 'local':
            return publisher
        return hashlib.sha256(json.dumps([self.publisher, sender, publisher]).encode()).hexdigest()

    def pending_events(self, target: str, *, local_only: bool = False):
        with self.lock:
            cursor = self._cursors.get(target, 0)
            return [(number, copy.deepcopy(body)) for number, sender, body in self._events.values()
                    if number > cursor and (not local_only or sender == 'local')][:128]

    def start_delivery(self, target):
        """A newly enabled destination starts with the next arriving update."""
        with self.lock:
            self._cursors[target] = self._event_serial

    def event_is_pending(self, target, number):
        with self.lock:
            return (number > self._cursors.get(target, 0)
                    and any(event[0] == number for event in self._events.values()))

    def acknowledge_event(self, target, number):
        with self.lock:
            self._cursors[target] = max(number, self._cursors.get(target, 0))

    def retain_targets(self, targets):
        with self.lock:
            self._cursors = {key: value for key, value in self._cursors.items() if key in targets}

    def receive_update(self, sender: str, value: object) -> bool:
        update = validate_update(value)
        with self.lock:
            key = (sender, update['publisher'], update['scope'])
            if self._sequences.get(key, 0) >= update['seq']:
                return False
            self._apply_update(sender, update)
            return True

    def retain_senders(self, senders):
        with self.lock:
            for key in list(self._sequences):
                sender, publisher, scope = key
                if sender in senders:
                    continue
                self._append_event(sender, {'v': 1, 'type': 'program_status', 'status': {'state': 'clear'},
                    'publisher': publisher, 'scope': scope, 'updated_at': int(datetime.now(timezone.utc).timestamp() * 1000)})
                self._sessions.pop(key, None)
                del self._sequences[key]

    @staticmethod
    def _session_identity(key):
        return 'program:' + hashlib.sha256(json.dumps(list(key)).encode()).hexdigest()

    def statuses(
        self, *, inactive_before_ms: float | None = None, include_closed: bool = False,
    ) -> tuple[AgentStatus, ...]:
        with self.lock:
            closed = self._closed_sessions if include_closed else {}
            rows = copy.deepcopy({**closed, **self._sessions})
        result = []
        for (sender, publisher, scope), update in sorted(rows.items()):
            status = update['status']
            app = status.get('app')
            context = update.get('context', {})
            if (inactive_before_ms is not None and update['updated_at'] < inactive_before_ms
                    and sender == 'local' and status['state'] != 'done'
                    and (update.get('_t3_scan') or is_integrated_session(scope, context))):
                continue
            updated_at = datetime.fromtimestamp(update['updated_at'] / 1000, timezone.utc)
            mode = MODES[status['state']]
            detail_mode = context.get('mode')
            if mode == AgentMode.WORKING and detail_mode in {'tool_running', 'long_task_progress'}:
                mode = AgentMode(detail_mode)
            origin = context.get('origin')
            if sender != 'local' and context.get('source_name'):
                origin = f"{origin} · {context['source_name']}" if origin else context['source_name']
            identity = self._session_identity((sender, publisher, scope))
            session_id = context.get('session_id')
            t3_link = context.get('t3_link') if sender == 'local' else None
            if session_id is None and not t3_link:
                session_id = identity
            result.append(AgentStatus(provider=context.get('provider') or app or 'program', agent_id=identity,
                display_name=context.get('display_name') or status.get('title') or app or scope, mode=mode,
                updated_at=updated_at,
                event_name=context.get('event_name') or 'ProgramStatus', session_id=session_id,
                cwd=context.get('cwd'), tool_name=context.get('tool_name'), message=status.get('msg'), origin=origin or sender,
                seq=update['seq'], session_link='sidepulse://sessions?' + urllib.parse.urlencode(
                    {'publisher': self.forward_publisher(sender, publisher), 'scope': scope}, quote_via=urllib.parse.quote),
                stale=context.get('event_name') == SESSION_CLOSED_EVENT, t3_link=t3_link))
        return tuple(result)

    def close(self):
        with self.lock:
            self._sessions.clear()
            self._closed_sessions.clear()
            self._sequences.clear()
            self._suppressed.clear()
            self._receipts.clear()
            self._events.clear()
            self._cursors.clear()
            self._revision = 0
