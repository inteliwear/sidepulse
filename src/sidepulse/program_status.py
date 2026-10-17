"""OSC 7501 semantics carried as JSON; no provider heuristics in this module."""
from __future__ import annotations

import hashlib
import copy
import json
import re
import threading
import socket
import os
import time
from collections import OrderedDict, deque
import uuid
from datetime import datetime, timezone

from .models import AgentMode, AgentStatus

STATES = {'idle', 'working', 'blocked', 'done', 'error', 'clear'}
KINDS = {'permission', 'question', 'auth'}
SEGMENT = re.compile(r'[A-Za-z0-9_.+-]{1,32}\Z')
MAX_BYTES = 65536
MAX_SESSIONS = 64
CONTEXT_LIMITS = {'provider': 128, 'session_id': 512, 'agent_id': 512,
                  'cwd': 4096, 'origin': 512, 'display_name': 512, 'tool_name': 512,
                  'mode': 64, 'event_name': 128, 'source_name': 256, 'title': 512, 'title_source': 16}
# Leave room for push-provider framing, sender keys, and JSON string escaping.
MAX_PHONE_STATUS_BYTES = 3000
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


def validate_context(value: object) -> dict:
    if not isinstance(value, dict):
        raise StatusError('Invalid presentation context')
    return {key: text(value[key], limit, key) for key, limit in CONTEXT_LIMITS.items() if key in value}


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


def phone_update(update: dict) -> dict:
    """Bound one push, never the combined session ledger. IDs are never clipped."""
    result = {k: copy.deepcopy(v) for k, v in validate_update(update).items() if k != 'context'}
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


