"""Desktop presentation of explicit statuses and isolated legacy hook compatibility.

This layer consumes normalized records; provider event translation belongs in
program_adapter. Compatibility projection preserves existing UI/device behavior
without changing the stored OSC state or third-party status semantics.
"""
from __future__ import annotations


class ProgramMonitor:
    """Keep existing hook presentation and add provider-independent explicit records."""
    def __init__(self, monitor, store):
        self.monitor, self.store = monitor, store

    def __getattr__(self, name):
        return getattr(self.monitor, name)

    def snapshot(self, include_stale=False):
        from dataclasses import replace
        from datetime import datetime, timezone
        from .collector import aggregate_status, status_counts_active
        legacy = self.monitor.snapshot(include_stale=include_stale)
        aliases = self.store.aliases()
        history = {s.agent_id: s for s in (*legacy.stale_statuses, *legacy.statuses)}
        statuses = [s for s in legacy.statuses if s.agent_id not in aliases]
        now = datetime.now(timezone.utc)
        grouped = {}
        expired_aliases = set()
        normalized_stale = []
        legacy_visible = {s.agent_id for s in legacy.statuses}
        for status, alias in self.store.status_entries():
            if status.mode.value == 'completed' and status.age_seconds(now) > 1200:
                if alias:
                    expired_aliases.add(alias)
                continue
            if alias:
                grouped.setdefault(alias, []).append(status)
            else:
                statuses.append(status)
        for alias, entries in grouped.items():
            # One UI row per hook agent, while the ledger retains individual waits.
            # An outstanding permission keeps that agent waiting through other tools.
            current = min(entries, key=lambda s: (
                0 if s.event_name == 'PermissionRequest' and s.mode.value == 'waiting_for_input' else 1,
                s.priority, -s.updated_at.timestamp()))
            previous = history.get(alias)
            if previous:
                # Normalized timestamps are milliseconds; legacy hooks retain microseconds.
                if int(previous.updated_at.timestamp() * 1000) > max(int(s.updated_at.timestamp() * 1000) for s in entries):
                    # A newer legacy event is a real fallback, not a duplicate.
                    current = previous
                else:
                    pending = any(s.event_name == 'PermissionRequest' and s.mode.value == 'waiting_for_input' for s in entries)
                    current = replace(current, mode=current.mode if pending else previous.mode, provider=previous.provider,
                        display_name=previous.display_name, session_id=previous.session_id,
                        cwd=previous.cwd, origin=previous.origin)
                if alias not in legacy_visible:
                    normalized_stale.append(replace(current, stale=True))
                    continue
            statuses.append(current)
        stale = [s for s in legacy.stale_statuses if s.agent_id not in aliases or s.agent_id in expired_aliases]
        stale.extend(normalized_stale)
        if any(status_counts_active(s) for s in statuses):
            stale.extend(replace(s, stale=True) for s in statuses if not status_counts_active(s))
            statuses = [s for s in statuses if status_counts_active(s)]
        statuses = tuple(sorted(statuses, key=lambda s: (s.priority, -s.updated_at.timestamp(), s.agent_id)))
        aggregate = aggregate_status(statuses, tuple(stale))
        active = len({(s.provider, s.session_id or s.agent_id, s.origin) for s in statuses if status_counts_active(s)})
        return replace(legacy, statuses=statuses, stale_statuses=tuple(stale), aggregate=replace(aggregate, active_count=active))
