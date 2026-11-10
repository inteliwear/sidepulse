# Fresh macOS testing with UTM

Use an Apple Virtualization macOS VM on Apple Silicon. The harness runs the
repository's real `scripts/setup.sh`, installs Claude Code and Codex CLI from
their official distributions, verifies installed hooks against a running daemon,
and optionally runs real authenticated agent turns. It refuses to run against a
physical Mac or as root. No host agent credentials are copied into the guest.

## Create the baseline

1. In UTM, create **Virtualize → macOS 12+**, using Apple's downloaded IPSW.
   Suggested name: **SidePulse Clean macOS**, 4 CPUs, 8 GB memory, 64 GB disk.
2. Finish macOS Setup Assistant and create a local test account. Sign into its
   desktop before running the harness: SidePulse uses GUI LaunchAgents.
3. Shut down the VM. Keep a stopped clone as the **clean baseline** before
   installing development tools or SidePulse. Clone that baseline for each clean
   installation test. A suspended VM is not a clean baseline. UTM snapshots and
   disposable mode depend on the installed host/UTM version; stopped clones work
   without assuming either feature.

   The host helper refuses running/suspended sources and existing destination
   names, then verifies the new stopped clone and records both UUIDs:

   ```sh
   .venv/bin/python scripts/macos-vm/host.py clone --vm 'SidePulse Clean macOS' --name 'SidePulse Clean Baseline'
   .venv/bin/python scripts/macos-vm/host.py clone --vm 'SidePulse Clean Baseline' --name 'SidePulse Install Test 001'
   ```

   It does not shut down, delete, or reset any machine. Keep the baseline stopped
   and boot only the disposable test copy.
4. Build a bundle on the host:

   ```sh
   .venv/bin/python scripts/macos-vm/host.py prepare
   ```

   This copies the current working tree, including uncommitted source changes,
   into `build/macos-vm/payload.tar.gz`. It includes only package sources, the
   installer, packaging metadata, and the harness. It excludes `.git`, virtual
   environments, hidden files, and symlinks. `manifest.json` records file hashes.
   This is a development install with version `0+vmtest`, not a release build.

## Share `/Volumes/SidePulse` on both sides

Keep the physical device mounted on the **host** as `/Volumes/SidePulse`.
In the stopped test VM's UTM settings, add these directory shares:

| Host folder | Access | Purpose |
| --- | --- | --- |
| `/Volumes/SidePulse` | Read/write | The real LED device's filesystem |
| The repository's `build/macos-vm` folder | Read-only | Installer and harness bundle |

Start the guest. Modern UTM commonly exposes the folders beneath
`/Volumes/My Shared Files`. Inside the guest, `mount-sidepulse.sh` creates
`/Volumes/SidePulse` as an alias to the automounted SidePulse directory. On older
single-share configurations it mounts the `share` VirtioFS tag directly. It never
covers an existing unrelated directory, formats a disk, or creates test files on
the device. If UTM does not expose the folders, fully shut down and start the VM
after adding the shares; a guest reboot alone can be insufficient.

Both paths then reference the host-owned filesystem. Do not attach the same raw
disk to two operating systems. USB passthrough gives the guest exclusive access
and does not satisfy simultaneous host/guest file access.

For meaningful LED observations, pause the host menu-bar job **before** stopping
the host service: the tray can otherwise recover the stopped daemon. Record which
jobs were running, and restore only those jobs afterward. With both jobs running,
the sequence is:

```sh
launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/io.sidepulse.agentstatus.plist"
sidepulse service stop
# Run the guest checks, then shut down the guest before restoring host writers.
sidepulse service start
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/io.sidepulse.agentstatus.plist"
```

Both sides can keep the volume mounted, but two active daemons can overwrite
each other's LED commands. The harness does not stop the host jobs automatically.

VirtioFS tests file discovery and normal file access. It does **not** validate
USB discovery/control, raw READ2ME access, Disk Arbitration, physical eject
prevention, or whether firmware visibly renders an LED program. Test those on
the host or in a separate exclusive USB passthrough run.

## Run inside the guest

Open **Install in Guest.command** from the shared bundle folder. This copies the
source into a new local `~/sidepulse-vm.XXXXXX` directory, establishes the
SidePulse path (which may ask for the guest admin password), and runs:

```sh
sh scripts/macos-vm/guest.sh install --disposable-vm
```

