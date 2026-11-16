"""Recognize our encoded Windows observer without interpreting arbitrary scripts."""
import base64
import re

WINDOWS_PREFIX = 'powershell.exe -NoProfile -NonInteractive -EncodedCommand '
WINDOWS_MARKER = '# sidepulse hook_entry.py observer\n'


def observer_script(command):
    if isinstance(command, str) and command.startswith(WINDOWS_PREFIX) and len(command) <= 65536:
        try:
            script = base64.b64decode(command[len(WINDOWS_PREFIX):], validate=True).decode('utf-16le')
            if script.startswith(WINDOWS_MARKER):
                return script
        except (ValueError, UnicodeError):
            pass
    return command


def windows_log_path(command):
    script = observer_script(command)
    if script == command:
        return None
    match = re.search(r"'--log' '((?:[^']|'')*)'", script)
    return match.group(1).replace("''", "'") if match else None
