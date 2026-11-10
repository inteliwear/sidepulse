#!/usr/bin/env python3
"""Record real tmux terminal cells beside installed-daemon observations.

These are terminal-cell captures, not screenshots or regenerated agent logs.
Authenticate before recording. Independent labels are required for a pass;
hook replays cannot supply evidence for a real-agent run.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import importlib.util
import json
from pathlib import Path
import subprocess
import time
import uuid

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('linux_checks', HERE / 'guest.py')
guest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guest)
MODES = {'idle_ready', 'working', 'tool_running', 'waiting_for_input', 'completed', 'blocked_error'}


def review(data, labels, directory):
    if labels.get('run_id') != data['run_id']:
        raise ValueError('Labels belong to another recording')
    frames = {str(frame['id']): frame for frame in data['frames']}
    comparisons = []
    for key, expected in labels.get('labels', {}).items():
        if key not in frames or expected not in MODES:
            raise ValueError('Unknown frame or expected mode')
        frame = frames[key]
        path = (directory / frame['screen']).resolve()
        intact = (directory.resolve() in path.parents and path.is_file() and
                  hashlib.sha256(path.read_bytes()).hexdigest() == frame['screen_sha256'])
        comparisons.append({'frame': key, 'expected': expected, 'detected': frame['detected'],
                            'matches': intact and expected in frame['detected']})
    covered = {item['expected'] for item in comparisons}
    missing = {'working', 'waiting_for_input', 'completed'} - covered
    status = ('failed' if data.get('error') or any(not r['matches'] for r in comparisons)
              else 'incomplete' if missing or not comparisons else 'passed')
    return {'run_id': data['run_id'], 'provider': data.get('provider'), 'reviewed_at': time.time(),
            'status': status, 'comparisons': comparisons, 'missing_states': sorted(missing),
            'unreviewed_frames': len(frames) - len(comparisons),
            'capture_kind': 'actual terminal cells; pixel screenshots are separate',
            'physical_leds': 'not_tested'}


def render(data, directory, result=None):
    entries = []
    for frame in data['frames']:
        text = (directory / frame['screen']).read_text()
        entries.append(f'<details><summary>Frame {frame["id"]} · {frame["t"]:.1f}s · '
                       f'{html.escape(", ".join(frame["detected"]))}</summary><pre>{html.escape(text)}</pre></details>')
    (directory / 'review.html').write_text('''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width"><title>Linux real-agent terminal review</title>
<style>body{background:#10151c;color:#e4ebf4;font:16px/1.5 system-ui;margin:32px}pre{overflow:auto;font:13px/1.3 monospace;background:#20252c;padding:16px}details{margin:12px 0}a{color:#98d9ff}</style>
<h1>Linux real-agent terminal review</h1><p>Actual tmux terminal cells captured during a real agent session. These are not pixel screenshots. Authentication must finish before capture. Physical LEDs and USB are not verified.</p>
<p>Independent review: ''' + html.escape(result['status'] if result else 'pending') + '</p><pre>' +
        html.escape(json.dumps(result, indent=2) if result else 'No labels: capture alone cannot pass.') + '</pre>' +
        '<p><a href="recording.json">Recorded observations</a></p>' + '\n'.join(entries) + '</html>')


def record(args):
    guest.guard()
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / 'screens').mkdir()
    hook = Path.home() / f'.local/state/sidepulse/agent-monitor/{args.provider}.jsonl'
    offset = hook.stat().st_size if hook.exists() else 0
    data = {'schema': 1, 'run_id': uuid.uuid4().hex, 'provider': args.provider,
            'workspace': str(args.workspace.resolve()), 'capture_kind': 'tmux terminal cells',
            'frames': [], 'error': None}
    start = time.monotonic()
    try:
        while time.monotonic() - start < args.seconds and not (args.output / 'stop').exists():
            before = guest.common.installed_snapshot()
            text = subprocess.check_output(['tmux', 'capture-pane', '-p', '-t', args.session], text=True, timeout=5)
            after = guest.common.installed_snapshot()
            sessions = {r['session_id'] for r in guest.common.read_records(hook, offset)
                        if r.get('cwd') == data['workspace'] and r.get('session_id')}
            detected = sorted({r['mode'] for reply in (before, after)
                               for r in guest.common.state_rows(reply)
                               if r.get('provider') == args.provider and r.get('session_id') in sessions})
            index = len(data['frames'])
            screen = f'screens/{index:05}.txt'
            (args.output / screen).write_text(text)
            frame = {'id': index, 't': time.monotonic() - start, 'wall_time': time.time(),
                     'screen': screen, 'screen_sha256': hashlib.sha256(text.encode()).hexdigest(),
                     'before': before, 'after': after, 'sessions': sorted(sessions), 'detected': detected}
            data['frames'].append(frame)
            with (args.output / 'frames.jsonl').open('a') as stream:
                stream.write(json.dumps(frame) + '\n')
            time.sleep(.5)
        if not (args.output / 'stop').exists():
            data['error'] = 'Recording time limit reached without an explicit finish'
    except Exception as exc:
        data['error'] = str(exc)
    finally:
        (args.output / 'recording.json').write_text(json.dumps(data, indent=2))
        render(data, args.output)
    return int(bool(data['error']))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    rec = sub.add_parser('record')
    rec.add_argument('provider', choices=['claude', 'codex'])
    rec.add_argument('--session', default='sidepulse-test')
    rec.add_argument('--workspace', type=Path, required=True)
    rec.add_argument('--output', type=Path, required=True)
    rec.add_argument('--seconds', type=int, default=600)
    rev = sub.add_parser('review')
    rev.add_argument('directory', type=Path)
    rev.add_argument('labels', type=Path)
    args = parser.parse_args()
    if args.command == 'record':
        if not 10 <= args.seconds <= 600 or args.session.startswith('-'):
            parser.error('Use a valid tmux session and a 10–600 second recording')
        return record(args)
    data = json.loads((args.directory / 'recording.json').read_text())
    result = review(data, json.loads(args.labels.read_text()), args.directory)
    (args.directory / 'visual-result.json').write_text(json.dumps(result, indent=2))
    render(data, args.directory, result)
    print(json.dumps(result, indent=2))
    return int(result['status'] != 'passed')


if __name__ == '__main__':
    raise SystemExit(main())
