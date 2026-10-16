"""OSC 7501 semantics carried as JSON; no provider heuristics in this module."""
from __future__ import annotations

import hashlib
import copy
import json
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .models import AgentMode, AgentStatus
from .providers import default_state_dir

STATES = {'idle', 'working', 'blocked', 'done', 'error', 'clear'}
KINDS = {'permission', 'question', 'auth'}
SEGMENT = re.compile(r'[A-Za-z0-9_.+-]{1,32}\Z')
MAX_BYTES = 65536
MAX_SCOPES = 64
MAX_RECORDS = 256
# Leave room for push-provider framing, sender keys, and JSON string escaping.
MAX_PHONE_STATUS_BYTES = 3000
MODES = {'idle': AgentMode.IDLE_READY, 'working': AgentMode.WORKING,
         'blocked': AgentMode.WAITING_FOR_INPUT, 'done': AgentMode.COMPLETED,
         'error': AgentMode.BLOCKED_ERROR}
PRIORITY = {'error': 0, 'blocked': 1, 'working': 2, 'done': 3, 'idle': 4}


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


def validate_status(value: object, *, allow_clear: bool = True) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get('state'), str) or value.get('state') not in STATES:
        raise StatusError('Unknown or missing state')
    state = value['state']
    if state == 'clear' and not allow_clear:
        raise StatusError('Snapshots contain records, not clear operations')
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
    limits = {'provider': 128, 'legacy_key': 512, 'session_id': 512, 'agent_id': 512,
              'cwd': 4096, 'origin': 512, 'display_name': 512, 'tool_name': 512,
              'mode': 64, 'event_name': 128, 'source_name': 256, 'title': 512}
    return {key: text(value[key], limit, key) for key, limit in limits.items() if key in value}


def context_legacy_key(context: dict, sender: str) -> str | None:
    if sender == 'local':
        return context.get('legacy_key')
    if context.get('provider') and context.get('source_name'):
        identity = context.get('agent_id') or context.get('session_id') or 'default'
        return f"{context['provider']}:agent:relay:{context['source_name']}:{identity}"
    return None


def encoded(value: object) -> str:
    result = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False, sort_keys=True)
    if len(result.encode('utf-8')) > MAX_BYTES:
        raise StatusError('Status snapshot exceeds 65536 bytes')
    return result


def fits_phone_push(snapshot: dict) -> bool:
    # FCM transports the nested object as a JSON string; budget that larger form.
    return len(json.dumps({'data': encoded(snapshot)}, ensure_ascii=False).encode('utf-8')) <= MAX_PHONE_STATUS_BYTES


def validate_snapshot(value: object) -> dict:
    if not isinstance(value, dict) or type(value.get('v')) is not int or value['v'] != 1 or value.get('type') != 'program_status_snapshot':
        raise StatusError('Unsupported status snapshot')
    result = {'v': 1, 'type': 'program_status_snapshot',
              'publisher': identifier(value.get('publisher'), 'publisher'),
              'revision': integer(value.get('revision'), 'revision'), 'scopes': {}}
    scopes = value.get('scopes')
    if not isinstance(scopes, dict) or len(scopes) > MAX_SCOPES:
        raise StatusError('Invalid scopes')
    count = 0
    for scope, item in scopes.items():
        identifier(scope)
        if not isinstance(item, dict) or not isinstance(item.get('records'), list):
            raise StatusError('Invalid scope records')
        records, ids = [], set()
        for record in item['records']:
            if not isinstance(record, dict):
                raise StatusError('Invalid record')
            status = validate_status(record.get('status'), allow_clear=False)
            key = status.get('id', '')
            if key in ids:
                raise StatusError('Duplicate record id')
            ids.add(key)
            updated = integer(record.get('updated_at'), 'updated_at')
            if updated > 253402300799999:
                raise StatusError('updated_at out of range')
            parsed = {'status': status, 'updated_at': updated}
            if 'context' in record:
                parsed['context'] = validate_context(record['context'])
            records.append(parsed)
            count += 1
        result['scopes'][scope] = {'seq': integer(item.get('seq'), 'seq'), 'records': records}
    if count > MAX_RECORDS:
        raise StatusError('Too many records')
    encoded(result)
    return result


