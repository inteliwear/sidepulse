#!/usr/bin/env python3
"""Render reviewed Windows evidence; unrun capabilities never become passes."""
import argparse
import html
import json
from pathlib import Path
import re

SCENARIOS = [
    ('Fresh ARM64 Python', 'fresh-arm64', 'fresh'),
    ('Fresh x64 Python on Windows ARM64', 'fresh-x64', 'fresh'),
    ('Public main package install', 'upgrade-arm64', 'main'),
    ('Main to frozen branch', 'upgrade-arm64', 'upgrade'),
    ('Same-version reinstall', 'upgrade-arm64', 'repeat'),
]


def summarize(directory):
    rows = []
    for label, machine, stage in SCENARIOS:
        candidates = []
        for path in sorted((directory / machine).rglob('result.json')):
            data = json.loads(path.read_text(encoding='utf-8-sig'))
            if data.get('scope') == 'native-windows' and data.get('stage') == stage:
                candidates.append((path, data))
        if not candidates:
            rows.append({'scenario': label, 'status': 'not_run', 'passed': 0, 'failed': 0})
            continue
        path, data = candidates[-1]
        checks = data['checks']
        failures = [c for c in checks if c['status'] == 'failed']
        passed = sum(c['status'] == 'passed' for c in checks)
        rows.append({'scenario': label, 'status': 'failed' if failures else 'passed' if checks else 'incomplete',
                     'passed': passed, 'failed': len(failures), 'failures': failures,
                     'python_platform': data.get('python_platform'),
                     'report': path.relative_to(directory).as_posix(),
                     'earlier_reports': [p.relative_to(directory).as_posix() for p, _ in candidates[:-1]]})
    unit_runs = []
    for path in sorted(directory.rglob('result.json')):
        result = json.loads(path.read_text(encoding='utf-8-sig'))
        if result.get('scope') != 'native-windows' or result.get('stage') != 'unit':
            continue
        checks = {c['name']: c['status'] for c in result['checks']}
        log = path.with_name('full-suite.log')
        text = log.read_text(encoding='utf-8', errors='replace') if log.exists() else ''
        counts = {}
        for name in ('passed', 'failed', 'skipped', 'errors', 'subtests passed', 'subtests failed'):
            found = re.findall(r'(\d+) ' + name + r'\b', text)
            if found:
                counts[name.replace(' ', '_')] = int(found[-1])
        unit_runs.append({'status': 'failed' if result['failures'] else
                         'passed' if checks.get('full-python-suite') == 'passed' else 'incomplete',
                         'python_platform': result.get('python_platform'), 'counts': counts,
                         'report': path.relative_to(directory).as_posix(),
                         'log': log.relative_to(directory).as_posix()})
    measured = rows + unit_runs
    return {'schema': 1, 'status': 'failed' if any(r['status'] == 'failed' for r in measured) else
            'passed' if all(r['status'] == 'passed' for r in measured) else 'incomplete',
            'scenarios': rows, 'real_agents': 'not_run', 'agent_screen_comparison': 'not_run',
            'unit_runs': unit_runs,
            'mounted_volume_events': 'not_run', 'reboot_autostart': 'not_run',
            'native_x64_ci': 'workflow_added_not_run', 'physical_usb_and_leds': 'not_tested'}