class ProgramState:
    """One current update per source/session, owned in memory by the daemon."""
    def __init__(self, *, publisher: str | None = None):
        self.publisher = publisher or str(uuid.uuid5(uuid.NAMESPACE_OID,
            f"sidepulse:{socket.gethostname()}:{uuid.getnode()}:{os.getuid()}"))
        self.lock = threading.RLock()
        self._sessions = OrderedDict()
        # Keep replay guards after clear/eviction; only current values are capped.
        self._sequences = {}
        self._suppressed = set()
        self._revision = 0
        self._receipts = OrderedDict()
        self._events = deque(maxlen=4096)
        self._cursors = {}
        self._event_serial = time.time_ns() // 1000

    def submit(self, report: dict) -> dict:
        if not isinstance(report, dict):
            raise StatusError('Report must be an object')
        if report.get('type') != 'program_status':
            raise StatusError('Unsupported message type')
        request = encoded(report)
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
            suppressed = report.get('_hook_suppressed') is True or scope in self._suppressed
            update = validate_update({**report, 'publisher': self.publisher,
                'seq': self._sequences.get(key, 0) + 1, 'updated_at': now,
                **({'status': {'state': 'clear'}} if suppressed else {})})
            previous = self._sessions.get(key)
            ignored = (report.get('unless_state') == 'done' and previous is not None
                       and previous['status']['state'] == 'done' and now - previous['updated_at'] <= 1200000)
            if ignored:
                self._expire_done(now)
            else:
                sequence = report.get('seq', update['seq'])
                if type(sequence) is not int or sequence != update['seq']:
                    raise StatusError('Sequence gap or duplicate; submit without seq locally')
                self._apply_update('local', update)
                if suppressed:
                    self._suppressed.add(scope)
                self._revision += 1
            ack = {'ok': True, 'seq': self._sequences[key], 'revision': self._revision}
            if receipt:
                self._receipts[receipt] = (request, ack)
                while len(self._receipts) > 4096:
                    self._receipts.popitem(last=False)
            return copy.deepcopy(ack)

    def _apply_update(self, sender, update):
        key = (sender, update['publisher'], update['scope'])
        previous = self._sessions.get(key)
        if sender == 'local' and previous and previous['status']['state'] == 'done' and update['updated_at'] - previous['updated_at'] > 1200000:
            previous = None
        if update['status']['state'] != 'clear' and 'context' in update:
            context = dict(update['context'])
            previous_context = previous.get('context', {}) if previous else {}
            if previous_context.get('title') and context.get('title_source') != 'session':
                context['title'] = previous_context['title']
            for field in ('cwd', 'origin', 'title'):
                if field not in context and field in previous_context:
                    context[field] = previous_context[field]
            if context.get('title') or context.get('cwd'):
                from .collector import display_name_from_parts, project_name
                short_id = (context.get('agent_id') or context.get('session_id') or '')[:8]
                context['display_name'] = display_name_from_parts(project_name(context.get('cwd')), context.get('title'), short_id, context.get('display_name') or '')
            update = {**update, 'context': validate_context(context)}
        # Prepare and validate the complete forwarded value before changing state.
        self._append_event(sender, update)
        if sender == 'local':
            self._expire_done(update['updated_at'])
        self._sequences[key] = update['seq']
        self._sessions.pop(key, None)
        if update['status']['state'] != 'clear':
            self._sessions[key] = update
        while len(self._sessions) > MAX_SESSIONS:
            self._sessions.popitem(last=False)

    def _expire_done(self, now):
        for key, update in list(self._sessions.items()):
            if key[:2] == ('local', self.publisher) and update['status']['state'] == 'done' and now - update['updated_at'] > 1200000:
                del self._sessions[key]

    def _append_event(self, sender, update):
        outgoing = dict(update)
        if sender != 'local':
            outgoing['publisher'] = hashlib.sha256(json.dumps([self.publisher, sender, update['publisher']]).encode()).hexdigest()
        # Epoch-based sequences keep increasing across daemon restarts while
        # matching the safe-integer range accepted by mobile clients.
        serial = max(self._event_serial + 1, time.time_ns() // 1000)
        outgoing['seq'] = serial
        outgoing = validate_update(outgoing)
        self._event_serial = serial
        self._events.append((serial, sender, outgoing))

    def pending_events(self, target: str, *, local_only: bool = False):
        with self.lock:
            cursor = self._cursors.get(target, 0)
            return [(number, copy.deepcopy(body)) for number, sender, body in self._events
                    if number > cursor and (not local_only or sender == 'local')][:128]

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

    def acknowledge_session(self, agent_id, seq, updated_at) -> bool:
        """Locally acknowledge this exact Done update without publishing or resequencing."""
        if not isinstance(agent_id, str) or type(seq) is not int or seq < 1 or not isinstance(updated_at, str):
            raise StatusError('Expected a session identity, sequence, and timestamp')
        with self.lock:
            for key, update in self._sessions.items():
                if self._session_identity(key) != agent_id:
                    continue
                timestamp = datetime.fromtimestamp(update['updated_at'] / 1000, timezone.utc).isoformat()
                if update['seq'] != seq or timestamp != updated_at or update['status']['state'] != 'done':
                    return False
                update['status'] = {k: v for k, v in update['status'].items() if k != 'msg'}
                update['status']['state'] = 'idle'
                self._revision += 1
                return True
            return False

    def statuses(self) -> tuple[AgentStatus, ...]:
        with self.lock:
            rows = copy.deepcopy(self._sessions)
        result = []
        for (sender, publisher, scope), update in sorted(rows.items()):
            status = update['status']
            app = status.get('app')
            context = update.get('context', {})
            mode = MODES[status['state']]
            detail_mode = context.get('mode')
            if mode == AgentMode.WORKING and detail_mode in {'tool_running', 'long_task_progress'}:
                mode = AgentMode(detail_mode)
            origin = context.get('origin')
            if sender != 'local' and context.get('source_name'):
                origin = f"{origin} · {context['source_name']}" if origin else context['source_name']
            identity = self._session_identity((sender, publisher, scope))
            result.append(AgentStatus(provider=context.get('provider') or app or 'program', agent_id=identity,
                display_name=context.get('display_name') or status.get('title') or app or scope, mode=mode,
                updated_at=datetime.fromtimestamp(update['updated_at'] / 1000, timezone.utc),
                event_name=context.get('event_name') or 'ProgramStatus', session_id=context.get('session_id') or identity,
                cwd=context.get('cwd'), tool_name=context.get('tool_name'), message=status.get('msg'), origin=origin or sender,
                seq=update['seq']))
        return tuple(result)

    def close(self):
        with self.lock:
            self._sessions.clear()
            self._sequences.clear()
            self._suppressed.clear()
            self._receipts.clear()
            self._events.clear()
            self._cursors.clear()
            self._revision = 0
