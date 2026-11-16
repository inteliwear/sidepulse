#!/usr/bin/env python3
"""Install, migrate, and exercise the installed package in a disposable Linux VM.

Hook replay is explicitly separate from authenticated real-agent execution.
The shared macOS harness supplies black-box hook/live assertions, never mocked
provider results. Linux checks below use real systemd, inotify and mount events.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET

SOURCE = Path(__file__).resolve().parents[2]
HOME_DIR = Path.home()
PYTHON = HOME_DIR / '.local/share/sidepulse/venv/bin/python'
RESULTS = HOME_DIR / 'sidepulse-linux-results'
spec = importlib.util.spec_from_file_location('agent_checks', SOURCE / 'scripts/macos-vm/check.py')
common = importlib.util.module_from_spec(spec)
spec.loader.exec_module(common)


def guard():
    if platform.system() != 'Linux' or os.getuid() == 0:
        raise RuntimeError('Run as the non-root test user inside a disposable Linux VM')
    if not Path('/etc/sidepulse-test-vm').is_file():
        raise RuntimeError('Missing harness VM marker; refusing to change this machine')
    if subprocess.run(['systemd-detect-virt', '--vm', '--quiet']).returncode:
        raise RuntimeError('A real virtual machine is required, not a container or physical host')
    if HOME_DIR != Path('/home/test'):
        raise RuntimeError('This harness owns only /home/test in its disposable VM')


def capture(argv):
    return subprocess.check_output([str(v) for v in argv], text=True, timeout=30).strip()


def installation_state():
    from sidepulse.settings import default_settings_path
    paths = [HOME_DIR / '.claude/settings.json', HOME_DIR / '.codex/config.toml',
             default_settings_path(HOME_DIR)]
    return {'python': str(PYTHON.resolve()), 'python_version': capture([PYTHON, '--version']),
            'version': capture([HOME_DIR / '.local/bin/sidepulse', '--version']),
            'service_pid': capture(['systemctl', '--user', 'show', '-p', 'MainPID', '--value', 'sidepulse']),
            'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'configs': {str(p.relative_to(HOME_DIR)): p.read_text() for p in paths if p.exists()}}


def seed_migration():
    from dataclasses import replace
    from sidepulse.settings import default_settings_path, load_settings, save_settings
    # Only disposable test-user configs; preserve the existing managed hooks.
    claude = HOME_DIR / '.claude/settings.json'
    data = json.loads(claude.read_text())
    data['env'] = {**data.get('env', {}), 'SIDEPULSE_MIGRATION_TEST': 'preserve-me'}
    data.setdefault('hooks', {}).setdefault('Stop', []).append({
        'hooks': [{'type': 'command', 'command': 'printf unrelated-linux-hook >&2'}]})
    claude.write_text(json.dumps(data, indent=2) + '\n')
    codex = HOME_DIR / '.codex/config.toml'
    with codex.open('a') as stream:
        stream.write('\n[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ntype = "command"\ncommand = "printf unrelated-linux-hook >&2"\n')
    settings = default_settings_path(HOME_DIR)
    save_settings(replace(load_settings(settings), idle_timeout_seconds=321), settings)


class LinuxHarness(common.Harness):
    def __init__(self, output, stage):
        super().__init__(output)
        self.stage = stage
        manifest = SOURCE / 'source-manifest.json'
        if manifest.exists():
            shutil.copyfile(manifest, output / manifest.name)

    def installed_source(self):
        import sidepulse
        installed = Path(sidepulse.__file__).resolve().parent.parent
        manifest = json.loads((SOURCE / 'source-manifest.json').read_text())
        hashes, mismatch = {}, []
        for name, expected in manifest.items():
            if not name.startswith('src/') or name.endswith('/_build_info.py'):
                continue
            path = installed / name.removeprefix('src/')
            actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
            hashes[name] = actual
            if actual != expected:
                mismatch.append(name)
        (self.output / 'installed-source.json').write_text(json.dumps(hashes, indent=2))
        assert not mismatch, f'Installed source differs: {mismatch}'
        assert hashes, 'No installed source was checked'
        return {'non_generated_files': len(hashes)}

    def service(self):
        enabled = capture(['systemctl', '--user', 'is-enabled', 'sidepulse.service'])
        active = capture(['systemctl', '--user', 'is-active', 'sidepulse.service'])
        assert enabled == 'enabled' and active == 'active', (enabled, active)
        self.command('systemd-unit', ['systemctl', '--user', 'cat', 'sidepulse.service'])
        return {'enabled': enabled, 'active': active,
                'pid': capture(['systemctl', '--user', 'show', '-p', 'MainPID', '--value', 'sidepulse'])}

    def recovery(self):
        from sidepulse.program_status import StatusError

        old = capture(['systemctl', '--user', 'show', '-p', 'MainPID', '--value', 'sidepulse'])
        assert int(old) > 0
        subprocess.run(['systemctl', '--user', 'kill', '--kill-whom=main',
                        '--signal=SIGKILL', 'sidepulse.service'], check=True)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            current = capture(['systemctl', '--user', 'show', '-p', 'MainPID', '--value', 'sidepulse'])
            if int(current) > 0 and current != old:
                try:
                    reply = self.snapshot()
                except (OSError, StatusError):
                    pass  # A new PID is not proof the socket is ready yet.
                else:
                    assert reply.get('ok') is True
                    return {'old_pid': old, 'new_pid': current}
            time.sleep(.2)
        raise AssertionError('Real systemd did not recover the crashed daemon')

    def reboot(self):
        previous = json.loads((RESULTS / 'last-observed-boot.json').read_text())
        current = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        assert previous['boot_id'] != current, 'No real OS reboot was observed'
        return {'before': previous['boot_id'], 'after': current}

    def device(self):
        # Own ephemeral filesystem, never /Volumes/SidePulse or a physical USB disk.
        from sidepulse.device_events import DeviceEvents
        from sidepulse.device_writer import discover_devices
        from sidepulse.program_transport import daemon_command

        enabled = self.snapshot()['outputs_enabled']
        # The CLI writes directly; an automatic daemon animation may otherwise
        # legitimately replace it before the assertion. Pause only this VM's
        # automatic writer, retaining kernel/discovery observation.
        mount = Path('/media/test/SidePulseHarness-' + uuid.uuid4().hex[:8])
        subprocess.run(['sudo', 'mkdir', '-p', str(mount)], check=True)
        watcher = DeviceEvents()
        observations = []
        try:
            daemon_command({'type': 'output_control', 'enabled': False})
            watcher.start()
            watcher.changed.clear()
            subprocess.run(['sudo', 'mount', '-t', 'tmpfs', '-o',
                            f'size=1m,uid={os.getuid()},gid={os.getgid()}', 'sidepulse-test', str(mount)], check=True)
            assert watcher.changed.wait(5), 'Kernel mount arrival did not notify the watcher'
            target = mount / 'LEDS.LED'
            target.write_text('off\n')
            assert any(d.root == mount for d in discover_devices()), 'New mounted device not discovered'
            self.command('device-write', [self.cli, 'write', 'off', '--device', str(mount)])
            actual = target.read_text()
            (self.output / 'device-file.txt').write_text(actual)
            assert actual.strip() == 'off', f'Installed CLI wrote unexpected bytes: {actual!r}'
            observations.append('mount arrival, discovery, installed CLI write')
            watcher.changed.clear()
            subprocess.run(['sudo', 'umount', str(mount)], check=True)
            assert watcher.changed.wait(5), 'Kernel unmount did not notify the watcher'
            assert not any(d.root == mount for d in discover_devices()), 'Unmounted device remained discoverable'
            observations.append('mount removal, inventory removal')
            return observations
        finally:
            try:
                watcher.close()
            finally:
                try:
                    if os.path.ismount(mount):
                        subprocess.run(['sudo', 'umount', str(mount)], check=True)
                    subprocess.run(['sudo', 'rmdir', str(mount)], check=True)
                finally:
                    daemon_command({'type': 'output_control', 'enabled': enabled})

    def migration(self):
        before = json.loads((RESULTS / 'migration-before.json').read_text())
        after = installation_state()
        (self.output / 'migration-after.json').write_text(json.dumps(after, indent=2))
        assert before['python'] == after['python'], 'Existing interpreter was replaced'
        if self.stage != 'reboot':
            assert before['service_pid'] != after['service_pid'], 'Upgrade did not restart the real service'
        assert json.loads(after['configs']['.claude/settings.json'])['env']['SIDEPULSE_MIGRATION_TEST'] == 'preserve-me'
        from sidepulse.settings import default_settings_path, load_settings
        assert load_settings(default_settings_path(HOME_DIR)).idle_timeout_seconds == 321
        for provider, name in [('claude', '.claude/settings.json'), ('codex', '.codex/config.toml')]:
            assert after['configs'][name].count('printf unrelated-linux-hook >&2') == 1
            assert set(dict(common.EVENTS)) <= set(common.installed_commands(provider, HOME_DIR))
        assert (HOME_DIR / '.local/bin/sp').resolve() == (HOME_DIR / '.local/bin/sidepulse').resolve()
        return 'Interpreter, settings, unrelated hooks once, managed hook uniqueness, alias and service restart'

    def save(self, live):
        failures = sum(row['status'] == 'failed' for row in self.results)
        result = {'schema': 1, 'scope': 'linux-vm', 'stage': self.stage,
                  'os_release': Path('/etc/os-release').read_text(), 'kernel': platform.release(),
                  'architecture': platform.machine(), 'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                  'live_agents': 'attempted' if live else 'not_run', 'visual_verification': 'not_run',
                  'physical_usb_and_leds': 'not_tested', 'results': self.results, 'failures': failures}
        (self.output / 'report.json').write_text(json.dumps(result, indent=2) + '\n')
        (RESULTS / 'last-observed-boot.json').write_text(json.dumps({'boot_id': result['boot_id']}))
        suite = ET.Element('testsuite', name='sidepulse-linux-vm', tests=str(len(self.results)), failures=str(failures))
        for row in self.results:
            case = ET.SubElement(suite, 'testcase', name=row['name'])
            if row['status'] == 'failed':
                ET.SubElement(case, 'failure', message=str(row['detail']))
        ET.ElementTree(suite).write(self.output / 'junit.xml', encoding='unicode')
        print(f'Report: {self.output / "report.json"}')
        return int(bool(failures))


def install(stage):
    RESULTS.mkdir(exist_ok=True)
    output = RESULTS / (time.strftime('%Y%m%d-%H%M%S') + '-' + stage + '-installer')
    output.mkdir()
    if stage in {'fresh', 'main'}:
        assert not PYTHON.exists() and not (HOME_DIR / '.config/systemd/user/sidepulse.service').exists(), 'Restore the clean baseline'
    else:
        assert PYTHON.is_file(), 'An existing main or branch installation is required'
        if stage == 'upgrade':
            seed_migration()
        (RESULTS / 'migration-before.json').write_text(json.dumps(installation_state(), indent=2))
    env = os.environ.copy()
    env.pop('PYTHONPATH', None)
    env.pop('PYTHONHOME', None)
    env['PATH'] = f'{HOME_DIR}/.local/bin:/usr/local/bin:/usr/bin:/bin'
    if stage == 'main':
        installer = output / 'official-setup.sh'
        subprocess.run(['curl', '-fsSL', 'https://sidepulse.io/setup.sh', '-o', str(installer)], check=True)
        env.pop('SIDEPULSE_INSTALL_SPEC', None)
    else:
        installer = SOURCE / 'scripts/setup.sh'
        env['SIDEPULSE_INSTALL_SPEC'] = str(SOURCE)
        env['SETUPTOOLS_SCM_PRETEND_VERSION_FOR_SIDEPULSE'] = '0+linuxtest'
        context = json.loads((SOURCE / 'build-context.json').read_text())
        env['SETUPTOOLS_SCM_PRETEND_METADATA_FOR_SIDEPULSE'] = '{node="g' + context['revision'] + '"}'
    with (output / 'install.log').open('w') as log:
        result = subprocess.run(['sh', str(installer)], env=env, stdout=log, stderr=subprocess.STDOUT, timeout=900)
    report = {'stage': stage, 'installer_exit_code': result.returncode,
              'installer_sha256': hashlib.sha256(installer.read_bytes()).hexdigest(),
              'fresh_precondition_verified': stage in {'fresh', 'main'}}
    (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    if result.returncode:
        raise RuntimeError(f'Installer failed; see {output / "install.log"}')
    os.execv(str(PYTHON), [str(PYTHON), str(Path(__file__).resolve()), 'verify', '--stage', stage])


def agent_prerequisites(output):
    """Use distro bubblewrap and its documented profile, never a global bypass."""
    profile = Path('/etc/apparmor.d/bwrap-userns-restrict')
    supplied = Path('/usr/share/apparmor/extra-profiles/bwrap-userns-restrict')
    probe = ['bwrap', '--ro-bind', '/', '/', '--unshare-user', '--unshare-net', '/bin/true']
    with (output / 'linux-prerequisites.log').open('w') as log:
        subprocess.run(['sudo', 'apt-get', 'install', '-y', 'bubblewrap', 'apparmor-profiles', 'apparmor-utils'],
                       stdout=log, stderr=subprocess.STDOUT, check=True, timeout=300)
        first = subprocess.run(probe, stdout=log, stderr=subprocess.STDOUT, timeout=20)
        if first.returncode:
            if not profile.exists():
                if not supplied.is_file():
                    raise RuntimeError('Missing Ubuntu bubblewrap AppArmor profile; see official Codex prerequisites')
                subprocess.run(['sudo', 'install', '-m', '0644', str(supplied), str(profile)], check=True)
            # Keep existing policy if already present; do not replace another
            # vendor's profile or disable unprivileged-userns restrictions.
            subprocess.run(['sudo', 'apparmor_parser', '-r', str(profile)],
                           stdout=log, stderr=subprocess.STDOUT, check=True)
            subprocess.run(probe, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=20)
    result = {'bubblewrap': capture(['bwrap', '--version']),
              'global_userns_restriction': Path('/proc/sys/kernel/apparmor_restrict_unprivileged_userns').read_text().strip()
                  if Path('/proc/sys/kernel/apparmor_restrict_unprivileged_userns').exists() else None,
              'profile_sha256': hashlib.sha256(profile.read_bytes()).hexdigest() if profile.exists() else None}
    (output / 'linux-prerequisites.json').write_text(json.dumps(result, indent=2))
    return result


def agents():
    RESULTS.mkdir(exist_ok=True)
    output = RESULTS / 'agent-install'
    output.mkdir(exist_ok=True)
    agent_prerequisites(output)
    installer = output / 'claude-install.sh'
    subprocess.run(['curl', '-fsSL', 'https://claude.ai/install.sh', '-o', str(installer)], check=True)
    with (output / 'claude-install.log').open('w') as log:
        subprocess.run(['bash', str(installer)], stdout=log, stderr=subprocess.STDOUT, check=True, timeout=600)
    release = json.loads(capture(['curl', '-fsSL', 'https://api.github.com/repos/openai/codex/releases/latest']))
    arch = 'aarch64' if platform.machine() == 'aarch64' else 'x86_64'
    name = f'codex-package-{arch}-unknown-linux-musl.tar.gz'
    asset = next(item for item in release['assets'] if item['name'] == name)
    archive = output / name
    subprocess.run(['curl', '-fLSs', asset['browser_download_url'], '-o', str(archive)], check=True, timeout=600)
    actual = hashlib.sha256(archive.read_bytes()).hexdigest()
    if asset.get('digest'):
        assert asset['digest'] == 'sha256:' + actual
    common.install_codex_package(archive, HOME_DIR)
    versions = {p: capture([HOME_DIR / '.local/bin' / p, '--version']) for p in common.PROVIDERS}
    (output / 'versions.json').write_text(json.dumps({'versions': versions, 'codex_asset': asset['browser_download_url'],
                                                    'codex_sha256': actual}, indent=2))
    subprocess.run([HOME_DIR / '.local/bin/sidepulse', 'setup'], check=True)
    print(json.dumps(versions))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['fresh', 'main', 'upgrade', 'repeat', 'verify', 'agents', 'live'])
    parser.add_argument('--stage', default='verify')
    parser.add_argument('--provider', choices=common.PROVIDERS, help='One real agent for live mode')
    args = parser.parse_args()
    guard()
    os.environ['PATH'] = f'{HOME_DIR}/.local/bin:/usr/local/bin:/usr/bin:/bin'
    if args.command in {'upgrade', 'repeat'} and PYTHON.is_file() and Path(sys.executable) != PYTHON:
        # Migration reads settings through the existing package before setup
        # replaces it. The system Python used to launch the harness has no app.
        os.execv(str(PYTHON), [str(PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]])
    if args.command in {'fresh', 'main', 'upgrade', 'repeat'}:
        install(args.command)
    if args.command == 'agents':
        agents()
        return 0
    if Path(sys.executable) != PYTHON:
        os.execv(str(PYTHON), [str(PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]])
    stage = ('live-' + args.provider if args.provider else 'live') if args.command == 'live' else args.stage
    harness = LinuxHarness(RESULTS / (time.strftime('%Y%m%d-%H%M%S') + '-' + stage + '-' + uuid.uuid4().hex[:6]), stage)
    harness.check('installed-cli', lambda: harness.command('version', [harness.cli, '--version']))
    harness.check('systemd-user-service', harness.service)
    for provider in common.PROVIDERS:
        harness.check('installed-hooks-' + provider, lambda p=provider: harness.replay(p))
    harness.check('installed-observer', harness.snapshot)
    # Check the installer's PID before the crash test deliberately replaces it.
    if stage in {'upgrade', 'repeat', 'reboot'} and (RESULTS / 'migration-before.json').exists():
        harness.check('migration-preserved', harness.migration)
    if stage == 'reboot':
        harness.check('real-os-reboot', harness.reboot)
    if stage != 'main':
        harness.check('installed-source', harness.installed_source)
        harness.check('native-mount-notifications-and-write', harness.device)
        harness.check('systemd-crash-recovery', harness.recovery)
    if args.command == 'live':
        for provider in ((args.provider,) if args.provider else common.PROVIDERS):
            harness.check('real-agent-' + provider, lambda p=provider: harness.live(p))
    return harness.save(args.command == 'live')


if __name__ == '__main__':
    raise SystemExit(main())
