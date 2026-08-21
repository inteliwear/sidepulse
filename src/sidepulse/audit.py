from __future__ import annotations

import csv
import html
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .battery import BatterySnapshot
from .lid_sleep import MacSleepSnapshot
from .models import AgentStatus, HookEvent
from .providers import default_state_dir


STATUS_AUDIT_LOG_NAME = "event-status.jsonl"
STATUS_AUDIT_MAX_BYTES = 5 * 1024 * 1024
STATUS_AUDIT_BACKUP_COUNT = 3
STATUS_HISTORY_LOG_NAME = "status-history.jsonl"
RAW_PREVIEW_LIMIT = 2000
MESSAGE_PREVIEW_LIMIT = 240
AUDIT_COLUMNS = (
    "audited_at",
    "logged_at",
    "provider",
    "hook_event",
    "status",
    "status_label",
    "origin",
    "display_name",
    "session_id",
    "agent_id",
    "cwd",
    "tool_name",
    "message",
    "raw_preview",
)


def default_status_audit_log_path(home: Path | None = None) -> Path:
    return default_state_dir(home) / STATUS_AUDIT_LOG_NAME


def default_status_history_log_path(home: Path | None = None) -> Path:
    return default_state_dir(home) / STATUS_HISTORY_LOG_NAME


def append_status_audit_record(
    event: HookEvent,
    status: AgentStatus | None,
    *,
    path: Path | None = None,
) -> None:
    target = path or default_status_audit_log_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        rotate_status_audit_log(target)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    status_audit_record(event, status),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )
    except OSError:
        pass


def rotate_status_audit_log(
    path: Path,
    *,
    max_bytes: int = STATUS_AUDIT_MAX_BYTES,
    backup_count: int = STATUS_AUDIT_BACKUP_COUNT,
) -> bool:
    """Bound audit-log growth while retaining a few recent generations."""
    if max_bytes <= 0 or backup_count <= 0:
        return False
    try:
        if path.stat().st_size < max_bytes:
            return False
    except OSError:
        return False

    oldest = path.with_name(f"{path.name}.{backup_count}")
    oldest.unlink(missing_ok=True)
    for index in range(backup_count - 1, 0, -1):
        source = path.with_name(f"{path.name}.{index}")
        if source.exists():
            source.replace(path.with_name(f"{path.name}.{index + 1}"))
    path.replace(path.with_name(f"{path.name}.1"))
    return True


def append_status_history_record(
    record: dict[str, object],
    *,
    path: Path | None = None,
) -> None:
    target = path or default_status_history_log_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                record,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            )
            + "\n"
        )


def status_history_record(
    *,
    agent_mode: str,
    display_status: str,
    battery: BatterySnapshot | None,
    mac_sleep: MacSleepSnapshot | None,
    lid_closed: bool | None,
    keep_awake_requested: bool,
    keep_awake_active: bool,
    sleep_prevention_policy: str,
    sleep_prevention_battery_safeguard_active: bool,
    sleep_prevention_min_battery_percent: float | None,
    closed_lid_awake_requested: bool,
    closed_lid_awake_active: bool,
    recorded_at: datetime | None = None,
) -> dict[str, object]:
    at = recorded_at or datetime.now(timezone.utc)
    battery_percent = battery.percent if battery is not None else None
    adapter_power = battery.adapter_power if battery is not None else None
    return {
        "recorded_at": at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "agent_status": agent_mode,
        "display_status": display_status,
        "battery_level": battery_percent,
        "battery_charging": battery.is_charging if battery is not None else None,
        "battery_charged": battery.is_charged if battery is not None else None,
        "battery_present": battery.battery_present if battery is not None else None,
        "battery_power_watts": round(battery.battery_watts, 2) if battery is not None else None,
        "charger_connected": battery.is_plugged if battery is not None else None,
        "adapter_connected": battery.adapter_connected if battery is not None else None,
        "charger_power_watts": round(adapter_power, 2) if adapter_power is not None else None,
        "adapter_watts": battery.adapter_watts if battery is not None else None,
        "adapter_voltage": round(battery.adapter_voltage, 2) if battery is not None else None,
        "adapter_current": round(battery.adapter_current, 2) if battery is not None else None,
        "adapter_name": battery.adapter_name if battery is not None else "",
        "adapter_manufacturer": battery.adapter_manufacturer if battery is not None else "",
        "adapter_model": battery.adapter_model if battery is not None else "",
        "lid_closed": lid_closed,
        "lid_status": bool_label(lid_closed, true_label="closed", false_label="open"),
        "sidepulse_keep_awake_requested": keep_awake_requested,
        "sidepulse_keep_awake_active": keep_awake_active,
        "sleep_prevention_policy": sleep_prevention_policy,
        "sleep_prevention_battery_safeguard_active": sleep_prevention_battery_safeguard_active,
        "sleep_prevention_min_battery_percent": sleep_prevention_min_battery_percent,
        "sidepulse_closed_lid_awake_requested": closed_lid_awake_requested,
        "sidepulse_closed_lid_awake_active": closed_lid_awake_active,
        "mac_sleep_prevented": mac_sleep.sleep_prevented if mac_sleep is not None else None,
        "mac_sleep_disabled": mac_sleep.sleep_disabled if mac_sleep is not None else None,
        "mac_prevent_system_sleep": (
            mac_sleep.prevent_system_sleep if mac_sleep is not None else None
        ),
        "mac_prevent_user_idle_system_sleep": (
            mac_sleep.prevent_user_idle_system_sleep if mac_sleep is not None else None
        ),
        "mac_prevent_user_idle_display_sleep": (
            mac_sleep.prevent_user_idle_display_sleep if mac_sleep is not None else None
        ),
        "mac_user_is_active": mac_sleep.user_is_active if mac_sleep is not None else None,
        "mac_sleep_status": sleep_status_label(mac_sleep),
        "mac_sleep_error": mac_sleep.error if mac_sleep is not None and mac_sleep.error else "",
    }