def apply_report(snapshot: dict, report: dict, now: int) -> dict:
    if type(report.get('v')) is not int or report['v'] != 1:
        raise StatusError('Unsupported protocol version')
    scope = identifier(report.get('scope'))
    kind = report.get('type')
    if not isinstance(kind, str) or kind not in {'program_status', 'scope_end', 'scope_reset'}:
        raise StatusError('Unsupported message type')
    status = validate_status(report.get('status')) if kind == 'program_status' else None
    existing = snapshot['scopes'].get(scope, {}).get('records', [])
    if report.get('unless_state') == 'done' and any(
            r['status'].get('id') == (status or {}).get('id') and r['status']['state'] == 'done' for r in existing):
        return copy.deepcopy(snapshot)
    updated = copy.deepcopy(snapshot)
    item = updated['scopes'].setdefault(scope, {'seq': 0, 'records': []})
    seq = report.get('seq', item['seq'] + 1)
    integer(seq, 'seq', 1)
    if seq != item['seq'] + 1:
        raise StatusError('Sequence gap or duplicate; submit without seq locally')
    records = {r['status'].get('id', ''): r for r in item['records']}
    if kind == 'scope_reset':
        records.clear()
    elif kind == 'scope_end':
        records = {k: r for k, r in records.items() if r['status']['state'] in {'done', 'error'}}
    elif status['state'] == 'clear':
        key = status.get('id', '')
        records = {k: r for k, r in records.items() if key and k != key and not k.startswith(key + '/')}
    else:
        key = status.get('id', '')
        previous_context = {**records.get('', {}).get('context', {}), **records.get(key, {}).get('context', {})}
        records[key] = {'status': status, 'updated_at': now}
        if 'context' in report:
            context = validate_context(report['context'])
            if previous_context.get('title') and context.get('provider') != 'codex':
                context['title'] = previous_context['title']
            for field in ('cwd', 'origin', 'title'):
                if field not in context and field in previous_context:
                    context[field] = previous_context[field]
            if context.get('title') or context.get('cwd'):
                from .collector import display_name_from_parts, project_name
                short_id = (context.get('agent_id') or context.get('session_id') or '')[:8]
                context['display_name'] = display_name_from_parts(project_name(context.get('cwd')), context.get('title'), short_id, context.get('display_name') or '')
            records[key]['context'] = context
    item.update(seq=seq, records=list(records.values()))
    updated['revision'] += 1
    return validate_snapshot(updated)


def default_program_state_path() -> Path:
    return default_state_dir() / 'program-status.sqlite3'


