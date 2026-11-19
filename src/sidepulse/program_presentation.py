"""Desktop presentation derived entirely from accounted OSC-style JSON state."""
from __future__ import annotations


class ProgramMonitor:
    def __init__(self, store):
        self.store = store

    def snapshot(self, include_stale=False):
        from dataclasses import replace
        from datetime import datetime, timezone
        from .collector import COMPLETED_VISIBLE_SECONDS, MonitorSnapshot, aggregate_status, status_counts_active
        from .models import AgentMode
        from .settings import load_settings

        now = datetime.now(timezone.utc)
        timeout = load_settings().session_timeout_seconds
        rows = self.store.statuses(
            inactive_before_ms=(now.timestamp() - timeout) * 1000, include_closed=True,
        )
        statuses = []
        stale = []
        has_active = any(status_counts_active(row) for row in rows)
        for row in rows:
            if row.mode != AgentMode.COMPLETED:
                statuses.append(row)
                continue
            age = row.age_seconds(now)
            if not row.stale and not has_active and age <= COMPLETED_VISIBLE_SECONDS:
                statuses.append(row)
            elif age <= timeout:
                stale.append(replace(row, stale=True))

        statuses.sort(key=lambda row: (row.priority, -row.updated_at.timestamp(), row.agent_id))
        visible = tuple(statuses)
        recent = tuple(stale)
        return MonitorSnapshot(aggregate_status(visible, recent), visible, recent, (), now)


class DaemonMonitor:
    """UI/CLI view of daemon-owned state; no local session accounting."""
    def __init__(self, path=None):
        self.path = path
        self.output_errors = {}
        self.runtime = {}
        self.devices = []
        self.outputs_enabled = True

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
                      'session_id', 'cwd', 'tool_name', 'message', 'origin', 'session_link', 't3_link')}
            return AgentStatus(**fields, mode=AgentMode(row['mode']), updated_at=parse_datetime(row['updated_at']),
                               stale=row.get('stale', False), seq=row.get('seq'))
        aggregate = value['aggregate']
        self.output_errors = reply.get('output_errors', {})
        self.runtime = reply.get('runtime', {})
        from .device_writer import DeviceCandidate
        from pathlib import Path
        self.devices = [DeviceCandidate(**{**row, 'root': Path(row['root']), 'target': Path(row['target'])}) for row in reply.get('devices', [])]
        self.outputs_enabled = reply.get('outputs_enabled', True)
        return MonitorSnapshot(AggregateStatus(AgentMode(aggregate['mode']), aggregate['active_count'],
            aggregate['stale_count'], status(aggregate.get('representative'))),
            tuple(status(row) for row in value['statuses']), tuple(status(row) for row in value['stale_statuses']),
            (), parse_datetime(value['collected_at']))
