#!/usr/bin/env python3
"""Summarize reviewed native Windows runtime evidence without promoting gaps."""
import argparse
import html
import json
import xml.etree.ElementTree as ET
from pathlib import Path

REQUIRED = {'install', 'installed-cli', 'cli-help', 'installed-source', 'setup',
            'managed-hooks', 'setup-repeat', 'managed-hooks-after-repeat',
            'installed-background-service', 'native-file-write', 'native-file-bytes',
            'native-ipc', 'test-dependencies', 'full-python-suite'}
SCENARIOS = [('fresh-arm64', 'Fresh ARM64'), ('upgrade-arm64', 'Main to branch, ARM64'),
             ('repeat-x64', 'Reinstall, x64 Python on Windows ARM64')]


def read(root, relative):
    path = (root / relative).resolve()
    if root.resolve() not in path.parents:
        raise ValueError('Evidence path must stay inside the reviewed report directory')
    return json.loads(path.read_text()) if path.is_file() else None


def summarize(root):
    manifest = read(root, 'runs.json') or {}
    rows = []
    for key, label in SCENARIOS:
        path = manifest.get(key, '')
        data = read(root, path) if path else None
        checks = data.get('checks', []) if data else []
        names = {c['name'] for c in checks if c.get('status') == 'passed'}
        valid = (data and data.get('scope') == 'native-windows'
                 and data.get('install_method') == 'native-powershell-installer'
                 and REQUIRED <= names and all(c.get('status') == 'passed' for c in checks))
        rows.append({'key': key, 'label': label, 'result': path,
                     'status': 'passed' if valid else 'failed' if data else 'not_run',
                     'passed': sum(c.get('status') == 'passed' for c in checks),
                     'failed': sum(c.get('status') == 'failed' for c in checks)})
    reboots = []
    for key, label in SCENARIOS:
        path = manifest.get(key + '-reboot', '')
        data = read(root, path) if path else None
        valid = (data and data.get('scope') == 'native-windows-reboot'
                 and data.get('service_start_called') is False
                 and data.get('before_boot_id') != data.get('after_boot_id')
                 and {c['name'] for c in data.get('checks', []) if c.get('status') == 'passed'} >= {
                     'real-os-reboot-and-same-interpreter', 'registered-user-logon-startup',
                     'installed-product-unchanged', 'settings-hooks-and-interpreter-preserved'})
        reboots.append({'label': label, 'result': path,
                        'status': 'passed' if valid else 'failed' if data else 'not_run'})
    agents = []
    for provider in ('claude', 'codex'):
        path = manifest.get(provider, '')
        data = read(root, path) if path else None
        valid = (data and data.get('provider') == provider and data.get('real_agent') is True
                 and data.get('status') == 'passed' and data.get('live_checks')
                 and all(data['live_checks'].values()) and not data.get('missing_states')
                 and len(data.get('comparisons', [])) >= 3
                 and all(c.get('matches') is True for c in data['comparisons']))
        agents.append({'label': provider.title(), 'result': path,
                       'status': 'passed' if valid else 'failed' if data else 'not_run'})
    all_rows = rows + reboots + agents
    latest = {'status': 'not_run', 'result': manifest.get('latest-windows-unit', '')}
    if latest['result']:
        unit = read(root, latest['result'])
        required = {'installed-source', 'test-dependencies', 'full-python-suite'}
        checks = unit.get('checks', []) if unit else []
        valid = (unit and unit.get('scope') == 'native-windows' and unit.get('stage') == 'unit'
                 and required <= {c['name'] for c in checks if c.get('status') == 'passed'}
                 and all(c.get('status') == 'passed' for c in checks))
        latest['status'] = 'passed' if valid else 'failed' if unit else 'not_run'
        all_rows.append(latest)
    earlier = []
    for item in manifest.get('earlier-runs', []):
        prior = read(root, item['result'])
        if not prior:
            continue
        checks = prior.get('checks', [])
        functional = [c for c in checks if c.get('name') not in {'test-dependencies', 'full-python-suite'}]
        earlier.append({'label': item['label'], 'result': item['result'],
                        'functional_passed': sum(c.get('status') == 'passed' for c in functional),
                        'functional_total': len(functional),
                        'full_suite': next((c['status'] for c in checks if c.get('name') == 'full-python-suite'), 'not_run')})
    linux = {'status': 'not_run'}
    environment = read(root, 'linux-environment.json')
    junit = root / 'linux-junit.xml'
    if environment and junit.is_file():
        cases = list(ET.parse(junit).iter('testcase'))
        counts = {name: sum(case.find(name) is not None for case in cases)
                  for name in ('failure', 'error', 'skipped')}
        successful = any(c.get('name') == 'unit' and c.get('exit_code') == 0
                         for c in environment.get('commands', []))
        linux = {'status': 'passed' if successful and cases and not counts['failure'] and not counts['error'] else 'failed',
                 'passed': len(cases) - sum(counts.values()), 'counts': counts}
    if manifest.get('require-linux-regression'):
        all_rows.append(linux)
    status = ('passed' if all(r['status'] == 'passed' for r in all_rows) else
              'failed' if any(r['status'] == 'failed' for r in all_rows) else 'incomplete')
    return {'status': status, 'latest_windows_unit': latest, 'notes': manifest.get('notes', []),
            'linux_regression': linux, 'earlier_runs': earlier, 'installations': rows, 'reboots': reboots, 'real_agents': agents,
            'physical_usb_and_leds': 'not_tested', 'native_x64_hardware': 'not_tested'}


