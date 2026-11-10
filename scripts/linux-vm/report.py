#!/usr/bin/env python3
"""Summarize collected evidence without turning unrun agent checks into passes."""
import argparse
import html
import json
from pathlib import Path
import re

STAGES = [('Fresh branch install', 'fresh', 'fresh-verified'),
          ('Fresh guest after OS reboot', 'fresh', 'reboot'),
          ('Official main baseline', 'upgrade', 'main'),
          ('Official main to branch', 'upgrade', 'upgrade'),
          ('Same-version reinstall', 'upgrade', 'repeat'),
          ('Upgraded guest after OS reboot', 'upgrade', 'reboot')]


def summarize(directory):
    rows, earlier = [], []
    for label, machine, stage in STAGES:
        candidates = []
        for path in sorted((directory / machine).rglob('report.json')):
            data = json.loads(path.read_text())
            if data.get('scope') == 'linux-vm' and data.get('stage') == stage:
                candidates.append((path, data))
        if not candidates:
            rows.append({'scenario': label, 'status': 'not_run', 'checks': 0})
            continue
        path, data = candidates[-1]
        rows.append({'scenario': label, 'status': 'passed' if data['failures'] == 0 else 'failed',
                     'checks': len(data['results']), 'report': str(path.relative_to(directory))})
        earlier.extend(str(p.relative_to(directory)) for p, d in candidates[:-1] if d['failures'])
    unit_file = directory / 'unit/environment.json'
    unit = json.loads(unit_file.read_text()) if unit_file.exists() else {}
    unit_passed = (len(unit.get('commands', [])) == 2 and
                   all(row['exit_code'] == 0 for row in unit['commands']))
    log = (directory / 'unit/unit.log').read_text() if (directory / 'unit/unit.log').exists() else ''
    counts = {}
    for name, pattern in [('passed', r'(\d+) passed'), ('skipped', r'(\d+) skipped'),
                          ('subtests_passed', r'(\d+) subtests passed')]:
        matches = re.findall(pattern, log)
        if matches:
            counts[name] = int(matches[-1])
    live = {'claude': 'not_run', 'codex': 'not_run'}
    visual = []
    for machine in ('fresh', 'upgrade'):
        for path in sorted((directory / machine).rglob('report.json')):
            data = json.loads(path.read_text())
            if data.get('stage', '').startswith('live') and data.get('live_agents') == 'attempted':
                real = {row['name']: row['status'] for row in data.get('results', [])
                        if row['name'].startswith('real-agent-')}
                for provider in live:
                    if 'real-agent-' + provider in real:
                        live[provider] = real['real-agent-' + provider]
        for path in sorted((directory / machine).rglob('visual-result.json')):
            visual.append({**json.loads(path.read_text()), 'report': str(path.with_name('review.html').relative_to(directory))})
    reviewed = {}
    for row in sorted(visual, key=lambda item: item.get('reviewed_at', 0)):
        reviewed[row.get('provider')] = row
    return {'schema': 1, 'installation_status': 'passed' if unit_passed and
            all(row['status'] == 'passed' for row in rows) else 'incomplete_or_failed',
            'stages': rows, 'vm_checks': sum(row['checks'] for row in rows),
            'unit': {'status': 'passed' if unit_passed else 'not_passed', **counts},
            'live_agents': 'passed' if set(live.values()) == {'passed'} else
                           'failed' if 'failed' in live.values() else 'partial' if 'passed' in live.values() else 'not_run',
            'live_providers': live,
            'terminal_review': 'passed' if all(reviewed.get(p, {}).get('status') == 'passed' for p in ('claude', 'codex')) else
                               'failed' if any(r.get('status') == 'failed' for r in reviewed.values()) else 'incomplete' if visual else 'not_run',
            'terminal_reports': {p: {'status': r['status'], 'samples': len(r.get('comparisons', [])), 'report': r['report']}
                                 for p, r in reviewed.items()},
            'native_x86_ci': 'workflow_added_not_run', 'windows': 'not_run',
            'physical_usb_and_leds': 'not_tested', 'earlier_failed_stage_reports': earlier}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    data = summarize(args.directory)
    (args.directory / 'summary.json').write_text(json.dumps(data, indent=2) + '\n')
    rows = []
    for row in data['stages']:
        link = ('<a href="' + html.escape(row['report'], quote=True) + '">JSON report</a>') if row.get('report') else '—'
        rows.append(f'<tr><td>{html.escape(row["scenario"])}</td><td>{row["status"]}</td><td>{row["checks"]}</td><td>{link}</td></tr>')
    terminals = ''.join('<li>' + html.escape(provider) + ': ' + html.escape(row['status']) +
                        f' · {row["samples"]} independently labeled samples · <a href="' +
                        html.escape(row['report'], quote=True) + '">terminal comparison</a></li>'
                        for provider, row in data['terminal_reports'].items())
    (args.directory / 'report.html').write_text('''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width"><title>SidePulse Linux verification</title>
<style>body{max-width:1000px;margin:48px auto;padding:0 24px;background:#10151c;color:#e4ebf4;font:17px/1.6 system-ui}a{color:#98d9ff}td,th{padding:12px;text-align:left;border-bottom:1px solid #334155}table{border-collapse:collapse;width:100%}.pending{color:#ffd28a}</style>
<h1>SidePulse Linux verification</h1><p>Ubuntu 24.04 ARM64 in a real QEMU/HVF VM. A separate Linux container runs the full Python suite.</p>
<p class="pending"><strong>Real-agent tests: ''' + html.escape(data['live_agents']) + '''. Terminal review: ''' + html.escape(data['terminal_review']) + '''.</strong> Installed-hook replays below do not represent authenticated live-agent execution. See the <a href="fresh/sidepulse-linux-results/agent-install/versions.json">installed agent versions</a>.</p>
<table><tr><th>Scenario</th><th>Result</th><th>Checks</th><th>Evidence</th></tr>''' + ''.join(rows) + '''</table>
<p>Full Linux suite: ''' + html.escape(json.dumps(data['unit'])) + '''. <a href="unit/unit.log">Test log</a> · <a href="unit/junit.xml">JUnit</a> · <a href="unit/environment.json">Environment</a>.</p>
<p>The VM checks exercise real systemd startup, installed Claude/Codex hook commands, matching daemon states for new sessions, 98 non-generated installed source files, native mount arrival/removal, isolated tmpfs writes, and recovery after killing the service main process. Upgrade checks preserve interpreter/settings/unrelated hooks and require a real service restart. Reboot verification requires a changed kernel boot ID.</p>
<h2>Real agent screens</h2><ul>''' + terminals + '''</ul><p>Actual terminal cells were labeled before viewing detected modes. An explicitly running background terminal is labeled tool running. Browser screenshots are separate pixel captures: <a href="claude-retry-question.png">Claude question</a> · <a href="claude-retry-finished.png">Claude completion</a> · <a href="codex-question.png">Codex question</a> · <a href="codex-plan-confirmation.png">Codex native plan menu</a> · <a href="codex-implemented.png">Codex implementation complete</a>.</p>
<p>The first harness attempts are retained. Corrections target only the main service process, wait for its responding socket after a new PID appears, pause and restore the VM's automatic writer while asserting manual CLI output, include the suite's support files, and carry the recorded Git revision into archive builds. An interrupted first terminal recording remains a failed capture. Codex initially could not execute its shell tool; provisioning now installs distro bubblewrap and the documented Ubuntu AppArmor profile, preserving the global restriction. No SidePulse product code was changed by this Linux harness work.</p>
<p>Native x86 Linux CI has been added but has not run for these uncommitted changes. Windows, physical USB, and physical LEDs are not verified here. Host credentials were not copied. <a href="vm-state.json">Recorded VM states</a>.</p>
<p><a href="summary.json">Summary</a> · <a href="source-context.json">Source context</a></p></html>''')
    print(json.dumps(data, indent=2))


if __name__ == '__main__':
    main()