Run from that copied source directory if invoking the command manually. The
install phase rejects an existing SidePulse installation so an update cannot be
mistaken for a clean install. It exercises the existing Python bootstrap on a
stock Mac; it does not preinstall Homebrew, Xcode, or Python to hide prerequisite
failures. If the installer fails, inspect `~/sidepulse-vm-results/install.log`.
In particular, compilation of the eject guard may expose a missing Command Line
Tools dependency on a fresh OS. Preserve that failure before installing tools.

After installation the harness:

- Checks CLI, service, menu-bar LaunchAgent, daemon connectivity, and shared
  device discovery/write permission.
- Installs Claude Code and Codex CLI, records versions, and refreshes SidePulse
  setup after Codex exists so hook trust can be configured.
- Invokes the actual installed hook commands for both providers, checking
  Working → Tool Running → Waiting for Input → Working → Completed.
- Requires silent, successful hook execution, new provider log records, and the
  corresponding daemon states. A fail-open hook that does nothing fails.
- Writes JSON and JUnit reports to `~/sidepulse-vm-results/<run>/`.

The harness runner requires Python 3.11+ (`tomllib`); the stock-Mac installer
currently provisions private Python 3.13. An existing Python 3.10 installation
is outside this fresh-machine fixture.

## Real Claude and Codex tests

Authenticate **inside the guest** using each CLI's normal login flow:

```sh
export PATH="$HOME/.local/bin:$PATH"
claude
codex login
```

Then run from the guest's copied source:

```sh
sh scripts/macos-vm/guest.sh live --disposable-vm
```

Each agent receives a small read-only task in its own new directory: execute
`sleep 2; cat fixture.txt` and return a randomly generated token. The harness
uses `claude -p` with the shell command allowlisted, and `codex exec` with a
read-only sandbox. It does not use Claude's bare mode because that skips hooks.
Calls use the guest's account quota and have a 180-second timeout.

A live pass requires the fixture token, newly emitted prompt/tool/completion
hooks, and Working plus Completed daemon states for the same session. Old logs,
another session, or a successful CLI exit alone cannot satisfy the assertions.
Missing authentication, CLI incompatibility, missing hooks, and timeouts fail
with logs. Without `live`, the report explicitly says `live_agents: not_run`.
Permission-request behavior is replayed; interactive approval dialogs and the
Claude/Codex desktop apps remain separate manual tests.

Use `guest.sh verify --disposable-vm` to repeat installation/replay checks without
reinstalling. Use a fresh baseline clone to repeat the **installation** test.

## Compare the real agent screen with SidePulse

The visual test uses the actual interactive Claude Code or Codex terminal UI,
including its normal permission prompts. It records screenshots of the guest
display and timestamps them alongside daemon snapshots, new hook records, raw
terminal output, and the current `/Volumes/SidePulse/LEDS.LED` contents. It does
not render a pretend agent screen from hooks or run a headless session for this
test.

Keep two stopped baseline clones:

- **Clean OS:** local account and shared folders, before installing SidePulse,
  development tools, or agents. Use this for installer regression tests.
- **Agents ready:** SidePulse and agents installed, agents signed in, Terminal
  screen recording allowed, and the shared device path working. Use copies for
  repeated visual tests. This clone contains the guest's login sessions; keep it
  local.

After authenticating, open **Record Claude in Guest.command** or **Record Codex
in Guest.command** from the shared bundle folder. Alternatively, inside the
guest's copied source, run:

```sh
sh scripts/macos-vm/guest.sh visual-claude --disposable-vm
sh scripts/macos-vm/guest.sh visual-codex --disposable-vm
```

These must run in the guest's visible Terminal, not a headless SSH shell. Keep
the Terminal on the primary guest display, with the SidePulse menu available.
Recording checks authentication first and leaves the normal approval behavior
enabled. Grant Screen Recording to Terminal inside the VM if macOS requests it;
retry after granting access. No screen capture permission on the host is needed.

For each provider:

1. Watch its initial real tool task (`sleep 5; cat fixture.txt`). Leave any
   permission prompt visible for at least three seconds before responding.
2. Leave the completed answer visible for at least three seconds.
3. Ask `Use your interactive question tool to ask me whether this test should
   use red or blue. Wait for my choice, then report it and finish.` Leave the
   question visible for three seconds. This tests a genuine wait even when the
   agent auto-approves its first shell command. Answer it and observe another turn.