def render(directory):
    data = summarize(directory)
    (directory / 'summary.json').write_text(json.dumps(data, indent=2) + '\n')
    rows, details = [], []
    for row in data['scenarios']:
        evidence = ('<a href="' + html.escape(row['report'], quote=True) + '">Result and logs</a>') if row.get('report') else '—'
        rows.append(f'<tr><td>{html.escape(row["scenario"])}</td><td class="{row["status"]}">{row["status"]}</td>'
                    f'<td>{row["passed"]}</td><td>{row["failed"]}</td><td>{evidence}</td></tr>')
        if row.get('report'):
            folder = Path(row['report']).parent.as_posix()
            links = ''.join(f'<li><a href="{html.escape(folder + "/" + p.name, quote=True)}">{html.escape(p.name)}</a></li>'
                            for p in sorted((directory / folder).glob('*.log')))
            errors = ''.join('<li>' + html.escape(c['name'] + ': ' + c.get('error', 'failed')) + '</li>'
                             for c in row.get('failures', []))
            details.append('<details><summary>' + html.escape(row['scenario']) + '</summary><ul>' + errors + '</ul><ul>' + links + '</ul></details>')
    units = ''.join('<li>' + html.escape(r['python_platform'] + ': ' + r['status'] + ' · ' + json.dumps(r['counts']))
                    + ' · <a href="' + html.escape(r['log'], quote=True) + '">Full test log</a></li>' for r in data['unit_runs'])
    extras = ' · '.join('<a href="' + name + '">' + label + '</a>' for name, label in
                        [('clean-baseline.json', 'Clean baseline'), ('windows-system.json', 'Windows environment'),
                         ('fresh-arm64-result-screen.png', 'Original test console'),
                         ('unit-result-screen.png', 'Completed suite console'),
                         ('terminal-render-check.png', 'Terminal rendering check'),
                         ('original-source-context.json', 'Original source snapshot'),
                         ('unit-source-context.json', 'Unit-rerun source snapshot'),
                         ('harness-host-checks.json', 'Host harness checks')]
                        if (directory / name).exists())
    (directory / 'report.html').write_text('''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width"><title>SidePulse Windows verification</title>
<style>body{max-width:1100px;margin:48px auto;padding:0 24px;background:#10151c;color:#e4ebf4;font:17px/1.6 system-ui}a{color:#98d9ff}td,th{padding:12px;text-align:left;border-bottom:1px solid #334155}table{border-collapse:collapse;width:100%}.failed{color:#ffabac}.passed{color:#a5e6bc}.not_run,.incomplete{color:#ffd28a}details{margin:20px 0}code{font-size:.9em}</style>
<h1>SidePulse Windows verification</h1><p>Windows 11 Pro ARM64 in UTM, cloned from a stopped baseline with no SidePulse installation. Each product probe runs as the normal test user. The x64 interpreter runs under Windows ARM64 emulation.</p>
<p class="failed"><strong>End-to-end Windows support is not verified.</strong> The original runs preserve failures in device writes, status initialization, and background-service startup.</p>
<table><tr><th>Scenario</th><th>Result</th><th>Passed</th><th>Failed</th><th>Evidence</th></tr>''' + ''.join(rows) + '''</table>
<h2>Original Windows findings</h2><ul><li>Device writing imports Unix-only <code>fcntl</code>.</li><li>Status initialization calls Unix-only <code>os.getuid</code>.</li><li>Windows background-service installation is unsupported.</li><li>The original full suite stopped at four platform-specific collection errors. Corrected collection guards are tested separately; the failed run remains available.</li></ul>
<h2>Full suite after collection fixes</h2><ul>''' + (units or '<li>No native Windows rerun result is available.</li>') + '''</ul>
<p>The rerun verifies that installed product files still match the original frozen source. Its failures include product incompatibilities and remaining Unix-specific test assumptions, such as shell commands, permission bits, and symlink creation. These counts are not a count of independent product bugs. The original collection failures remain available.</p>
<h2>Coverage boundaries</h2><p>Real signed-in Claude/Codex runs, agent-screen comparisons, mounted-volume arrival/removal, reboot/autostart, physical USB, and physical LEDs have not been verified by these installation probes. Generated hook configuration checks do not prove that native agents execute those commands. The write fixture is a directory, not a removable volume.</p>
<p>Main is installed using <code>pip install git+https://github.com/inteliwear/sidepulse.git@main</code>. Each installed snapshot records its resolved Git commit. This does not establish a native Windows version of the Unix setup script. The frozen branch source is verified against installed package files. Upgrade probes preserve the interpreter, settings, and unrelated hooks.</p>
<h2>Retained logs</h2>''' + ''.join(details) + '<p>' + extras + '''</p><p>Windows x64 CI has been added but has not run for these uncommitted changes. <a href="summary.json">Machine-readable summary</a>.</p></html>''')
    return data


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path, help='Directory containing reviewed, publishable evidence only')
    args = parser.parse_args()
    print(json.dumps(render(args.directory), indent=2))
