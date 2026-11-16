#!/usr/bin/env python3
"""Bundle this working tree, or run the bundle over SSH in a logged-in macOS VM."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import shlex
import subprocess
import tarfile
import time

ROOT = Path(__file__).resolve().parents[2]
FILES = ("pyproject.toml", "README.md", "LICENSE", "MANIFEST.in", "LEDS_FORMAT.md", "scripts/setup.sh")
TREES = ("src", "scripts/macos-vm")
UTMCTL = "/Applications/UTM.app/Contents/MacOS/utmctl"


def utm_inventory():
    result = subprocess.run([UTMCTL, "list"], check=True, text=True, capture_output=True)
    machines = []
    for line in result.stdout.splitlines()[1:]:
        fields = line.split(None, 2)
        if len(fields) == 3:
            machines.append(dict(zip(("uuid", "status", "name"), fields)))
    return machines


def clone_stopped_vm(identifier, name, output):
    """Never stop, overwrite, or delete a VM as a side effect of cloning."""
    machines = utm_inventory()
    matches = [vm for vm in machines if identifier in (vm["uuid"], vm["name"])]
    if len(matches) != 1:
        raise ValueError("Source must identify exactly one registered UTM VM")
    source = matches[0]
    if source["status"] != "stopped":
        raise ValueError("Shut down the source VM before cloning; suspended is not sufficient")
    if not name.strip() or any(vm["name"] == name for vm in machines):
        raise ValueError("Choose a nonempty, unused VM name")
    subprocess.run([UTMCTL, "clone", source["uuid"], "--name", name], check=True)
    old_ids = {vm["uuid"] for vm in machines}
    created = [vm for vm in utm_inventory() if vm["name"] == name and vm["uuid"] not in old_ids]
    if len(created) != 1 or created[0]["status"] != "stopped":
        raise RuntimeError("Clone command returned, but a new stopped VM could not be verified")
    output.mkdir(parents=True, exist_ok=True)
    record = {"source": source, "clone": created[0], "created_at": time.time(),
              "note": "Stopped clone; OS cleanliness and agent authentication require separate verification"}
    path = output / ("clone-" + created[0]["uuid"] + ".json")
    path.write_text(json.dumps(record, indent=2) + "\n")
    print(f"Stopped clone: {name} ({created[0]['uuid']})\nRecord: {path}")
    return record


def source_files(root):
    paths = [root / name for name in FILES]
    paths += [p for directory in TREES for p in (root / directory).rglob("*") if p.is_file()]
    return sorted(p for p in paths if not p.is_symlink() and not any(
        part == "__pycache__" or part.endswith(".egg-info") or part.startswith(".")
        for part in p.relative_to(root).parts) and p.suffix not in {".pyc", ".pyo"})


def prepare(root, output):
    output.mkdir(parents=True, exist_ok=True)
    files = source_files(root)
    manifest = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    with tarfile.open(output / "payload.tar.gz", "w:gz") as tar:
        for path in files:
            tar.add(path, arcname=str(path.relative_to(root)), recursive=False)
        content = json.dumps(manifest, indent=2).encode()
        info = tarfile.TarInfo("vm-source-manifest.json")
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    # Shared storage holds only the bundle; installation and results live in the guest.
    (output / "Install in Guest.command").write_text('''#!/bin/sh
set -eu
case "$(sysctl -n hw.model)" in VirtualMac*) ;; *) echo 'Guest VM only'; exit 2 ;; esac
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
WORK=$(mktemp -d "$HOME/sidepulse-vm.XXXXXX")
tar -xzf "$HERE/payload.tar.gz" -C "$WORK"
echo "Harness copied to $WORK"
sh "$WORK/scripts/macos-vm/mount-sidepulse.sh"
sh "$WORK/scripts/macos-vm/guest.sh" install --disposable-vm
''')
    (output / "Install in Guest.command").chmod(0o755)
    repair = output / "repair-in-guest.command"
    repair.write_text((output / "Install in Guest.command").read_text().replace(
        'guest.sh" install --disposable-vm', 'guest.sh" repair --disposable-vm'))
    repair.chmod(0o755)
    for mode in ("verify", "live"):
        launcher = output / f"{mode}-in-guest.command"
        launcher.write_text(repair.read_text().replace(
            'guest.sh" repair --disposable-vm', f'guest.sh" {mode} --disposable-vm'))
        launcher.chmod(0o755)
    for provider in ("claude", "codex"):
        launcher = output / f"Record {provider.title()} in Guest.command"
        launcher.write_text('''#!/bin/sh
set -eu
case "$(sysctl -n hw.model)" in VirtualMac*) ;; *) echo 'Guest VM only'; exit 2 ;; esac
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
WORK=$(mktemp -d "$HOME/sidepulse-vm.XXXXXX")
tar -xzf "$HERE/payload.tar.gz" -C "$WORK"
''' + f'sh "$WORK/scripts/macos-vm/guest.sh" visual-{provider} --disposable-vm\n')
        launcher.chmod(0o755)
    return output / "payload.tar.gz"


def remote_command(target, argv, *, capture=False):
    return subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", target,
                           shlex.join(argv)], check=capture, text=True, capture_output=capture)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run", "clone"))
    parser.add_argument("--output", type=Path, default=ROOT / "build/macos-vm")
    parser.add_argument("--ssh", help="SSH alias or user@guest-IP; existing key authentication required")
    parser.add_argument("--mode", choices=("install", "verify", "live"), default="install")
    parser.add_argument("--vm", help="Source UTM UUID or exact name for clone")
    parser.add_argument("--name", help="Unused name for the new stopped clone")
    args = parser.parse_args()
    if args.command == "clone":
        if not args.vm or not args.name:
            parser.error("clone requires --vm and --name")
        try:
            clone_stopped_vm(args.vm, args.name, args.output.resolve())
        except (ValueError, RuntimeError) as exc:
            parser.error(str(exc))
        return 0
    bundle = prepare(ROOT, args.output.resolve())
    print(f"Bundle: {bundle}", flush=True)
    if args.command == "prepare":
        return 0
    if not args.ssh or args.ssh.startswith("-") or any(c.isspace() for c in args.ssh):
        parser.error("run needs a valid --ssh destination")
    # Check identity before uploading or installing anything.
    model = remote_command(args.ssh, ["sysctl", "-n", "hw.model"], capture=True).stdout.strip()
    if not model.startswith("VirtualMac"):
        parser.error(f"SSH destination is not a macOS VM: {model}")
    work = remote_command(args.ssh, ["mktemp", "-d", "/tmp/sidepulse-vm.XXXXXX"], capture=True).stdout.strip()
    # mktemp output is used by scp, so allow only the exact expected path shape.
    if not work.startswith("/tmp/sidepulse-vm.") or not work.removeprefix("/tmp/sidepulse-vm.").isalnum():
        parser.error("Unexpected guest temporary directory")
    subprocess.run(["scp", "-o", "BatchMode=yes", str(bundle), f"{args.ssh}:{work}/payload.tar.gz"], check=True)
    remote_command(args.ssh, ["tar", "-xzf", f"{work}/payload.tar.gz", "-C", work], capture=True)
    result = remote_command(args.ssh, ["/bin/sh", f"{work}/scripts/macos-vm/guest.sh", args.mode, "--disposable-vm"])
    reports = args.output.resolve() / ("results-" + time.strftime("%Y%m%d-%H%M%S"))
    reports.mkdir(parents=True, exist_ok=True)
    copied = subprocess.run(["scp", "-r", "-o", "BatchMode=yes",
                             f"{args.ssh}:sidepulse-vm-results/.", str(reports)])
    print(f"Guest: {work}\nReports: {reports}")
    return result.returncode or copied.returncode


if __name__ == "__main__":
    raise SystemExit(main())
