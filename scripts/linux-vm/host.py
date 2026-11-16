#!/usr/bin/env python3
"""Disposable Ubuntu ARM64 VMs on Apple Silicon, with real systemd and SSH.

All VM disks, keys, host-key records, logs, and evidence stay under --directory.
No existing VM or host installation is modified.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import socket
import subprocess
import tarfile
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[2]
IMAGE_URL = 'https://cloud-images.ubuntu.com/releases/noble/release/'
IMAGE_NAME = 'ubuntu-24.04-server-cloudimg-arm64.img'


def call(argv, **kwargs):
    return subprocess.run([str(v) for v in argv], check=True, **kwargs)


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def download_image(cache):
    cache.mkdir(parents=True, exist_ok=True)
    image = cache / IMAGE_NAME
    sums = cache / 'SHA256SUMS'
    if not sums.exists():
        urllib.request.urlretrieve(IMAGE_URL + 'SHA256SUMS', sums)
    expected = next(line.split()[0] for line in sums.read_text().splitlines()
                    if line.split()[-1].lstrip('*') == IMAGE_NAME)
    if not image.exists():
        partial = image.with_suffix('.partial')
        urllib.request.urlretrieve(IMAGE_URL + IMAGE_NAME, partial)
        partial.replace(image)
    if digest(image) != expected:
        raise ValueError('Ubuntu image does not match the saved official SHA256SUMS; cache retained')
    return image


def load(directory):
    config = json.loads((directory / 'vm.json').read_text())
    if config.get('schema') != 1 or config.get('kind') != 'sidepulse-disposable-linux':
        raise ValueError('Directory is not a VM created by this harness')
    return config


def qmp(directory, command):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(3)
        connection.connect(str(directory / 'qmp.sock'))
        with connection.makefile('rwb') as stream:
            json.loads(stream.readline())
            for operation in ('qmp_capabilities', command):
                stream.write((json.dumps({'execute': operation}) + '\n').encode())
                stream.flush()
                while True:
                    reply = json.loads(stream.readline())
                    if 'error' in reply:
                        raise RuntimeError(reply['error'])
                    if 'return' in reply:
                        break
            return reply['return']


def running(directory):
    try:
        qmp(directory, 'query-status')
        return True
    except (OSError, ValueError):
        # A process may be starting or its socket may be stale. Never mutate a
        # disk based only on an unresponsive QMP connection.
        pidfile = directory / 'qemu.pid'
        if pidfile.exists():
            try:
                os.kill(int(pidfile.read_text()), 0)
                raise RuntimeError('QEMU is alive but not responding; inspect its log')
            except ProcessLookupError:
                pass
        return False


def ssh_args(directory, config, *, scp=False):
    return ['scp' if scp else 'ssh', '-P' if scp else '-p', str(config['ssh_port']),
            '-i', str(directory / 'id_ed25519'), '-o', 'IdentitiesOnly=yes',
            '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
            '-o', 'StrictHostKeyChecking=accept-new',
            '-o', f'UserKnownHostsFile={directory / "known_hosts"}']


def remote(directory, command, **kwargs):
    return call(ssh_args(directory, load(directory)) + ['test@127.0.0.1', command], **kwargs)


def create(directory, cache, port):
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        raise ValueError('This VM launcher requires an Apple Silicon Mac; guest.py runs on Linux')
    if directory.exists():
        raise ValueError('Choose a new VM directory; existing disks are never overwritten')
    image = download_image(cache)
    directory.mkdir(parents=True)
    directory.chmod(0o700)
    call(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', directory / 'id_ed25519'])
    firmware = Path(shutil.which('qemu-system-aarch64')).resolve().parents[1] / 'share/qemu'
    if not firmware.exists():
        firmware = Path('/opt/homebrew/share/qemu')
    shutil.copyfile(firmware / 'edk2-arm-vars.fd', directory / 'vars.fd')
    call(['qemu-img', 'create', '-f', 'qcow2', '-F', 'qcow2', '-b', image.resolve(),
          directory / 'disk.qcow2', '32G'])
    instance = str(uuid.uuid4())
    seed = directory / 'seed'
    seed.mkdir()
    key = (directory / 'id_ed25519.pub').read_text().strip()
    (seed / 'meta-data').write_text(f'instance-id: {instance}\nlocal-hostname: sidepulse-linux-test\n')
    (seed / 'user-data').write_text('''#cloud-config
users:
  - name: test
    shell: /bin/bash
    groups: [sudo]
    sudo: ALL=(ALL) NOPASSWD:ALL
    lock_passwd: true
    ssh_authorized_keys:
      - ''' + key + '''
ssh_pwauth: false
package_update: true
packages: [python3-venv, python3-pip, git, curl, ca-certificates, tmux, ttyd]
write_files:
  - path: /etc/sidepulse-test-vm
    permissions: '0644'
    content: ''' + instance + '''
runcmd:
  - [systemctl, disable, --now, ttyd.service]
  - [loginctl, enable-linger, test]
  - [touch, /var/lib/sidepulse-test-ready]
''')
    call(['hdiutil', 'makehybrid', '-iso', '-joliet', '-default-volume-name', 'cidata',
          '-o', directory / 'seed.iso', seed], stdout=subprocess.DEVNULL)
    config = {'schema': 1, 'kind': 'sidepulse-disposable-linux', 'instance_id': instance,
              'ssh_port': port, 'firmware': str(firmware / 'edk2-aarch64-code.fd'),
              'image': str(image.resolve()), 'image_sha256': digest(image),
              'image_source': IMAGE_URL + IMAGE_NAME, 'created_at': time.time()}
    (directory / 'vm.json').write_text(json.dumps(config, indent=2) + '\n')


def start(directory):
    config = load(directory)
    if running(directory):
        raise ValueError('VM is already running')
    call(['qemu-system-aarch64', '-name', 'SidePulse Linux Test', '-machine', 'virt',
          '-accel', 'hvf', '-cpu', 'host', '-smp', '4', '-m', '4096',
          '-drive', f'if=pflash,format=raw,readonly=on,file={config["firmware"]}',
          '-drive', f'if=pflash,format=raw,file={directory / "vars.fd"}',
          '-drive', f'if=virtio,format=qcow2,file={directory / "disk.qcow2"}',
          '-drive', f'if=virtio,format=raw,readonly=on,file={directory / "seed.iso"}',
          '-device', 'virtio-net-pci,netdev=net', '-netdev',
          f'user,id=net,hostfwd=tcp:127.0.0.1:{config["ssh_port"]}-:22',
          '-display', 'none', '-serial', f'file:{directory / "console.log"}',
          '-qmp', f'unix:{directory / "qmp.sock"},server=on,wait=off',
          '-pidfile', directory / 'qemu.pid', '-daemonize'])
    wait_ready(directory)


def wait_ready(directory):
    deadline = time.monotonic() + 55
    while time.monotonic() < deadline:
        try:
            remote(directory, 'test -f /var/lib/sidepulse-test-ready && '
                   'systemctl --user show-environment >/dev/null',
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except subprocess.CalledProcessError:
            time.sleep(1)
    raise RuntimeError('Guest SSH/user manager not ready; inspect console.log; VM left running')


def stop(directory):
    load(directory)
    if running(directory):
        qmp(directory, 'system_powerdown')
        deadline = time.monotonic() + 55
        while running(directory) and time.monotonic() < deadline:
            time.sleep(1)
        if running(directory):
            raise RuntimeError('Graceful shutdown timed out; VM left running, no force stop')


def clone(source, directory, port):
    config = load(source)
    if running(source):
        raise ValueError('Shut down the baseline before cloning')
    if directory.exists():
        raise ValueError('Clone destination already exists')
    directory.mkdir(parents=True, mode=0o700)
    for name in ('vars.fd', 'seed.iso', 'id_ed25519', 'id_ed25519.pub'):
        shutil.copy2(source / name, directory / name)
    # Copy the stopped writable layer. A clone must not depend on a baseline
    # disk that could later be booted and changed; only the verified vendor
    # cloud image remains a shared, immutable backing file.
    shutil.copy2(source / 'disk.qcow2', directory / 'disk.qcow2')
    config.update(ssh_port=port, baseline=str(source), cloned_at=time.time())
    (directory / 'vm.json').write_text(json.dumps(config, indent=2) + '\n')


def bundle(output):
    output.mkdir(parents=True, exist_ok=True)
    files = [ROOT / p for p in ('pyproject.toml', 'README.md', 'LICENSE', 'MANIFEST.in', 'LEDS_FORMAT.md')]
    for tree in ('src', 'scripts', 'tests', 'examples', 'firmware', 'docs', 'profiles'):
        files.extend(p for p in (ROOT / tree).rglob('*') if p.is_file()
                     and not p.is_symlink() and not any(
                         s.startswith('.') or s == '__pycache__' or s.endswith('.egg-info')
                         for s in p.relative_to(ROOT).parts) and p.suffix not in {'.pyc', '.pyo'})
    manifest = {str(p.relative_to(ROOT)): digest(p) for p in sorted(files)}
    (output / 'source-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    context = {
        'revision': call(['git', '-C', ROOT, 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip(),
        'worktree': call(['git', '-C', ROOT, 'status', '--porcelain'], capture_output=True, text=True).stdout,
    }
    (output / 'build-context.json').write_text(json.dumps(context, indent=2) + '\n')
    with tarfile.open(output / 'source.tar.gz', 'w:gz') as archive:
        for p in sorted(files):
            archive.add(p, arcname=str(p.relative_to(ROOT)), recursive=False)
        archive.add(output / 'source-manifest.json', arcname='source-manifest.json')
        archive.add(output / 'build-context.json', arcname='build-context.json')
    (output / 'source-context.json').write_text(json.dumps({**context,
        'bundle_sha256': digest(output / 'source.tar.gz')}, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['create', 'start', 'wait', 'stop', 'clone', 'ssh', 'upload', 'collect', 'bundle'])
    parser.add_argument('--directory', type=Path, default=ROOT / 'build/linux-vm/baseline')
    parser.add_argument('--source', type=Path)
    parser.add_argument('--cache', type=Path, default=ROOT / 'build/linux-vm/cache')
    parser.add_argument('--port', type=int, default=22280)
    parser.add_argument('--output', type=Path, default=ROOT / 'build/linux-vm/input')
    parser.add_argument('--exec', dest='remote_command', default='uname -a')
    args = parser.parse_args()
    directory = args.directory.resolve()
    if ',' in str(directory) or len(str(directory / 'qmp.sock').encode()) >= 104:
        parser.error('VM path must be short enough for a Unix socket and contain no commas')
    if not 1024 <= args.port <= 65535:
        parser.error('Choose an unprivileged TCP port')
    if args.command == 'create':
        create(directory, args.cache.resolve(), args.port)
    elif args.command == 'start':
        start(directory)
    elif args.command == 'wait':
        wait_ready(directory)
    elif args.command == 'stop':
        stop(directory)
    elif args.command == 'clone':
        if not args.source:
            parser.error('clone requires --source')
        clone(args.source.resolve(), directory, args.port)
    elif args.command == 'ssh':
        remote(directory, args.remote_command)
    elif args.command == 'bundle':
        bundle(args.output.resolve())
    elif args.command == 'upload':
        config = load(directory)
        archive = args.output / 'source.tar.gz'
        revision = digest(archive)[:16]
        destination = f'/home/test/sidepulse-source-{revision}'
        remote(directory, 'mkdir -p ' + destination)
        call(ssh_args(directory, config, scp=True) + [args.output / 'source.tar.gz',
             f'test@127.0.0.1:{destination}/source.tar.gz'])
        remote(directory, f'tar -xzf {destination}/source.tar.gz -C {destination} && '
               'if test -d /home/test/sidepulse-source && ! test -L /home/test/sidepulse-source; then '
               f'mv /home/test/sidepulse-source /home/test/sidepulse-source-original-{revision}; fi && '
               f'ln -sfn {destination} /home/test/sidepulse-source')
        print('Guest source: ' + destination)
    elif args.command == 'collect':
        config = load(directory)
        args.output.mkdir(parents=True, exist_ok=True)
        call(ssh_args(directory, config, scp=True) + ['-r',
             'test@127.0.0.1:/home/test/sidepulse-linux-results', args.output])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
