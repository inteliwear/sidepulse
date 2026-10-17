"""Desktop presentation derived entirely from accounted OSC-style JSON state."""
from __future__ import annotations


class ProgramMonitor:
    def __init__(self, store):
        self.store = store

    def snapshot(self, include_stale=False):
        from dataclasses import replace
        from datetime import datetime, timezone
        from .collector import MonitorSnapshot, aggregate_status, status_counts_active
        now = datetime.now(timezone.utc)
        statuses = [status for status in self.store.statuses()
                    if status.mode.value != 'completed' or status.age_seconds(now) <= 1200]
        stale = []
        if any(status_counts_active(s) for s in statuses):
            stale = [replace(s, stale=True) for s in statuses if not status_counts_active(s)]
            statuses = [s for s in statuses if status_counts_active(s)]
        statuses = tuple(sorted(statuses, key=lambda s: (s.priority, -s.updated_at.timestamp(), s.agent_id)))
        aggregate = aggregate_status(statuses, tuple(stale))
        active = sum(status_counts_active(s) for s in statuses)
        return MonitorSnapshot(replace(aggregate, active_count=active), statuses, tuple(stale), (), now)


class DaemonMonitor:
    """UI/CLI view of daemon-owned state; no local session accounting."""
    def __init__(self, path=None):
        self.path = path
        self.output_errors = {}

    def snapshot(self, include_stale=False):
        from .program_transport import daemon_command
        from .collector import MonitorSnapshot
        from .models import AgentStatus, AgentMode, AggregateStatus, parse_datetime
        reply = daemon_command({'type': 'get_state'}, path=self.path)
        value = reply['snapshot']
        def status(row):
            if row is None:
                return None
            fields = {key: row.get(key) for key in ('provider', 'agent_id', 'display_name', 'event_name',
                      'session_id', 'cwd', 'tool_name', 'message', 'origin')}
            return AgentStatus(**fields, mode=AgentMode(row['mode']), updated_at=parse_datetime(row['updated_at']),
                               stale=row.get('stale', False))
        aggregate = value['aggregate']
        self.output_errors = reply.get('output_errors', {})
        return MonitorSnapshot(AggregateStatus(AgentMode(aggregate['mode']), aggregate['active_count'],
            aggregate['stale_count'], status(aggregate.get('representative'))),
            tuple(status(row) for row in value['statuses']), tuple(status(row) for row in value['stale_statuses']),
            (), parse_datetime(value['collected_at']))
