"""Producer-side recovery of Codex's pending plan review from its own rollout."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat

MAX_TRANSCRIPT_BYTES = 2 * 1024 * 1024
SESSION_ID = re.compile(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}')


def is_proposed_plan(text: object) -> bool:
    if not isinstance(text, str):
        return False
    # A quoted code example is not the structured plan rendered by Codex.
    visible = []
    fence = None
    for line in text.splitlines():
        marker = re.match(r'^\s*(`{3,}|~{3,})', line)
        if marker:
            value = marker.group(1)
            if fence is None:
                fence = value
            elif value[0] == fence[0] and len(value) >= len(fence):
                fence = None
            continue
        if fence is None:
            visible.append(line)
    match = re.search(r'^\s*<proposed_plan>\s*\n(.+?)\n\s*</proposed_plan>\s*$',
                      '\n'.join(visible), flags=re.MULTILINE | re.DOTALL)
    return bool(match and match.group(1).strip())


def pending_plan_review(raw: dict, session_id: str | None, turn_id: str | None) -> bool:
    """Require the matching current Plan-mode turn, never just an empty Stop.

    Only a bounded tail of a regular rollout file is read. Missing, truncated,
    or unsupported evidence leaves the normal completion classification intact.
    No transcript contents are sent to receivers.
    """
    if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id) or not turn_id:
        return False
    message = raw.get('last_assistant_message')
    if isinstance(message, str) and message.strip() and not is_proposed_plan(message):
        return False
    name = raw.get('transcript_path')
    if not isinstance(name, str):
        return False
    path = Path(name)
    if not path.is_absolute() or not path.name.endswith(session_id + '.jsonl'):
        return False
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0))
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                return False
            offset = max(0, info.st_size - MAX_TRANSCRIPT_BYTES)
            stream.seek(offset)
            data = stream.read(MAX_TRANSCRIPT_BYTES)
        if offset:
            data = data.partition(b'\n')[2]  # Discard a potentially partial first row.
    except (OSError, ValueError):
        return False

    current_turn = mode = final = None
    aborted = False
    for line in data.splitlines():
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(row, dict) or not isinstance(row.get('payload'), dict):
            continue
        payload = row['payload']
        if row.get('type') == 'turn_context':
            current_turn = payload.get('turn_id')
            collaboration = payload.get('collaboration_mode')
            mode = collaboration.get('mode') if isinstance(collaboration, dict) else None
            final = None
            aborted = False
        elif current_turn == turn_id and mode == 'plan':
            if row.get('type') == 'response_item' and payload.get('type') == 'message':
                if payload.get('role') == 'user':
                    final = None
                elif payload.get('role') == 'assistant' and payload.get('phase') in {'final', 'final_answer'}:
                    metadata = payload.get('internal_chat_message_metadata_passthrough')
                    if isinstance(metadata, dict) and metadata.get('turn_id', turn_id) != turn_id:
                        final = None
                        continue
                    content = payload.get('content')
                    if isinstance(content, list):
                        final = '\n'.join(item['text'] for item in content
                                          if isinstance(item, dict) and isinstance(item.get('text'), str))
                    else:
                        final = content
            elif row.get('type') == 'event_msg' and payload.get('type') in {'turn_aborted', 'task_aborted'}:
                final = None
                aborted = True
    candidate = message if isinstance(message, str) and message.strip() else final
    return current_turn == turn_id and mode == 'plan' and not aborted and is_proposed_plan(candidate)