class ProgramStore:
    """Atomic local accounting and durable snapshots; safe across UI/service processes."""
    def __init__(self, path: Path | None = None):
        self.path = path or default_program_state_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(self.path), timeout=2, check_same_thread=False)
        self.path.chmod(0o600)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS snapshots (sender TEXT, publisher TEXT, body TEXT NOT NULL, PRIMARY KEY(sender,publisher))')
        self.db.execute('CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS receipts (id TEXT PRIMARY KEY, result TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS scope_sequences (scope TEXT PRIMARY KEY, seq INTEGER NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS aliases (key TEXT PRIMARY KEY, scope TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS remote_aliases (sender TEXT, key TEXT, PRIMARY KEY(sender,key))')
        self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('publisher', ?)", (str(uuid.uuid4()),))
        self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('phone_revision', '0')")
        self.db.commit()
        self.publisher = self.db.execute("SELECT value FROM metadata WHERE key='publisher'").fetchone()[0]

    def local_snapshot(self) -> dict:
        with self.lock:
            row = self.db.execute("SELECT body FROM snapshots WHERE sender='local' AND publisher=?", (self.publisher,)).fetchone()
        return json.loads(row[0]) if row else {'v': 1, 'type': 'program_status_snapshot', 'publisher': self.publisher, 'revision': 0, 'scopes': {}}

    def submit(self, report: dict) -> dict:
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                ack = self._submit_report(report)
                self.db.commit()
                return ack
            except BaseException:
                self.db.rollback()
                raise

    def _submit_report(self, report: dict) -> dict:
        receipt = report.get('request_id')
        if receipt is not None:
            identifier(receipt, 'request_id')
        # Retrying the same request cannot increment the sequence or redisplay Done.
        request = encoded(report)
        if receipt:
            row = self.db.execute('SELECT result FROM receipts WHERE id=?', (receipt,)).fetchone()
            if row:
                old = json.loads(row[0])
                if old['request'] != request:
                    raise StatusError('request_id reused for a different report')
                return old['ack']
        if report.get('type') == 'program_status_batch':
            reports = report.get('reports')
            if type(report.get('v')) is not int or report['v'] != 1 or not isinstance(reports, list) or not 1 <= len(reports) <= 16:
                raise StatusError('Invalid status batch')
            if any(not isinstance(item, dict) or item.get('type') == 'program_status_batch' for item in reports):
                raise StatusError('Invalid status batch item')
            ack = {'ok': True, 'reports': [self._submit_report(item) for item in reports]}
        else:
            now = int(datetime.now(timezone.utc).timestamp() * 1000)
            current = self.local_snapshot()
            # Empty scopes have no visible state; keep ordering separately.
            # Done visibility is 20 minutes on every receiver.
            for scope, item in list(current['scopes'].items()):
                item['records'] = [r for r in item['records'] if r['status']['state'] != 'done' or now - r['updated_at'] <= 1200000]
                if not item['records']:
                    del current['scopes'][scope]
            scope = identifier(report.get('scope'))
            previous = self.db.execute('SELECT seq FROM scope_sequences WHERE scope=?', (scope,)).fetchone()
            if scope not in current['scopes'] and previous:
                current['scopes'][scope] = {'seq': previous[0], 'records': []}
            suppression_key = 'hook_suppressed:' + scope
            if report.get('_hook_suppressed') is True and report.get('legacy_key'):
                self.db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', (suppression_key, '1'))
            suppressed = report.get('legacy_key') and self.db.execute('SELECT 1 FROM metadata WHERE key=?', (suppression_key,)).fetchone()
            effective_report = {**report, 'type': 'scope_reset'} if suppressed else report
            snapshot = apply_report(current, effective_report, now)
            sequence = snapshot['scopes'][scope]['seq']
            self.db.execute('INSERT OR REPLACE INTO scope_sequences VALUES (?,?)', (scope, sequence))
            if not snapshot['scopes'][scope]['records']:
                del snapshot['scopes'][scope]
            self.db.execute('INSERT OR REPLACE INTO snapshots VALUES (?,?,?)', ('local', self.publisher, encoded(snapshot)))
            legacy = report.get('legacy_key')
            if isinstance(legacy, str):
                self.db.execute('INSERT OR REPLACE INTO aliases VALUES (?,?)', (text(legacy, 512, 'legacy_key'), report['scope']))
            ack = {'ok': True, 'seq': sequence, 'revision': snapshot['revision']}
            if snapshot['revision'] != current['revision']:
                self.db.execute("UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='phone_revision'")
        if receipt:
            self.db.execute('INSERT INTO receipts VALUES (?,?)', (receipt, json.dumps({'request': request, 'ack': ack})))
            self.db.execute('DELETE FROM receipts WHERE rowid NOT IN (SELECT rowid FROM receipts ORDER BY rowid DESC LIMIT 4096)')
        return ack

    def receive(self, sender: str, value: object) -> bool:
        snapshot = validate_snapshot(value)
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                row = self.db.execute('SELECT body FROM snapshots WHERE sender=? AND publisher=?', (sender, snapshot['publisher'])).fetchone()
                if row and json.loads(row[0])['revision'] >= snapshot['revision']:
                    self.db.commit()
                    return False
                for scope in snapshot['scopes'].values():
                    for record in scope['records']:
                        legacy = context_legacy_key(record.get('context', {}), sender)
                        if legacy:
                            self.db.execute('INSERT OR IGNORE INTO remote_aliases VALUES (?,?)', (sender, legacy))
                count = self.db.execute('SELECT count(*) FROM snapshots').fetchone()[0]
                if not row and count >= 64:
                    raise StatusError('Too many publishers')
                self.db.execute('INSERT OR REPLACE INTO snapshots VALUES (?,?,?)', (sender, snapshot['publisher'], encoded(snapshot)))
                self.db.execute("UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='phone_revision'")
                self.db.commit()
                return True
            except BaseException:
                self.db.rollback()
                raise

    def retain_senders(self, senders: set[str]) -> None:
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                existing = {row[0] for row in self.db.execute('SELECT DISTINCT sender FROM snapshots')}
                removed = existing - senders
                for sender in removed:
                    self.db.execute('DELETE FROM snapshots WHERE sender=?', (sender,))
                    self.db.execute('DELETE FROM remote_aliases WHERE sender=?', (sender,))
                if removed:
                    self.db.execute("UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='phone_revision'")
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def phone_snapshot(self) -> dict:
        with self.lock:
            # UI and daemon use different connections. Read records and revision
            # in one SQLite snapshot so a concurrent writer cannot mix versions.
            self.db.execute('BEGIN')
            try:
                rows = self.db.execute('SELECT sender,publisher,body FROM snapshots ORDER BY sender,publisher').fetchall()
                revision = int(self.db.execute("SELECT value FROM metadata WHERE key='phone_revision'").fetchone()[0])
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise
        scopes = {}
        for sender, publisher, body in rows:
            for scope, item in json.loads(body)['scopes'].items():
                key = hashlib.sha256(json.dumps([sender, publisher, scope]).encode()).hexdigest()
                scopes[key] = {'seq': item['seq'], 'records': [
                    {'status': r['status'], 'updated_at': r['updated_at']} for r in item['records']]}
        return validate_snapshot({'v': 1, 'type': 'program_status_snapshot',
            'publisher': self.publisher + '-phone', 'revision': revision, 'scopes': scopes})

    def statuses(self) -> tuple[AgentStatus, ...]:
        return tuple(status for status, _ in self.status_entries())

    def status_entries(self) -> tuple[tuple[AgentStatus, str | None], ...]:
        with self.lock:
            rows = self.db.execute('SELECT sender,publisher,body FROM snapshots ORDER BY sender,publisher').fetchall()
        result = []
        for sender, publisher, body in rows:
            for scope, item in json.loads(body)['scopes'].items():
                records = {r['status'].get('id', ''): r for r in item['records']}
                for key, record in sorted(records.items()):
                    status = record['status']
                    app = status.get('app')
                    parent = key
                    while not app and parent:
                        parent = parent.rsplit('/', 1)[0] if '/' in parent else ''
                        app = records.get(parent, {}).get('status', {}).get('app')
                    context = record.get('context', {})
                    mode = MODES[status['state']]
                    detail_mode = context.get('mode')
                    if mode == AgentMode.WORKING and detail_mode in {'tool_running', 'long_task_progress'}:
                        mode = AgentMode(detail_mode)
                    origin = context.get('origin')
                    if sender != 'local' and context.get('source_name'):
                        origin = f"{origin} · {context['source_name']}" if origin else context['source_name']
                    identity = 'program:' + hashlib.sha256(json.dumps([sender, publisher, scope, key]).encode()).hexdigest()
                    session = 'program:' + hashlib.sha256(json.dumps([sender, publisher, scope]).encode()).hexdigest()
                    item = AgentStatus(provider=context.get('provider') or app or 'program', agent_id=identity,
                        display_name=context.get('display_name') or status.get('title') or app or scope, mode=mode,
                        updated_at=datetime.fromtimestamp(record['updated_at'] / 1000, timezone.utc),
                        event_name=context.get('event_name') or 'ProgramStatus', session_id=context.get('session_id') or session,
                        cwd=context.get('cwd'), tool_name=context.get('tool_name'),
                        message=status.get('msg'), origin=origin or sender)
                    result.append((item, context_legacy_key(context, sender)))
        return tuple(result)

    def aliases(self) -> set[str]:
        with self.lock:
            return {r[0] for r in self.db.execute('SELECT key FROM aliases UNION SELECT key FROM remote_aliases')}

    def close(self):
        self.db.close()
