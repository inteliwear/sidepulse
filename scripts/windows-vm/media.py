#!/usr/bin/env python3
"""Freeze Windows test source and verified Python installers onto a read-only ISO."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]
PYTHON_SHA256 = {
    'arm64': '3089a1977c32b26f5078e5a7d0285b7a861b8c96250c5dac3b6af59fbe9e9a75',
    'amd64': '5da012101ba2e17113a9a1cd901e9ce1441086f2b7ecb5de06ec94163899a190',
}
GIT_INSTALLER = 'Git-2.56.0.4-arm64.exe'
GIT_SHA256 = 'e7dcc4adbb0eb373064598cd18bc75e60128707d816ead51cd9bd98e356f0075'


def build(destination, downloads, guest_tools=None, git_installer=None):
    if destination.exists():
        raise ValueError('Choose a new media directory; frozen inputs are never overwritten')
    for arch, expected in PYTHON_SHA256.items():
        path = downloads / f'python-3.13.17-{arch}.exe'
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f'Python {arch} installer does not match python.org checksum')
    if git_installer is not None:
        if git_installer.name != GIT_INSTALLER or hashlib.sha256(git_installer.read_bytes()).hexdigest() != GIT_SHA256:
            raise ValueError('Git installer does not match the official release asset checksum')
    destination.mkdir(parents=True)
    stage = destination / 'disc'
    stage.mkdir()
    spec = importlib.util.spec_from_file_location('source_bundle', ROOT / 'scripts/linux-vm/host.py')
    bundler = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bundler)
    bundler.bundle(destination / 'input')
    shutil.copy2(destination / 'input/source.tar.gz', stage)
    shutil.copy2(destination / 'input/source-context.json', stage)
    shutil.copy2(ROOT / 'scripts/windows-vm/prepare.py', stage)
    if git_installer is not None:
        shutil.copy2(git_installer, stage)
        (stage / 'git-source.json').write_text(json.dumps({
            'url': 'https://github.com/git-for-windows/git/releases/download/v2.56.0.windows.4/' + GIT_INSTALLER,
            'sha256': GIT_SHA256,
            'checksum_source': 'https://api.github.com/repos/git-for-windows/git/releases/tags/v2.56.0.windows.4',
            'installation': 'Manual; no unattended license acceptance or elevation.'}, indent=2) + '\n')
    for arch in PYTHON_SHA256:
        shutil.copy2(downloads / f'python-3.13.17-{arch}.exe', stage)
        command = f'''@echo off
setlocal
if /I not "%USERNAME%"=="test" (
  echo Run only as the disposable VM test user.
  exit /b 1
)
powershell.exe -NoProfile -Command "$p = Join-Path '%~dp0' 'python-3.13.17-{arch}.exe'; $s = Get-AuthenticodeSignature $p; if ($s.Status -ne 'Valid' -or $s.SignerCertificate.Subject -notmatch 'Python Software Foundation') {{ throw 'Python signature validation failed' }}"
if errorlevel 1 exit /b 1
start /wait "" "%~dp0python-3.13.17-{arch}.exe" /quiet InstallAllUsers=0 Include_launcher=0 Include_test=0 PrependPath=0 TargetDir="%LOCALAPPDATA%\\Programs\\SidePulseTestPython-{arch}"
if errorlevel 1 exit /b 1
"%LOCALAPPDATA%\\Programs\\SidePulseTestPython-{arch}\\python.exe" "%~dp0prepare.py" "%~dp0source.tar.gz" "%~dp0source-context.json"
exit /b %ERRORLEVEL%
'''
        (stage / f'prepare-{arch}.cmd').write_bytes(command.replace('\n', '\r\n').encode('ascii'))
    if guest_tools is not None:
        # Copy the actual drivers and installer, not unattended OS setup rules.
        # The regular Windows setup and permission prompts remain in control.
        shutil.copytree(guest_tools / 'Drivers', stage / 'Drivers')
        installers = list(guest_tools.glob('utm-guest-tools-*.exe'))
        if len(installers) != 1:
            raise ValueError('Expected exactly one official UTM guest-tools installer')
        shutil.copy2(installers[0], stage)
        shutil.copy2(guest_tools / 'virtio-win_license.txt', stage)
    inventory = {str(p.relative_to(stage)): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in stage.rglob('*') if p.is_file()}
    (stage / 'disc-manifest.json').write_text(json.dumps(inventory, indent=2) + '\n')
    subprocess.run(['hdiutil', 'makehybrid', '-iso', '-joliet', '-default-volume-name',
                    'SIDEPULSE_TEST', '-o', str(destination / 'harness.iso'), str(stage)], check=True)
    print(destination / 'harness.iso')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--downloads', type=Path, default=ROOT / 'build/windows-vm/media')
    parser.add_argument('--guest-tools', type=Path,
                        help='Read-only mounted official UTM guest-tools disc')
    parser.add_argument('--git-installer', type=Path,
                        help='Verified official Git ARM64 installer; copied but never auto-run')
    args = parser.parse_args()
    build(args.output.resolve(), args.downloads.resolve(), args.guest_tools, args.git_installer)


if __name__ == '__main__':
    main()