4. Optionally run a longer command and interrupt it, or trigger a harmless tool
   failure, to inspect those transitions too.
5. Exit the agent normally after observing the results. Capture stops after at
   most ten minutes. Keyboard input is forwarded to the agent; the harness does
   not synthesize hooks or inject SidePulse status reports.

Open the run's `review.html`. Scrub through the screenshots and label what the
**agent screen** shows independently of the detected state. Beside each screen,
the report shows that session's detected modes at the start/end of capture and
the LED file bytes. The HTML uses local assets and works offline. Raw terminal
output and hooks remain available to diagnose mismatches. Screenshots are taken
roughly once a second; very short transitions can fall between captures.

Use **Export review labels**, then validate the labels using the guest's Python:

```sh
~/.local/share/sidepulse/venv/bin/python scripts/macos-vm/visual.py review \
  /path/to/visual-run /path/to/review-labels.json
```

This produces `visual-result.json`. A pass requires reviewed Working, Waiting
for Input, and Completed screens, matching detection for the same run/session,
and intact screenshot files. Missing review or missing scenario coverage is
**incomplete**, never a pass. A mismatched or missed state fails. Recordings and
labels are evidence for review; they are not an automatic visual classifier.
LED file capture verifies observed bytes only; physical LED appearance requires
a separate camera or human observation.

## Optional host orchestration over SSH

After enabling Remote Login for the guest test user and configuring an SSH key,
use an SSH alias or `user@guest-IP`. The user must also be logged into the VM GUI.
Mount the shared SidePulse path before running:

```sh
.venv/bin/python scripts/macos-vm/host.py run --ssh sidepulse-test --mode install
.venv/bin/python scripts/macos-vm/host.py run --ssh sidepulse-test --mode live
```

The host runner verifies the remote hardware model is a VM, uploads the bundle,
runs the guest harness, and retrieves reports under `build/macos-vm/results-*`,
including after a nonzero test result. SSH uses existing key authentication and
normal host-key verification. It never enables SSH or copies credentials for you.

## Harness regression checks

```sh
.venv/bin/python -m pytest -q tests/test_macos_vm_harness.py
.venv/bin/python -m pytest -q tests/test_macos_vm_visual.py
sh -n scripts/macos-vm/guest.sh scripts/macos-vm/mount-sidepulse.sh
```

The tests use temporary configs and isolated sockets, including real hook
subprocesses for both providers. They do not contact model providers or hardware.

