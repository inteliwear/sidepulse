#!/usr/bin/env python3
"""Run real Windows agents and compare independently labeled VM screenshots.

Authentication must finish before recording. Screenshots come from computer
control, never from model transcripts or a regenerated terminal view.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('windows_probe', HERE / 'probe.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
MODES = {'idle_ready', 'working', 'tool_running', 'waiting_for_input', 'completed', 'blocked_error'}


def records(path, offset):
    if not path.exists():
        return []
    rows = []
    with path.open('rb') as stream:
        stream.seek(offset)
        for line in stream:
            try:
                value = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if isinstance(value, dict):
                if isinstance(value.get('event'), dict):
                    value = {**value, **value['event']}
                rows.append(value)
    return rows


def same_path(left, right):
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def run(args):
    probe.guard('vm')
    home = Path.home().resolve()
    workspace = args.workspace.resolve()
    owned = home / 'SidePulseAgentTests'
    if owned not in workspace.parents or not workspace.is_dir():
        raise RuntimeError('Use a prepared workspace beneath the disposable SidePulseAgentTests directory')
    output = args.output.resolve()
    if owned not in output.parents or workspace == output or workspace in output.parents or output in workspace.parents:
        raise RuntimeError('Evidence must stay in a separate SidePulseAgentTests directory outside the agent workspace')
    args.output.mkdir(parents=True, exist_ok=False)
    from sidepulse.providers import detect_log_path
    from sidepulse.program_transport import daemon_command
    from sidepulse.terminal_wrap import run_wrapped
    hook = detect_log_path(args.provider)
    offset = hook.stat().st_size if hook.exists() else 0
    version = subprocess.check_output([str(args.agent), '--version'], text=True, timeout=20).strip()
    data = {'schema': 1, 'run_id': uuid.uuid4().hex, 'provider': args.provider,
            'workspace': str(workspace), 'version': version,
            'capture_kind': 'independent VM screenshots plus installed-daemon observations',
            'started_at': time.time(), 'frames': [], 'exit_code': None, 'error': None}
    data['agent_sha256'] = hashlib.sha256(args.agent.read_bytes()).hexdigest()
    stop = threading.Event()

    def observe():
        with (args.output / 'frames.jsonl').open('w', encoding='utf-8') as stream:
            while not stop.is_set():
                frame = {'id': len(data['frames']), 'before': time.time()}
                try:
                    reply = daemon_command({'type': 'get_state'}, timeout=.5)
                    sessions = {r['session_id'] for r in records(hook, offset)
                                if r.get('session_id') and same_path(r.get('cwd', ''), workspace)}
                    rows = reply['snapshot']['statuses'] + reply['snapshot'].get('stale_statuses', [])
                    frame.update(snapshot=reply, sessions=sorted(sessions),
                        detected=sorted({r['mode'] for r in rows if r.get('provider') == args.provider
                                         and r.get('session_id') in sessions}))
                except Exception as exc:
                    frame.update(error=str(exc), detected=[])
                frame['after'] = time.time()
                data['frames'].append(frame)
                stream.write(json.dumps(frame, ensure_ascii=False) + '\n')
                stream.flush()
                if time.time() - data['started_at'] > args.seconds:
                    data['error'] = 'Recording exceeded the allowed duration; no successful capture is claimed'
                stop.wait(.25)

    observer = threading.Thread(target=observe, daemon=True)
    observer.start()
    old_cwd = Path.cwd()
    try:
        os.chdir(workspace)
        data['exit_code'] = run_wrapped([str(args.agent), *args.agent_args])
    except Exception as exc:
        data['error'] = str(exc)
        raise
    finally:
        os.chdir(old_cwd)
        stop.set()
        observer.join(3)
        data['finished_at'] = time.time()
        events = records(hook, offset)
        (args.output / 'hook-events.json').write_text(json.dumps(events, indent=2), encoding='utf-8')
        own = [r for r in events if r.get('session_id') and same_path(r.get('cwd', ''), workspace)]
        names = [r.get('hook_event_name') or r.get('event_name') or r.get('event') for r in own]
        event_names = {name for name in names if isinstance(name, str)}
        try:
            result = json.loads((workspace / 'result.json').read_text(encoding='utf-8-sig'))
            token = (workspace / 'fixture.txt').read_text(encoding='utf-8').strip()
            data['live_checks'] = {'fresh_session_hook_events': bool(own),
                'real_tool_events': {'PreToolUse', 'PostToolUse'} <= event_names,
                'fixture_token_from_file': bool(token) and result.get('token') == token,
                'answered_question': result.get('choice') == 'blue'}
        except (OSError, ValueError, AttributeError):
            data['live_checks'] = {'fixture_result': False}
        (args.output / 'recording.json').write_text(json.dumps(data, indent=2), encoding='utf-8')
    return int(bool(data['error']) or data['exit_code'] != 0)


def review(directory, labels):
    data = json.loads((directory / 'recording.json').read_text())
    if labels.get('run_id') != data['run_id']:
        raise ValueError('Screenshot labels belong to another run')
    comparisons = []
    for label in labels.get('screenshots', []):
        if label['expected'] not in MODES:
            raise ValueError('Unknown expected mode')
        path = (directory / label['image']).resolve()
        if directory.resolve() not in path.parents or path.suffix.lower() not in {'.png', '.jpg', '.jpeg'}:
            raise ValueError('Screenshot must be a PNG or JPEG inside this recording')
        image = path.read_bytes() if path.is_file() else b''
        native_image = (image.startswith(b'\x89PNG\r\n\x1a\n') if path.suffix.lower() == '.png'
                        else image.startswith(b'\xff\xd8\xff') and image.endswith(b'\xff\xd9'))
        intact = native_image and hashlib.sha256(image).hexdigest() == label['sha256']
        at = label['at']
        before = [f for f in data['frames'] if f['after'] <= at and at - f['after'] <= 2]
        after = [f for f in data['frames'] if f['before'] >= at and f['before'] - at <= 2]
        adjacent = ([before[-1]] if before else []) + ([after[0]] if after else [])
        matches = (intact and len(adjacent) == 2 and
                   all(not f.get('error') and label['expected'] in f['detected'] for f in adjacent))
        comparisons.append({**label, 'matches': matches,
                            'frames': [f['id'] for f in adjacent],
                            'detected': [f['detected'] for f in adjacent]})
    covered = {r['expected'] for r in comparisons}
    missing = sorted({'working', 'waiting_for_input', 'completed'} - covered)
    live = data.get('live_checks', {})
    observed_defect = labels.get('observed_defect')
    status = ('failed' if observed_defect or data.get('error') or data.get('exit_code') != 0 or not live or not all(live.values()) or any(not c['matches'] for c in comparisons)
              else 'incomplete' if not comparisons or missing else 'passed')
    result = {'run_id': data['run_id'], 'provider': data['provider'], 'status': status,
              'comparisons': comparisons, 'missing_states': missing,
              'live_checks': live,
              'observed_defect': observed_defect,
              'real_agent': True, 'physical_usb_and_leds': 'not_tested'}
    (directory / 'visual-result.json').write_text(json.dumps(result, indent=2) + '\n')
    entries = ''.join('<section><h2>' + html.escape(c['label']) + '</h2><p>Expected: ' +
                      html.escape(c['expected']) + ' · Detected: ' + html.escape(str(c['detected'])) +
                      ' · Match: ' + str(c['matches']) + '</p><img src="' + html.escape(c['image'], quote=True) +
                      '" alt="' + html.escape(c['label'], quote=True) + '"></section>' for c in comparisons)
    (directory / 'review.html').write_text('''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width"><title>Windows real-agent screen review</title>
<style>body{max-width:1200px;margin:40px auto;padding:20px;background:#10151c;color:#e4ebf4;font:17px/1.6 system-ui}img{max-width:100%}section{margin:40px 0}a{color:#98d9ff}</style>
<h1>Windows real-agent screen review</h1><p>Independent VM screenshot labels compared with the installed daemon immediately before and after each screenshot. Authentication precedes recording. Physical USB and LEDs are unverified.</p><p>''' + html.escape(status) + ' · ' + html.escape(data['version']) + '</p>' + entries + '</html>')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    rec = sub.add_parser('run')
    rec.add_argument('provider', choices=['claude', 'codex'])
    rec.add_argument('--workspace', type=Path, required=True)
    rec.add_argument('--output', type=Path, required=True)
    rec.add_argument('--agent', type=Path, required=True)
    rec.add_argument('--seconds', type=int, default=900)
    rev = sub.add_parser('review')
    rev.add_argument('directory', type=Path)
    rev.add_argument('labels', type=Path)
    argv = sys.argv[1:]
    split = argv.index('--') if '--' in argv else len(argv)
    args = parser.parse_args(argv[:split])
    if args.command == 'run':
        args.agent_args = argv[split + 1:]
        if not 10 <= args.seconds <= 1800:
            parser.error('Use a 10–1800 second recording budget')
        return run(args)
    if split != len(argv):
        parser.error('Agent arguments apply only to run')
    result = review(args.directory, json.loads(args.labels.read_text()))
    print(json.dumps(result, indent=2))
    return int(result['status'] != 'passed')


if __name__ == '__main__':
    raise SystemExit(main())
