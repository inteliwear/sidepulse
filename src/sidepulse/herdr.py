"""Translate Herdr agent state into local OSC-style status reports."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess

from .program_adapter import clean_text
from .program_status import CONTEXT_LIMITS, HERDR_EVENT, SEGMENT, StatusError, session_link_identity, text
from .program_transport import daemon_command, submit_status

PLUGIN_ID = 'sidepulse.status'
PLUGIN_DIR = Path(__file__).with_name('resources') / 'herdr'
STATES = {'working': 'working', 'blocked': 'blocked', 'idle': 'idle', 'done': 'done'}


def _digest(value: str) -> str:
    return hashlib.sha256(os.fsencode(value)).hexdigest()[:32]


def server_prefix(endpoint: str) -> str:
    return 'herdr-' + _digest(os.path.realpath(endpoint)) + '-'


def terminal_scope(prefix: str, terminal_id: str) -> str:
    return prefix + _digest(terminal_id)


def _request(endpoint: str, method: str, timeout: float, **params) -> dict:
    limit = 8 * 1024 * 1024
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        connection.connect(endpoint)
        connection.sendall(json.dumps({'id': 'sidepulse', 'method': method, 'params': params}).encode() + b'\n')
        with connection.makefile('rb') as stream:
            line = stream.readline(limit + 1)
    if not line or len(line) > limit:
        raise StatusError('Invalid Herdr socket response size')
    reply = json.loads(line)
    if not isinstance(reply, dict) or not isinstance(reply.get('result'), dict):
        raise StatusError(f'Herdr {method} did not return a result')
    return reply['result']


def current_pane_scope() -> str | None:
    """Resolve inherited pane IDs, including aliases after a cross-workspace move."""
    endpoint, pane_id = os.getenv('HERDR_SOCKET_PATH'), os.getenv('HERDR_PANE_ID')
    if not endpoint or not pane_id or os.name != 'posix':
        return None
    pane = _request(endpoint, 'pane.get', .2, pane_id=pane_id).get('pane')
    terminal_id = pane.get('terminal_id') if isinstance(pane, dict) else None
    if not isinstance(terminal_id, str) or not terminal_id:
        raise StatusError('Herdr pane has no terminal ID')
    return terminal_scope(server_prefix(endpoint), terminal_id)


def _agent_report(scope: str, agent: dict) -> dict:
    provider = clean_text(agent['agent'], CONTEXT_LIMITS['provider'])
    status = {'state': STATES[agent['agent_status']]}
    app = 'claude-code' if provider == 'claude' else provider
    if SEGMENT.fullmatch(app):
        status['app'] = app
    context = {'provider': provider, 'event_name': HERDR_EVENT, 'origin': 'Herdr'}
    title = agent.get('title') or agent.get('terminal_title_stripped')
    if isinstance(title, str) and title:
        status['title'] = clean_text(title, 192)
        context.update(title=clean_text(title, CONTEXT_LIMITS['title']), title_source='session')
    native = agent.get('agent_session')
    native_id = native.get('value') if isinstance(native, dict) and native.get('kind') == 'id' else None
    for field, value in (('session_id', native_id), ('cwd', agent.get('foreground_cwd') or agent.get('cwd'))):
        if isinstance(value, str) and value:
            try:
                context[field] = text(value, CONTEXT_LIMITS[field], field)
            except StatusError:
                pass
    return {'v': 1, 'type': 'program_status', 'scope': scope, 'status': status, 'context': context}


def _reconcile(endpoint: str) -> None:
    prefix = server_prefix(endpoint)
    view = daemon_command({'type': 'get_state'})
    rows = view['snapshot']['statuses'] + view['snapshot']['stale_statuses']
    existing = {}
    for row in rows:
        identity = session_link_identity(row)
        if identity and identity[0] == view['publisher'] and identity[1].startswith(prefix):
            existing[identity[1]] = row

    snapshot = _request(endpoint, 'session.snapshot', 1).get('snapshot')
    if not isinstance(snapshot, dict) or not all(isinstance(snapshot.get(key), list) for key in ('panes', 'agents')):
        raise StatusError('Invalid Herdr session snapshot')
    panes = {terminal_scope(prefix, pane['terminal_id']): pane for pane in snapshot['panes']}
    agents = {terminal_scope(prefix, agent['terminal_id']): agent for agent in snapshot['agents']
              if agent.get('agent')}

    event = json.loads(os.environ.get('HERDR_PLUGIN_EVENT_JSON', '{}')).get('data', {})
    ended = event.get('released') is True or event.get('type') == 'pane_exited'
    pane_ended = event.get('pane_id') if ended else None
    for scope, row in existing.items():
        pane = panes.get(scope)
        if scope in agents or (pane and pane['pane_id'] != pane_ended and row['event_name'] != HERDR_EVENT):
            continue
        # If a direct report arrived after get_state, leave it for the next sync.
        submit_status({'v': 1, 'type': 'program_status', 'scope': scope,
                       'seq': row['seq'] + 1, 'status': {'state': 'clear'}})
    for scope, agent in agents.items():
        if agent.get('agent_status') in STATES:
            submit_status(_agent_report(scope, agent))


def sync() -> None:
    import fcntl

    endpoint, state_dir = os.getenv('HERDR_SOCKET_PATH'), os.getenv('HERDR_PLUGIN_STATE_DIR')
    if not endpoint or not state_dir:
        raise StatusError('Run sync via the SidePulse Herdr plugin action')
    # Hook commands run concurrently. Read fresh snapshots in lock order.
    with (Path(state_dir) / (server_prefix(endpoint) + 'sync.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        _reconcile(endpoint)


def _cli(executable: str, *args: str) -> str:
    result = subprocess.run([executable, *args], capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise StatusError(result.stderr.strip() or result.stdout.strip() or f'Herdr exited with {result.returncode}')
    return result.stdout


def setup(*, uninstall: bool, dry_run: bool) -> int:
    if os.name != 'posix':
        raise StatusError('The SidePulse Herdr plugin requires macOS or Linux')
    if dry_run:
        action = 'unlink' if uninstall else 'link and sync'
        print(f'Would {action} the SidePulse Herdr plugin.')
        return 0

    executable = os.getenv('HERDR_BIN_PATH') or shutil.which('herdr')
    if not executable:
        raise StatusError('Herdr is unavailable on PATH')
    if uninstall:
        _cli(executable, 'plugin', 'unlink', PLUGIN_ID)
        print('SidePulse Herdr plugin removed.')
        return 0

    _cli(executable, 'plugin', 'link', str(PLUGIN_DIR))
    print('SidePulse Herdr plugin installed.')
    status = json.loads(_cli(executable, 'status', 'server', '--json'))
    if status.get('running'):
        _cli(executable, 'plugin', 'action', 'invoke', PLUGIN_ID + '.sync')
        print('Initial sync requested for the current Herdr session.')
    else:
        print('SidePulse will sync when Herdr starts.')
    return 0