def status_audit_record(event: HookEvent, status: AgentStatus | None) -> dict[str, str]:
    return {
        "audited_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "logged_at": event.logged_at.isoformat(),
        "provider": event.provider,
        "hook_event": event.event_name,
        "status": status.mode.value if status is not None else "",
        "status_label": status.mode_label if status is not None else "",
        "origin": status.origin if status is not None and status.origin else event.origin or "",
        "display_name": status.display_name if status is not None else "",
        "session_id": event.session_id or "",
        "agent_id": event.status_key,
        "cwd": event.cwd or "",
        "tool_name": event.tool_name or "",
        "message": truncate_preview(event.message or raw_message(event.raw), MESSAGE_PREVIEW_LIMIT),
        "raw_preview": truncate_preview(json_preview(event.raw), RAW_PREVIEW_LIMIT),
    }


def raw_message(raw: dict[str, Any]) -> str:
    for key in ("message", "last_assistant_message", "prompt"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def bool_label(
    value: bool | None,
    *,
    true_label: str = "true",
    false_label: str = "false",
) -> str:
    if value is True:
        return true_label
    if value is False:
        return false_label
    return "unknown"


def sleep_status_label(mac_sleep: MacSleepSnapshot | None) -> str:
    if mac_sleep is None:
        return "unknown"
    prevented = mac_sleep.sleep_prevented
    if prevented is True:
        return "prevented"
    if prevented is False:
        return "allowed"
    return "unknown"


def json_preview(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except TypeError:
        return str(value)


def truncate_preview(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def read_status_audit_records(path: Path | None = None) -> list[dict[str, str]]:
    source = path or default_status_audit_log_path()
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    records: list[dict[str, str]] = []
    for line in lines:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            records.append({column: str(obj.get(column, "")) for column in AUDIT_COLUMNS})
    return records


def read_status_history_records(
    path: Path | None = None,
    *,
    limit: int | None = None,
) -> list[dict[str, object]]:
    source = path or default_status_history_log_path()
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    if limit is not None and limit > 0:
        lines = lines[-limit:]

    records: list[dict[str, object]] = []
    for line in lines:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records


def export_status_audit_csv(
    destination: Path,
    *,
    source: Path | None = None,
) -> int:
    records = read_status_audit_records(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=AUDIT_COLUMNS)
        writer.writeheader()
        writer.writerows(records)
    return len(records)


def export_status_audit_html(
    destination: Path,
    *,
    source: Path | None = None,
) -> int:
    records = read_status_audit_records(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(table_row(record) for record in records)
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    destination.write_text(
        "\n".join(
            [
                "<!doctype html>",
                '<meta charset="utf-8">',
                "<title>SidePulse Agent Debug Log</title>",
                "<style>",
                "body{font:14px -apple-system,BlinkMacSystemFont,sans-serif;margin:24px;color:#1d1d1f}",
                "h1{font-size:22px;margin:0 0 4px}",
                "p{color:#6e6e73;margin:0 0 20px}",
                "table{border-collapse:collapse;width:100%;table-layout:fixed}",
                "th,td{border-bottom:1px solid #ddd;padding:7px 8px;text-align:left;vertical-align:top;word-wrap:break-word}",
                "th{position:sticky;top:0;background:#fff;font-weight:600}",
                "td.raw{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}",
                "</style>",
                "<h1>SidePulse Agent Debug Log</h1>",
                f"<p>{len(records)} events exported {html.escape(generated_at)}</p>",
                "<table>",
                "<thead><tr>",
                "".join(f"<th>{html.escape(column)}</th>" for column in AUDIT_COLUMNS),
                "</tr></thead>",
                f"<tbody>{body}</tbody>",
                "</table>",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return len(records)


def table_row(record: dict[str, str]) -> str:
    cells = []
    for column in AUDIT_COLUMNS:
        css_class = ' class="raw"' if column == "raw_preview" else ""
        cells.append(f"<td{css_class}>{html.escape(record.get(column, ''))}</td>")
    return "<tr>" + "".join(cells) + "</tr>"
