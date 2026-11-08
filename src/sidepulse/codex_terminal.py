"""Reconcile Codex's native OSC dialog-close signal with its hook plan wait."""
from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import time
import urllib.parse
import uuid


def identity(row):
    link = urllib.parse.urlsplit(row.get('session_link', ''))
    if link.scheme != 'sidepulse' or link.netloc != 'sessions':
        return None
    query = urllib.parse.parse_qs(link.query)
    if len(query.get('publisher', [])) != 1 or len(query.get('scope', [])) != 1:
        return None
    return query['publisher'][0], query['scope'][0]


class CodexTerminal:
    def __init__(self, command, scope, *, query=None):
        self.enabled = bool(command) and Path(command[0]).name.lower() in {'codex', 'codex.exe'}
        self.scope = scope
        self.cwd = os.path.normcase(os.path.abspath(os.getcwd()))
        # Daemon timestamps have millisecond precision too.
        self.started_at = int(time.time() * 1000) / 1000
        self.query = query

    def dismiss_plan(self, submit):
        if not self.enabled:
            return
        try:
            if self.query is None:
                from .program_transport import daemon_command
                query = daemon_command
            else:
                query = self.query
            rows = query({'type': 'get_state'}, timeout=.2)['snapshot']['statuses']
            # Our unguessable per-invocation OSC scope identifies this daemon's
            # local publisher. Never rewrite a status forwarded by another host.
            publishers = {key[0] for row in rows if (key := identity(row)) and key[1] == self.scope}
            if len(publishers) != 1:
                return
            roots = []
            for row in rows:
                key = identity(row)
                if (key is None or key[0] not in publishers or row.get('provider') != 'codex'
                        or row.get('event_name') == 'ProgramStatus' or not row.get('cwd')
                        or os.path.normcase(os.path.abspath(row['cwd'])) != self.cwd):
                    continue
                updated = datetime.fromisoformat(row['updated_at']).timestamp()
                if updated >= self.started_at:
                    roots.append((row, key[1]))
            # Shared directories or concurrent sessions are ambiguous: leave
            # their hook states alone instead of guessing which picker closed.
            if len(roots) != 1:
                return
            row, scope = roots[0]
            if row.get('event_name') != 'Stop' or row.get('mode') != 'waiting_for_input':
                return
            # An ordinary assistant question is still waiting at an idle
            # composer. Only the null-Stop plan recovery or a structured plan
            # may be reconciled with the picker closing.
            from .codex_plan import is_proposed_plan
            if row.get('message') and not is_proposed_plan(row['message']):
                return
            seq = row.get('seq')
            if type(seq) is not int or seq < 1:
                return
            submit({'v': 1, 'type': 'program_status', 'scope': scope,
                    'seq': seq + 1, 'request_id': str(uuid.uuid4()),
                    'status': {'state': 'done', 'app': 'codex'},
                    'context': {'provider': 'codex', 'session_id': row['session_id'],
                                'cwd': row['cwd'], 'mode': 'completed',
                                'event_name': 'PlanDismissed'}}, timeout=.2)
            # The explicit next sequence is checked atomically by the daemon.
            # A newer hook makes this stale dismissal fail without changing it.
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
