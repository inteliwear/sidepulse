"""Decode one OSC 7501 report at the producer boundary, before JSON transport."""
from __future__ import annotations

import base64
import binascii
import re

from .program_status import StatusError, text, validate_status

MAX_OSC_BYTES = 4096
_KEY = re.compile(r'[a-z]+\Z')
_VALUE = re.compile(r'[A-Za-z0-9_.,+/=\-]*\Z')
_LIMITS = {'msg': 2732, 'title': 256, 'app': 32, 'id': 128}


def parse_osc7501(line: str) -> dict:
    """Return normalized status fields; never scan or execute terminal output.

    Accept exactly one ESC ] 7501 ; ... sequence, optionally terminated by
    ESC \\ or BEL, and optionally followed by one LF or CRLF. The CLI argument
    or stdin EOF supplies the boundary when the OSC terminator is omitted.
    Malformed pairs are skipped, but invalid text or an exceeded limit rejects
    the entire report, including when a later duplicate would replace it.
    """
    if not isinstance(line, str):
        raise StatusError('OSC input must be text')
    if line.endswith('\r\n'):
        line = line[:-2]
    elif line.endswith('\n'):
        line = line[:-1]
    try:
        size = len(line.encode('utf-8'))
    except UnicodeError as exc:
        raise StatusError('OSC input must be UTF-8') from exc
    if size > MAX_OSC_BYTES:
        raise StatusError('OSC sequence exceeds 4096 bytes')
    if not line.startswith('\x1b]7501;'):
        raise StatusError('Expected one OSC 7501 sequence beginning with ESC ] 7501 ;')
    if line.endswith('\x1b\\'):
        body = line[7:-2]
    elif line.endswith('\x07'):
        body = line[7:-1]
    else:
        body = line[7:]
    if any(char in body for char in ('\x1b', '\x07', '\n', '\r')):
        raise StatusError('Supply exactly one OSC sequence, without terminal output')
    if body.strip() == '?' or body.lstrip().startswith('?:'):
        raise StatusError('OSC capability queries are not status reports')

    fields = {}
    for pair in body.split(':'):
        if '=' not in pair:
            continue
        key, value = (part.strip(' \t\r\n') for part in pair.split('=', 1))
        if len(key.encode('utf-8')) > 16:
            raise StatusError('OSC key exceeds 16 bytes')
        if key in _LIMITS and len(value.encode('utf-8')) > _LIMITS[key]:
            raise StatusError(f'OSC {key} exceeds its encoded limit')
        if key == 'id' and (len(value.split('/')) > 8 or any(len(p.encode('utf-8')) > 32 for p in value.split('/'))):
            raise StatusError('OSC id exceeds its segment or depth limit')
        if not _KEY.fullmatch(key) or not _VALUE.fullmatch(value):
            continue
        if key in {'msg', 'title'}:
            try:
                value = base64.b64decode(value + '=' * (-len(value) % 4), validate=True).decode('utf-8')
            except (binascii.Error, UnicodeError) as exc:
                raise StatusError(f'OSC {key} must be base64 UTF-8') from exc
            # Check every text pair before duplicate keys can overwrite it.
            value = text(value, 2048 if key == 'msg' else 192, key)
        elif key == 'progress':
            # Bound conversion too: invalid optional progress is simply absent.
            digits = value.lstrip('0') or '0'
            value = int(digits) if value and re.fullmatch(r'[0-9]{1,3}', digits) else None
        elif key not in {'state', 'id', 'app', 'kind'}:
            continue
        fields[key] = value
    return validate_status(fields)
