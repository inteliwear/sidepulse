# Linux installation and agent testing

The Linux harness uses a disposable Ubuntu 24.04 ARM64 guest with a real kernel,
systemd user manager, inotify, mount notifications, and installed SidePulse
commands. A container run separately covers the full Python suite. Neither
hook replay nor a passing unit suite is an authenticated real-agent pass.

The Apple Silicon launcher uses the locally installed QEMU with HVF. The
[official Ubuntu cloud image](https://cloud-images.ubuntu.com/releases/noble/release/)
is checked against its saved SHA256SUMS. A
[NoCloud seed](https://docs.cloud-init.io/en/latest/reference/datasources/nocloud.html)
creates the isolated `test` user, its dedicated SSH key, and prerequisites.
There is no password login. SSH listens only on a forwarded host loopback port.
The Ubuntu package's default ttyd service is disabled; an optional test terminal
also listens only on guest loopback and requires a separate SSH tunnel.

## Create a stopped clean baseline

From this repository on an Apple Silicon Mac with QEMU installed:

```sh
python3 scripts/linux-vm/host.py create
python3 scripts/linux-vm/host.py start
python3 scripts/linux-vm/host.py ssh --exec 'cloud-init status --long'
python3 scripts/linux-vm/host.py stop
```

The baseline includes Python/venv, Git, curl, tmux and ttyd. It contains no
SidePulse installation, agent credentials, or agent configuration. Creation
refuses an existing destination; cloning refuses a running baseline. A clone
copies the stopped writable disk layer so subsequent baseline changes cannot
modify its filesystem. Only the verified vendor cloud image is shared as an
immutable backing file. Shutdown is graceful and never falls back to a force
stop. Inspect `console.log` if startup or shutdown fails.

VM disks, SSH keys, firmware variables, and host-key records remain under
`build/linux-vm/`. **Do not serve that directory over HTTP or commit it.** Serve
only a separate evidence directory containing copied reports. The host's real
SidePulse volume is not attached, and its service can keep running.

## Fresh installation

```sh
python3 scripts/linux-vm/host.py clone --source build/linux-vm/baseline \
  --directory build/linux-vm/fresh --port 22281
python3 scripts/linux-vm/host.py start --directory build/linux-vm/fresh
python3 scripts/linux-vm/host.py bundle --output build/linux-vm/input
python3 scripts/linux-vm/host.py upload --directory build/linux-vm/fresh
python3 scripts/linux-vm/host.py ssh --directory build/linux-vm/fresh \
  --exec 'python3 /home/test/sidepulse-source/scripts/linux-vm/guest.py fresh'
```

The bundle records the exact checkout revision, dirty-file list, per-file
hashes, and archive hash. Uploads retain versioned source directories in the
guest. The real `scripts/setup.sh` installs that source through
`SIDEPULSE_INSTALL_SPEC`, using test version `0+linuxtest`. Packaging receives
the recorded Git revision through setuptools-scm metadata; generated
`_build_info.py` is excluded from source-byte comparisons.

The fresh precondition rejects an existing installation. Checks require an
enabled, active systemd unit, new hook events from both installed agent configs,
matching states for unique sessions, and matching installed source bytes.
The native test mounts its own small tmpfs under `/media/test`, observes
arrival/removal notifications, verifies discovery/removal, and writes `off`
through the installed CLI. It does not touch a physical device. The crash test
kills only the unit's main process and requires both a new PID and a responding
daemon after systemd recovers it.

## Official main, upgrade, repeat and reboot

Create a second clone with another directory and port, then run these guest
commands through `host.py ssh --directory build/linux-vm/upgrade --exec ...`:

```sh
python3 /home/test/sidepulse-source/scripts/linux-vm/guest.py main
python3 /home/test/sidepulse-source/scripts/linux-vm/guest.py upgrade
python3 /home/test/sidepulse-source/scripts/linux-vm/guest.py repeat
```

`main` downloads and runs the official `https://sidepulse.io/setup.sh` with its
default install source. The resolved package version and installer are retained.
`upgrade` seeds a nondefault SidePulse setting and unrelated Claude/Codex hooks.
Migration assertions check the original interpreter, retained settings, unrelated
hooks present exactly once, unique managed hooks, CLI aliases, and an actual
service restart. The PID assertion runs **before** crash recovery deliberately
replaces the daemon. `repeat` tests replacement when the version is unchanged.

After the checks finish, stop and start the guest with `host.py`, then run:

```sh
python3 /home/test/sidepulse-source/scripts/linux-vm/guest.py verify --stage reboot
```

A changed kernel boot ID is required. A simple service restart cannot satisfy
the reboot check. Service, installed hooks, source hashes, mount behavior, and
crash recovery are verified again. JSON and JUnit reports retain failures;
reruns create new timestamped result directories.

`.github/workflows/linux-installation.yml` adds fresh and official-main upgrade
paths on native Ubuntu 24.04 GitHub runners, with an isolated Unix account and
real systemd manager. It uploads evidence even on failures. CI does not reboot
the runner or authenticate paid agent accounts; those remain VM checks.

## Real Claude and Codex

Install the vendor CLIs in the chosen working VM:

```sh
python3 /home/test/sidepulse-source/scripts/linux-vm/guest.py agents
```

Claude uses its official installer. Codex uses the vendor's Linux package,
preserving helper binaries and checking the release asset digest when provided.
Versions, download identity and install logs are saved separately. Sign in
inside this guest; host credential directories are never copied into it.

Agent provisioning also installs the distro's bubblewrap package and probes
namespace creation. Ubuntu 24.04 may require its supplied AppArmor profile;
the harness follows the [official Codex prerequisites](https://learn.chatgpt.com/docs/sandboxing#prerequisites)
and loads that profile only when the probe fails. Existing profiles are not
replaced. The global unprivileged-user-namespace restriction stays enabled.
The initial real Codex failure without this prerequisite is retained rather
than rerun with unrestricted execution.

Start an optional real browser terminal from the guest:

```sh
systemd-run --user --unit=sidepulse-test-terminal --collect \
  ttyd -W -i 127.0.0.1 -p 7681 \
  tmux new-session -A -s sidepulse-test /bin/bash
```

Use an SSH loopback tunnel from host port 8768 to guest port 7681, with the
dedicated key and host-key file created by `host.py`. Finish both sign-ins in
that terminal, then run `guest.py live`. The live test requires a real shell
tool to read a newly generated fixture token, new hooks from this workspace's
session, and working/completed daemon observations. Merely printing a canned
answer, returning success from a fail-open hook, or finding a stale session
cannot pass.

For interactive questions and native Codex plan confirmation, start
`terminal.py record claude|codex --workspace PATH --output PATH` using the
installed SidePulse Python, then operate that real agent in the tmux session.
Authenticate first: do not capture sign-in screens or credentials. The recorder
saves actual terminal cells and contemporaneous daemon snapshots. Finish by
creating `stop` inside its result directory; a timeout is a recording error.
Label terminal captures independently before reading detected modes, then run
`terminal.py review RESULT_DIRECTORY LABELS_JSON`. Labels contain the recording
`run_id` and a `labels` object mapping frame IDs to expected modes. A pass needs
working, waiting, and completed coverage, unchanged capture hashes, and no
mismatches. These captures are explicitly terminal cells, not pixel screenshots;
save separate browser screenshots when inspecting the rendered terminal.

`host.py collect --directory VM_DIRECTORY --output EVIDENCE_DIRECTORY` retrieves
only `/home/test/sidepulse-linux-results`. It does not retrieve credential
directories or the VM's SSH private key. Stop the working guests after collecting
evidence, unless one is intentionally waiting for user sign-in.

## Full Linux suite

Extract a frozen source bundle inside a disposable Linux container and run:

```sh
python3 scripts/linux-vm/unit.py --output /evidence/unit
```

This installs the declared package plus test dependencies, runs the entire
suite, and records Python/kernel/architecture, source context, exit codes,
logs and JUnit. Do not interpret skipped macOS UI tests as Linux UI coverage.
This is separate from native VM service, reboot and authenticated-agent tests.

## Verification on November 5, 2026

The tested product source is `codex/osc7501-integration` at `337c703`, with
the new harness files recorded as uncommitted inputs. No SidePulse product
code was changed in this harness task. All 98 non-generated installed product
files match the frozen input manifests. Official main resolved
`ffb1d3c2f3eed3b7e80e32d7876a7e1d67bd5dfb`, version `0.2.4.dev6+gffb1d3c2f`.

| Real Ubuntu VM scenario | Selected final result |
| --- | --- |
| Fresh branch installation | 8/8 checks |
| Fresh installation after a real OS reboot | 9/9 checks |
| Official main baseline | 5/5 checks |
| Main to branch upgrade | 9/9 checks |
| Same-version reinstallation | 9/9 checks |
| Upgraded installation after a real OS reboot | 10/10 checks |

All **50 selected VM checks passed**. A separate live run passed **10/10**
checks, including real signed-in Claude Code **2.1.318** and Codex CLI
**0.166.0** reading newly generated fixture tokens through their shell tools.
Both agents also passed after the final cold start with bubblewrap/AppArmor
prerequisites installed. Account sign-ins persist only inside the test guest.
The global AppArmor user-namespace restriction remained `1`.

The full suite in a disposable Linux ARM64/Python 3.12 container passed
**959 tests and 542 subtests**, with **82 skips** for unavailable or
platform-specific checks. The added harness has 18 safety/evidence regressions.
The native x86 CI workflow is added but has not run for these new changes.

Real interactive terminal review passed **30/30 independently labeled samples**:
six Claude samples from 171 captured terminal frames and 24 Codex samples from
591 frames. Coverage includes working, active tools, unanswered structured
questions, three native Codex plan menus, No/stay, Escape, new ordinary answers,
and acceptance followed by real shell execution and completion. An explicitly
running background terminal was labeled `tool_running` before viewing detected
modes. No/Escape leaves the plan awaiting the next submitted instruction.
Browser pixel screenshots were saved separately. These results cover sampled
terminal states, not every frame or physical LED appearance.

Initial failures are retained: incomplete support-file bundles/build metadata,
crash-test targeting and readiness, the automatic-writer/manual-CLI assertion
race, a Codex task before its Linux execution prerequisites were installed, and
an interrupted first Claude capture with a daemon-response error. That capture
was not promoted to a pass; a separate short recording passed afterward.

Evidence is under `build/linux-vm/evidence/`, with JSON/JUnit reports, source
contexts, logs, recordings, independent labels and browser screenshots. All
Linux working VMs and the clean baseline are stopped. The host SidePulse service
remained running, and no host credentials or physical device volume were shared.