def render(root):
    data = summarize(root)
    sections = ['<p>' + html.escape(note) + '</p>' for note in data['notes']]
    if data['latest_windows_unit']['result']:
        latest = data['latest_windows_unit']
        sections.append('<h2>Latest native Windows regression</h2><p>' + latest['status'] +
                        ' · <a href="' + html.escape(latest['result'], quote=True) + '">Installed-source and full-suite result</a></p>')
    for heading, key in [('Installations and full suite', 'installations'),
                         ('Real reboot and logon startup', 'reboots'), ('Real agents and screen comparison', 'real_agents')]:
        rows = []
        for row in data[key]:
            link = ('<a href="' + html.escape(row['result'], quote=True) + '">Evidence</a>') if row['result'] else 'Pending'
            if key == 'real_agents' and row['result']:
                review = str(Path(row['result']).with_name('review.html'))
                if (root / review).is_file():
                    link += ' · <a href="' + html.escape(review, quote=True) + '">Screens</a>'
            rows.append('<tr><td>' + html.escape(row['label']) + '</td><td>' + row['status'] + '</td><td>' + link + '</td></tr>')
        sections.append('<h2>' + heading + '</h2><table><thead><tr><th>Test</th><th>Status</th><th>Result</th></tr></thead><tbody>' + ''.join(rows) + '</tbody></table>')
    manifest = read(root, 'runs.json') or {}
    history = []
    for item in manifest.get('screen-history', []):
        path = (root / item['review']).resolve()
        if root.resolve() not in path.parents:
            raise ValueError('Screen history must stay inside reviewed evidence')
        if path.is_file():
            history.append('<li>' + html.escape(item['label']) + ' · <a href="' +
                           html.escape(item['review'], quote=True) + '">Screens and comparisons</a></li>')
    if history:
        sections.append('<h2>Retained earlier screen runs</h2><ul>' + ''.join(history) + '</ul>')
    if data['linux_regression']['status'] != 'not_run':
        linux = data['linux_regression']
        sections.append('<h2>Linux regression</h2><p>' + linux['status'] + ' · ' + str(linux['passed']) +
                        ' test cases passed. <a href="linux-environment.json">Environment and commands</a> · <a href="linux-junit.xml">Full test result</a>.</p>')
    if data['earlier_runs']:
        sections.append('<h2>Earlier retained runs</h2><ul>' + ''.join(
            '<li>' + html.escape(row['label']) + ': ' + str(row['functional_passed']) + '/' +
            str(row['functional_total']) + ' functional checks; full suite: ' + row['full_suite'] +
            ' · <a href="' + html.escape(row['result'], quote=True) + '">Evidence</a></li>'
            for row in data['earlier_runs']) + '</ul><p>These earlier suites exposed additional issues. Their failures remain retained; final-source validation above is separate.</p>')
    (root / 'summary.json').write_text(json.dumps(data, indent=2) + '\n')
    (root / 'report.html').write_text('''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>SidePulse Windows runtime tests</title><style>body{font:17px/1.6 system-ui;max-width:1050px;margin:40px auto;padding:24px;background:#10151c;color:#e4ebf4}table{width:100%;border-collapse:collapse}th,td{text-align:left;border-bottom:1px solid #384454;padding:12px}a{color:#97d6ff}h1,h2{line-height:1.3}</style>
<h1>SidePulse Windows runtime tests</h1><p>Overall: <strong>''' + data['status'] + '''</strong></p>
<p>Native Windows 11 ARM64 in UTM. x64 Python runs under Windows ARM64 emulation. Product probes run as the normal test user. Failed and earlier runs remain in the original report.</p>''' + ''.join(sections) + '''
<h2>Coverage limits</h2><p>Physical USB and LEDs, native x64 hardware, and CI execution remain unverified. Filesystem tests use owned directories; they do not establish physical removable-volume behavior. A hook replay alone cannot pass the real-agent or screen tests.</p>
<p><a href="summary.json">Machine-readable summary</a></p></html>''')
    return data


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    print(json.dumps(render(parser.parse_args().directory), indent=2))