References: [UTM macOS guest support](https://docs.getutm.app/guest-support/macos/),
[UTM automount tag issue](https://github.com/utmapp/UTM/issues/7677),
[Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode),
[Claude programmatic usage](https://code.claude.com/docs/en/headless).

## Collect evidence without enabling SSH

On the host, start the one-shot receiver using the private UTM bridge address
(`ifconfig bridge100` shows it):

```sh
.venv/bin/python scripts/macos-vm/collect.py --bind 192.168.64.1
```

Then run the generated script in the guest:

```sh
sh '/Volumes/My Shared Files/macos-vm/collect-results.command'
```

The receiver accepts one archive through a random, temporary upload URL, binds
only to the specified private interface, and exits after receipt or five minutes.
It saves `guest-evidence-*.tar.gz` and its SHA-256 digest in `build/macos-vm`.
It does not extract or execute uploaded content. The archive includes only
`~/sidepulse-vm-results`; recordings can contain on-screen account information,
so keep evidence local unless reviewed for sharing.

The first actual stock-macOS run on 2026-10-26 failed during compilation of
SidePulse Pro Eject Prevention because Apple Command Line Tools were absent.
The private Python bootstrap, package installation, hooks, background service,
and shared `/Volumes/SidePulse` path were reached. The menu-bar setup and live
agent tests had not run. Installing developer tools afterward is remediation,
not a passing clean-install result. Keep that failure evidence and the clean
baseline before continuing.

The installer now checks Apple Command Line Tools before changing the
installation. If they are missing, it exits with `xcode-select --install`, tells
you to wait until Apple's installer finishes, and asks you to rerun setup.
It also explains how to repair an invalid developer-tools selection.

To resume an already-partial installation after addressing a prerequisite,
run `sh '/Volumes/My Shared Files/macos-vm/repair-in-guest.command'` in the guest.
This reruns the real installer, installs the agent CLIs, and runs the checks.
It writes separate `repair.log` and repair reports, preserving the original
clean-install failure. Repair is not a substitute for a new clean-baseline test.

After installing Apple's tools in the working VM, SidePulse setup and native
Claude/Codex CLI installation succeeded. The 2026-10-26 verification run passed
all seven checks: version, service, menu bar, daemon, shared device, Claude hook
replay, and Codex hook replay. Codex's `hooks.state` trust metadata is excluded
from executable event arrays. These replay passes do not establish live-agent
or visual-comparison success.

After guest sign-in, the first live Claude run passed: it returned the random
fixture token and emitted prompt, tool-start, tool-finish, and stop events. The
first Codex run correctly failed: the bare executable release did not include
`codex-code-mode-host`, so Codex could not run its shell tool. The harness now
installs the complete official `codex-package` archive, preserving companion
binaries, resources, and package metadata. Both the original failure and rerun
reports are retained. Visual recording additionally requires Terminal's Screen
Recording permission inside the guest.

The subsequent live run (`20261026-220514-d5bf61`) passed all nine checks,
including both actual agents. Both emitted `UserPromptSubmit`, `PreToolUse`,
`PostToolUse`, and `Stop`; their matching daemon sessions showed `working`,
`tool_running`, and `completed`. Visual review is still pending Screen Recording
permission and independent screenshot labels; this live pass is not a visual
comparison pass.

### Actual visual results, 2026-10-26

These are the original before-fix results. A subsequent Codex fix passed
16 independently reviewed frames on 2026-10-27, including a real pending
question and completion after answering. See
[the main comparison and fix verification](macos-main-comparison.md#codex-fix-verified-on-2026-10-27)
for the new evidence, installation provenance, and limits.

Terminal Screen Recording was granted inside the VM. Both agents ran in their
real interactive terminal UIs. Screen labels were assigned from the captured
images, then checked against the same session's daemon state and image hashes.

- **Claude passed:** recording `20261026-221742-visual-claude-2b7d51`, 68 frames;
  eight reviewed frames cover working, tool running, waiting for input, and
  completed. All eight matched.
- **Codex failed:** recording `20261026-221928-visual-codex-abe24f`, 207 frames;
  working and completed matched, but four reviewed question-wait frames
  (75, 90, 105, 120) showed a queued question while SidePulse said `tool_running`.
  The actual hooks show `request_user_input_async` returning immediately,
  followed by `clocksleep` calls while the question remains pending. Ordinary
  tool-state detection does not preserve this asynchronous question wait.

Reports, labels, screenshots, terminal recordings, hooks, and LED file samples
are in `build/macos-vm/visual-evidence/sidepulse-vm-results/<recording>/`.
Open `review.html` to scrub the screens; `visual-result.json` contains the
checked verdict. Reviewed labels are retained in the HTML after validation.
This is sampled visual verification, not a claim that every captured frame was
reviewed. The Codex mismatch is preserved as a failing end-to-end finding;
the shared physical LED appearance and native USB behavior remain outside this
VM screen comparison.

LED file sampling was intermittent through the shared volume: 31/68 Claude
frames and 80/207 Codex frames read `LEDS.LED`; the other samples recorded
file-not-found errors. Screen/state comparison is reported separately from this
device-file evidence and does not establish uninterrupted hardware delivery.

After capture, the working VM was shut down normally and cloned to **SidePulse
Agents Ready** (`E1F8C4A3-9A54-406C-B0FF-2A6DA71E3F1F`). The original **SidePulse
Clean Baseline** (`C46D7C0C-7DB3-4EAD-A873-85B53EC9B885`) remains stopped and
unchanged. The agents-ready clone includes guest sign-ins and screen capture
permission. Its SidePulse service is stopped: stop the host writer before
starting the guest service for another test.

## Installation and main upgrade verification — November 5, 2026

Tested `codex/osc7501-integration` at
`031c58150942a3f022befbd721118c4c0ffc3a56` in macOS 27.2 arm64 UTM guests.
The target was a local source bundle installed through the real `scripts/setup.sh`
with `SIDEPULSE_INSTALL_SPEC`, using test version `0+vmtest`.
Official main was installed with `curl -fsSL https://sidepulse.io/setup.sh | bash`;
its installer resolved `ffb1d3c2f3eed3b7e80e32d7876a7e1d67bd5dfb`, version
`0.2.4.dev6+gffb1d3c2f`. The before-upgrade collector hash independently matches
that fetched main source.

The stopped Clean Baseline was cloned into **SidePulse OSC Fresh 2026-11-05**
(`77D00967-E871-4D24-837D-6D6DE2B236E5`). Before adding tools, the OSC installer
exited 1 with Command Line Tools installation/selection instructions and left no
partial SidePulse environment. Apple Command Line Tools 27.2 were then installed.
Before installing SidePulse, that stopped prerequisite-ready VM was cloned into
**SidePulse Main Upgrade 2026-11-05**
(`546A20A9-E03A-4298-935D-44DD56C7EA48`). Both installation paths therefore started
without an existing SidePulse environment. Original baselines were untouched.

| Scenario | Final result |
| --- | --- |
| Stock OS missing developer tools | Expected prerequisite exit; instructions and no partial install verified |
| Fresh OSC install with tools present | 10/10 checks passed |
| Fresh OSC after OS reboot/login | 7/7 checks passed |
| Official main baseline before upgrade | 10/10 checks passed on corrected observer |
| Main to OSC | 7/7 installed-state/hook checks and 11/11 migration assertions passed |
| Repeat OSC installer at the same version | 7/7 checks and 11/11 migration assertions passed |
| Upgraded installation after OS reboot/login | 7/7 checks and 11/11 migration assertions passed |

Native packages were Claude Code 2.1.318 and Codex CLI 0.166.0. Checks execute the
installed provider hook commands and require newly logged events plus matching
states for unique test sessions. They verify the service, menu bar, daemon,
shared filesystem access, and prompt/tool/permission/completion transitions.

Migration assertions verify the existing Python interpreter, a nondefault
SidePulse setting, an unrelated Claude setting, unrelated Claude/Codex hooks
preserved exactly once, managed hook migration, new CLI aliases, removal of the
owned legacy `agent-monitor` alias, installed OSC source, and daemon response.
All 97 non-generated product files matched the bundle after installation.
`_build_info.py` is regenerated by packaging; its installed version/revision/time
are retained separately rather than required to equal the checkout template.
The first strict metadata-mismatch artifact is preserved.

The first main baseline report failed its initial observer/Claude checks because
`latest.json` did not exist yet; the later Codex replay passed. The harness now
retries transient missing/partial legacy state for at most ten seconds and reads
main's legacy daemon snapshot after hook replays have produced session state.
The retry run also archived the prior state file before replay. New regressions
verify that another session cannot satisfy the wait, permanently missing state
still fails, and invalid state schema errors are not hidden. The original
failed report remains unchanged beside the passing retry. No SidePulse product
code was changed for this run.

Local validation: **166 passed, 1 skipped, 308 subtests passed** across installer,
packaging, VM harness, visual-harness, service recovery, environment, runtime,
and tray tests. The focused harness/visual run additionally passed 29 tests.

Evidence root: `build/macos-vm/install-matrix-20261105/`, including `report.html`,
`summary.json`, `context.json`, frozen input bundles/scripts, installer logs,
source manifests, before/after settings and launchers, JSON/JUnit checks, and
preserved initial failures. The clean OS safeguard is in `fresh-guest/.../stock`;
fresh results are in `fresh-guest/.../fresh`; main and upgrade stages are in
`upgrade-guest/.../upgrade`. Original received archives retain SHA-256 digests.

The guest `/Volumes/SidePulse` alias was restored after each reboot. Host tray
recovery restarted the initially stopped host daemon, so both host jobs were
paused before final upgrade stages; no LED contents or physical appearance are
asserted by these reports. Guest-local states and shared filesystem access are
independent of host output. Both test VMs were shut down and the host service and
tray restored after testing.

This is installation, migration, and installed-hook verification. Authenticated
live agent tasks, native USB/eject behavior, physical LEDs, optional privileged
closed-lid setup, and the previously recorded Codex plan-confirmation screen gap
were not retested here.

The later [plan-confirmation fix verification](macos-main-comparison.md#plan-confirmation-fix-verification-2026-11-05)
uses a separate signed-in VM clone and real Codex 0.164.0. All sampled pending
plan states and completions match, with one separate working/tool-running
mismatch retained in the strict visual report. It does not change the scope
or frozen source of the installation matrix above.
