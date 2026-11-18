"""Producer-side hook translation. Receivers never import this provider adapter."""
from __future__ import annotations
import hashlib
import json
import uuid
import socket
from .program_status import CONTEXT_LIMITS, MAX_OUTBOUND_MESSAGE_BYTES, hook_scope, validate_status


def clean_text(value: str, limit: int) -> str:
    value = ''.join(' ' if ord(c) < 32 or 127 <= ord(c) <= 159 else c for c in value)
    return value.encode('utf-8', errors='replace')[:limit].decode('utf-8', errors='ignore')


def provider_report(provider: str, scope: str, status: dict, context: dict) -> dict:
    """Encode provider status and presentation metadata for the shared daemon."""
    status = {**status, 'app': 'claude-code' if provider == 'claude' else provider}
    context = {'provider': provider, 'source_name': socket.gethostname(), **context}
    if context.get('title'):
        status['title'] = clean_text(context['title'], 192)
    context = {key: value for key, value in context.items() if value is not None}
    for field in ('origin', 'display_name', 'tool_name', 'source_name', 'title'):
        if field in context:
            context[field] = clean_text(context[field], CONTEXT_LIMITS[field])
    return {'v': 1, 'type': 'program_status', 'scope': scope,
            'status': validate_status(status), 'context': context}


def hook_report(provider: str, line: dict) -> dict | None:
    from .collector import (
        StatusMetadata,
        codex_question_tool,
        explicit_mode_for_record,
        project_dir_from_event,
        prompt_title_from_event,
        provider_session_title,
        should_ignore_record,
        status_from_event,
    )
    from .providers import parse_log_line
    record = parse_log_line(provider, json.dumps(line, ensure_ascii=False))
    if record is None:
        return None
    scope = hook_scope(provider, record.session_id or record.status_key)
    base = {'v': 1, 'type': 'program_status', 'scope': scope,
            'request_id': str(uuid.uuid4())}
    session_title = provider_session_title(record)
    title = session_title or prompt_title_from_event(record)
    metadata = StatusMetadata(cwd=project_dir_from_event(record), title=title)
    interpreted = status_from_event(record, metadata)
    if interpreted is None:
        if should_ignore_record(record, metadata):
            return {**base, 'status': {'state': 'clear'}, '_hook_suppressed': True}
        return None
    mode = interpreted.mode.value
    states = {'idle_ready': 'idle', 'working': 'working', 'tool_running': 'working',
              'long_task_progress': 'working', 'waiting_for_input': 'blocked',
              'blocked_error': 'error', 'completed': 'done'}
    if mode not in states:
        return None
    status = {'state': states[mode]}
    if record.agent_id:
        status['id'] = 'agents/' + hashlib.sha256(record.agent_id.encode()).hexdigest()[:32]
    if record.event_name == 'PermissionRequest':
        status['id'] = wait_id(record)
    if status['state'] == 'blocked':
        status['kind'] = 'permission' if 'permission' in record.event_name.lower() else 'question'
    if interpreted.message:
        status['msg'] = clean_text(interpreted.message, MAX_OUTBOUND_MESSAGE_BYTES)
    # Presentation metadata travels as JSON context outside the OSC status fields.
    context = {
        'session_id': record.session_id, 'agent_id': record.agent_id,
        'cwd': interpreted.cwd, 'origin': interpreted.origin, 't3_link': record.t3_link,
        'display_name': interpreted.display_name, 'tool_name': record.tool_name,
        'mode': interpreted.mode.value, 'event_name': record.event_name,
        'title': title, 'title_source': 'session' if session_title else 'prompt',
    }
    result = provider_report(provider, scope, status, context)
    result['request_id'] = base['request_id']
    from .herdr import current_pane_scope
    if pane_scope := current_pane_scope():
        result.update(scope=pane_scope, _hook_scope=scope)
    if record.event_name == 'Notification':
        result['unless_state'] = 'done'
    if (provider == 'codex' and not codex_question_tool(record)
            and record.event_name not in {'UserPromptSubmit', 'Stop', 'SubagentStop', 'Interrupt', 'SessionEnd', 'SessionStart'}
            and mode != 'blocked_error' and explicit_mode_for_record(record) is None):
        # Ordinary tools can continue after an async question is queued. The
        # daemon atomically preserves an existing question wait for this scope.
        result['unless_kind'] = 'question'
    return result


def wait_id(record, *, use_invocation: bool = True) -> str:
    prefix = 'agents/' + hashlib.sha256(record.agent_id.encode()).hexdigest()[:32] + '/' if record.agent_id else ''
    invocation = record.raw.get('tool_use_id') if use_invocation else None
    identity = ['tool_use_id', invocation] if isinstance(invocation, str) and invocation else [record.tool_name, record.raw.get('tool_input')]
    signature = json.dumps(identity, sort_keys=True, ensure_ascii=False)
    return prefix + 'permissions/' + hashlib.sha256(signature.encode()).hexdigest()[:32]
