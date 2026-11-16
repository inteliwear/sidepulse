# Native Windows test environment

Native Windows installation and upgrade probes have run and exposed failures.
The Linux results are in [linux-vm.md](linux-vm.md). A Windows VM being created
or a probe compiling on macOS does **not** count as a Windows pass.

## Recorded run: 2026-11-06

The stopped clean baseline was cloned separately for fresh ARM64 Python, fresh
x64 Python, and main-to-branch upgrade tests. All product probes ran as the
non-elevated `test` user. Python 3.13.17 x64 ran under Windows ARM64 emulation;
this does not establish native x64 hardware coverage.

| Scenario | Passed | Failed |
| --- | ---: | ---: |
| Fresh ARM64 Python | 10 | 5 |
| Fresh x64 Python on Windows ARM64 | 10 | 5 |
| Public main package install | 8 | 4 |
| Main to frozen branch | 10 | 5 |
| Same-version reinstall | 9 | 4 |

Main resolved to `ffb1d3c2f3eed3b7e80e32d7876a7e1d67bd5dfb`.
The frozen working source records base revision
`337c70381f9def5e194b74caf748f6c2174cfddf`; its manifest captures the uncommitted
harness files too. Fresh and upgraded installations matched all 98
non-generated product files. Setup preserved the seeded settings, unrelated
hooks, interpreter identity, and one managed command per hook event.

The original runs failed background-service startup, device writes (`fcntl`),
the resulting file contents, and status initialization (`os.getuid`). Full-suite
runs also stopped at four platform-specific collection errors. After correcting
collection guards, a separate x64 rerun against the unchanged installed product
executed: **714 passed, 164 failed, 77 errors, 90 skipped, 422 subtests passed**.
The remaining suite failures include product incompatibilities and tests that
assume Unix shell commands, permission bits, or symlink privileges; they are not
164 distinct product bugs. Original evidence remains intact.

The native ARM64 install successfully built dependency wheels from source; the
absence of prebuilt ARM64 wheels did not block package installation. USB runtime
behavior has not been established.

Collected evidence is private under `build/windows-vm/collected/`. The report
renderer accepts a separate directory containing reviewed artifacts only:

```sh
python3 scripts/windows-vm/report.py build/windows-vm/published-20261106
```

Windows real-agent execution, screen comparison, mounted-volume events, and
reboot/autostart remain unverified. These results do not establish working
end-to-end Windows support.

## Windows runtime fixes in progress: 2026-11-06

The working branch now includes a native PowerShell installer, per-user Windows
logon startup, authenticated local status transport, process-owned write locks,
private credential files, and ConPTY terminal wrapping. Windows-generated hooks
use a managed PowerShell command and still fail open if the interpreter disappears.

The first fixed installation passed all 13 functional probes and six native
runtime tests. A clean ARM64 installation through `scripts/setup.ps1` then
passed all 13 functional probes and eight runtime tests. The later fresh x64
installation, main-to-branch upgrade, and repeat install also passed their
functional probes. Their broader suites exposed additional portability issues;
those failed runs remain retained. They must not be reported as full passes.

The completed installation/reboot matrix uses R7, archive SHA-256
`6fe22ee1572ad1da420a1882c6c8b5cc7b77a60d2adba736c323e87902bff17d`.
It preserves LF bytes on Windows and keeps a configured folder's parent watcher
armed across deletion and recreation. Linux regression against this input
completed with **989 passed, 88 skipped, and 554 subtests passed**.

The previous R6 reinstall suites passed on ARM64 and emulated x64 Python
(**925 passed, 132 skipped** each). Its clean ARM64 suite retained three failures:
a folder recreation race, an HTTP rejection test affected by Windows TCP reset,
and a ConPTY cold-start timeout. R7 fixes the watcher race, checks HTTP rejection
before sending the body, and gives the real ConPTY subprocess a 60-second startup
budget while retaining its exit-status and OSC assertions. R7 validation on 2026-11-07 passed all three full native suites: clean ARM64,
direct public-main-to-branch upgrade, and x64 Python reinstall on ARM64 Windows.
Each completed with **928 passed, 132 skipped, and 532 subtests passed**, plus
**15/15 installation checks**. The additional ARM64 repeat installer passed
13/13 functional checks. All 103 installed product files matched the frozen
source. Main still resolved to the historical revision listed above.

All three R7 VMs then passed a real reboot and normal sign-in check: the kernel
boot identity changed, the registered service responded without a service-start
call, and installed product files, interpreter, seeded preferences, and hooks
were unchanged. Each passed all four reboot checks. Authenticated Windows
agent-screen tests are tracked separately below; physical USB/LED behavior and native
x64 hardware remain unverified. Earlier failures are retained separately.

### Live Windows keyboard correction: R8, 2026-11-07

The first signed-in Claude task on R7 exposed a real input relay defect: Return
could appear as a Win32 key-event sequence in the prompt and submit an unintended
follow-up. The wrapper mixed CRT character input with Windows virtual-terminal
input. R8 reads the console's UTF-8 byte stream directly and restores its modes
and code page on exit. Its frozen archive SHA-256 is
`418f65e4a00ef94a406210c018fd3ffb6ca4ed0b9189e70d9f4ef968d4931a89`.

A nested native ConPTY regression sends Unicode, an arrow key, and Return and
requires exact input bytes plus the child's exit status. It failed on the
unchanged installed R7 product and passed on installed R8. The repeated real
Claude task then accepted Blue once, wrote the expected fixture result, kept a
clean prompt, and exited normally with code zero. Independent question and
completion screenshots both matched the installed daemon. The active screen
comparison remains failed: its label was `working` while the captured screen
showed a file tool and the daemon reported `tool_running`. Keep that original
label/result; this run does not establish a complete passing visual comparison.
At this R8 checkpoint, Codex authentication and its real question/plan screens
were still pending; the later results below supersede that gap. R8
Linux regression passed with **990 passed, 89 skipped, 554 subtests passed**.

The three R7 installation/reboot results above do not imply that the complete
matrix has been repeated on R8. Screenshot evidence preserves native PNG or JPEG
bytes and hashes; authentication screens and credentials are excluded.

### Native Codex plan-picker reconciliation

The real Codex 0.167.0 task exposed a hook-silent case: choosing **No, stay in
Plan mode** or pressing Escape closes the picker without a new hook event.
Question, approval, plan acceptance, fixture writes, and completion worked.
A keyboard-handoff attempt (R9) passed its unit suite but failed the real
No/Escape screen comparisons; those captures are retained. It has been removed.

The terminal wrapper now reconciles Codex's native OSC 7501 idle signal with
one matching local hook plan wait. Codex clears its terminal scope while the
plan picker is visible, then reports idle on dismissal. The wrapper requires
one local Codex session in its working directory updated since launch. It uses
the acknowledged local sequence so a newer turn or question wins atomically.
Concurrent sessions, other publishers/directories, and ordinary assistant
questions are left alone. No screen text, keyboard input, or timeout is used to
guess dismissal. Unwrapped Codex has no verified equivalent dismissal signal.

R11 archive SHA-256:
`5dd3e427de28264c21e9775251524294571aa0911e603bcf4ba40fd85c0eba52`.
The full Linux suite passed **999 tests, 89 skipped, 557 subtests**. Native Windows
ARM64 passed **939 tests, 132 skipped, 535 subtests** against the installed
product, with all product source hashes checked first. These are separate from
the earlier R7 clean-install, public-main upgrade, and reboot matrix. The reviewed
report identifies the artifact used for each real-agent screen comparison.

The final R12 guard additionally preserves ordinary assistant text questions at
an idle composer. R12 archive SHA-256 is
`af277db8d7d71f7b5025f9c6277335bc56f9e37dd77d87b6329eec252d0ec7a2`.
Its latest full suites passed **1,000 Linux tests** (89 skipped, 557 subtests) and
**940 native Windows ARM64 tests** (132 skipped, 535 subtests). The final R12 real
Codex run passed all eight independent screen comparisons, including No,
Escape, acceptance, and a streaming follow-up, with a correct fixture result
and normal exit. The earlier R11 run also passed eight comparisons. The R9
Claude run passed working, question, and completion comparisons. The report
retains those artifact versions and all earlier failed comparisons separately.

To reproduce the interactive Codex scenario, launch the signed-in CLI through
`agents.py run codex` with `--no-alt-screen -a on-request -s workspace-write` as
agent arguments. Enter `/plan`, then ask it to read `fixture.txt` with the shell,
use `request_user_input` to choose Blue or Red, and propose writing `result.json`
with the fixture token and selected color. Choose Blue. Capture the pending
plan; choose **No, stay in Plan mode** and capture the idle prompt. Ask for the
same plan again, dismiss it with Escape, and capture the idle prompt. Ask again,
accept the plan, and capture working and completed screens. Require the exact
JSON result, fresh actual hook/tool events, and independent labels before
inspecting detection. Authentication always precedes recording. A commentary
preamble can provide a stable working capture before the first tool starts;
working-to-tool transitions remain failed exact comparisons when captured.

### Development Windows installer

From a reviewed checkout, in PowerShell:

```powershell
& ([scriptblock]::Create((Get-Content -Raw .\scripts\setup.ps1))) -SourceDirectory (Get-Location).Path
```

The installer requires Python 3.10+ with pip and venv, explains how to install it
when missing, and creates a private per-user environment. It does not require
administrator access or a machine-wide execution-policy change. Re-running it
uses the existing interpreter and preserves agent settings and unrelated hooks.
This script is a branch addition; its presence here is not a claim that a public
Windows installer has been released.

### Reboot and live-screen proof

`scripts/windows-vm/reboot.py stamp --root ROOT` records the kernel boot identity
and installed interpreter and product hashes. Use the same optional `--run-id`
for stamp and check to retain separate baselines. Close completed installer consoles, perform a
normal Windows reboot, sign in, then run `reboot.py check --root ROOT` using the
installed interpreter. The check only queries the registered daemon: it never
starts a substitute service. A matching boot identity fails the check.

`scripts/windows-vm/agents.py run` starts an actual signed-in Claude or Codex CLI
through the installed ConPTY wrapper. Prepare separate workspaces and evidence
folders beneath the disposable user's `SidePulseAgentTests` directory. Each
fixture contains `fixture.txt`; the agent must read its token, ask the user to
choose a color, and write `result.json` containing `token` and `choice: "blue"`.
Capture real VM screenshots independently and label working, waiting-for-input,
and completed states before inspecting detection results. The review compares
installed-daemon observations immediately before and after each intact screenshot.
It also requires fresh workspace hook sessions, actual tool events, and the
correct file result. Authentication must finish before recording.

`runtime_report.py` accepts a separate reviewed evidence directory with
`runs.json` paths for `fresh-arm64`, `upgrade-arm64`, `repeat-x64`, each corresponding
`-reboot`, and `claude`/`codex` visual results. Missing full-suite, reboot, or live
proof keeps the report incomplete or failed. Never publish agent credentials,
authentication screens, full VM profiles, or the entire private build directory.

## Baseline

Use a new Windows VM, with no SidePulse installation and no copied host agent
credentials. On Apple Silicon, UTM can virtualize ARM64 Windows. Keep its TPM 2.0
device enabled. Use Microsoft's Windows ARM64 ISO and compare its SHA-256 with
the value shown on Microsoft's download page before booting it.

With the host's UTM 4.6.5, Windows welcome setup first showed an empty panel and
then froze input at the country screen. Removing only this test VM's Intel HDA
sound device restored keyboard input after restarting the guest. This harness
does not require audio. The failed attempts and diagnostics are retained under
`build/windows-vm/evidence/`.

The current machine is `SidePulse Windows Clean 2026-11-05`, UTM UUID
`1E341AB4-3B9B-46EA-9545-0DD3DA864D48`, with 8 GiB RAM, four virtual CPUs, and a
128 GiB sparse disk. The Windows 11 Pro installer was selected. Its source ISO
is Microsoft's English ARM64 build `26300.9457`, SHA-256:

```text
8992f2d2cfd2fa647e579e9eb31fc21f34da8333c29b6170fde17d2ed6623e9c
```

Finish Windows setup normally and create the disposable `test` account. Install
UTM's official guest tools and a supported Python interpreter. Record the actual
OS, Python, CPU architecture, tool versions, and downloaded artifact hashes.
An x64 interpreter on ARM64 Windows must be identified as such.

The working VM has both per-user Python 3.13.17 interpreters prepared. Its Git
prerequisite is the official PortableGit 2.56.0.4 ARM64 package, extracted by the
normal `test` user into `C:\Users\test\Tools\Git`. The package SHA-256 is
`044110f9d951858452ddb5e4b5415ea0f2b5b2598e794d6fec77fe28b9aa8cc0`, verified
against the official Git for Windows release asset. The regular Git installer
was canceled at its admin prompt. Test launchers add the portable `cmd` directory
to their own process PATH; no global PATH change is required.

Before installing SidePulse, shut the guest down and clone the stopped VM as
the clean baseline. Use separate fresh and main-upgrade clones. A baseline
containing signed-in accounts should remain private and should be identified
separately from an OS-only baseline.

For this two-interpreter matrix, run `scripts/windows-vm/baseline.py` using the
ARM64 Python interpreter in the normal test session before shutting down. It
checks both prepared interpreters, refuses an existing SidePulse distribution,
checks portable Git, and saves a receipt under `SidePulseHarness`. This receipt
proves preparation only; it does not count as a product test.

The host helper registers the specific Windows VM by UUID and refuses to clone
it while it is running. For example, after preparation and normal shutdown:

```sh
python3 scripts/windows-vm/host.py register --directory build/windows-vm/clean \
  --uuid 1E341AB4-3B9B-46EA-9545-0DD3DA864D48
python3 scripts/windows-vm/host.py clone --source build/windows-vm/clean \
  --directory build/windows-vm/fresh --name "SidePulse Windows Fresh 2026-11-05"
python3 scripts/windows-vm/host.py clone --source build/windows-vm/clean \
  --directory build/windows-vm/upgrade --name "SidePulse Windows Upgrade 2026-11-05"
python3 scripts/windows-vm/host.py start --directory build/windows-vm/fresh
```

Use `host.py stop --directory ...` for a normal guest shutdown. With Guest Tools,
it calls Windows' `shutdown.exe /s /t 0`; it omits `/f` and positive timeouts that
implicitly force applications closed. Without the agent it requests the virtual
power button. It retains a VM that fails to respond. Registration and clone records are private host
metadata; they are not proof that Windows setup or SidePulse tests passed.

VM disks, account data, installers, and collected evidence belong under
`build/windows-vm/` or UTM's private storage. Do not serve that whole directory
over HTTP or commit it. Publish only reviewed evidence.

On the host, place the Python 3.13.17 ARM64 and AMD64 installers from python.org
in `build/windows-vm/media/`, then freeze a test disc:

```sh
python3 scripts/windows-vm/media.py --output build/windows-vm/input-disc-new
```

The builder requires python.org's exact published installer hashes and refuses
to overwrite frozen inputs. Mount `harness.iso` in the guest after Windows setup.
Run `prepare-arm64.cmd` as the normal `test` user. It verifies the Python
publisher signature, installs Python for that user, verifies and extracts the
source bundle, and creates the VM marker. `prepare-amd64.cmd` provides a separate
x64 interpreter when testing that configuration. Preparation does not install
SidePulse; capture the stopped clean baseline before running the probes.

To include UTM's drivers, mount the official guest-tools ISO read-only on the
host and add `--guest-tools "/Volumes/UTM Guest Tools"` to the media builder. It
copies the driver files, installer, and license; it does not copy unattended
Windows setup instructions. At Windows' network setup, choose **Install driver**
and select `D:\Drivers\NetKVM\w11\ARM64` on the test disc (use its actual drive
letter). Continue through the normal Windows account setup.

## Initial installation probes

`scripts/windows-vm/probe.py` is an initial native Windows probe runner. It
requires a disposable VM marker at `%USERPROFILE%\SidePulseHarness\vm.json` containing:

```json
{"kind": "sidepulse-disposable-windows"}
```

It also verifies a virtual-machine model, the `test` profile, and a non-elevated
process. Its separate CI scope accepts only a Windows GitHub Actions runner.
Configuration changes are confined to the selected harness directory beneath
the user's profile.

From a frozen source checkout or verified source bundle, run in PowerShell:

```powershell
$env:PATH = "$env:USERPROFILE\Tools\Git\cmd;$env:PATH"
$python = Join-Path $env:LOCALAPPDATA 'Programs\SidePulseTestPython-arm64\python.exe'
& $python scripts/windows-vm/probe.py fresh --root "$env:USERPROFILE\SidePulseFresh" --full-suite
```

Use a different clean clone and harness root for main and upgrade:

```powershell
& $python scripts/windows-vm/probe.py main --root "$env:USERPROFILE\SidePulseUpgrade"
& $python scripts/windows-vm/probe.py upgrade --root "$env:USERPROFILE\SidePulseUpgrade" --full-suite
& $python scripts/windows-vm/probe.py repeat --root "$env:USERPROFILE\SidePulseUpgrade"
```

Run from the source directory printed by preparation. For the separate x64
configuration, use `SidePulseTestPython-amd64\python.exe` and separate probe
roots. Preparation keeps separate frozen source directories for each Python
architecture. Python is not added to the user's global PATH by the bootstrap.

Main uses Python's package installer with
`git+https://github.com/inteliwear/sidepulse.git@main`; Git must be installed.
The media builder's optional `--git-installer` accepts
`Git-2.56.0.4-arm64.exe` only when it matches the SHA-256 published on the official
Git for Windows release asset. It copies the installer onto the disc without
running it, accepting its license, or bypassing its Windows permission prompt.
The installed package's `direct_url.json` records the resolved commit. This is
a package-install test, not a claim that the Unix `setup.sh` is a native Windows
installer.

Each run retains command logs and a JSON result under its root's `evidence/`
directory. The optional full Python suite also saves JUnit XML. An unsupported
background service, failed write, or failed local messaging check remains a
failure. The runner does not start a substitute daemon to turn service setup
green. Its directory write fixture is not a removable-volume test.

After correcting test-platform guards, `probe.py unit --root ...` can rerun the
full suite against an existing probe installation. It first requires every
non-generated installed product file to match the selected frozen source. It
does not reinstall SidePulse or relabel the original failed test run.

With Guest Tools installed, collect a probe's evidence into a new private host
directory:

```sh
python3 scripts/windows-vm/host.py collect --directory build/windows-vm/fresh \
  --root SidePulseFresh --output build/windows-vm/collected/fresh
```

The collector accepts only a probe root name beneath `C:\Users\test`, and copies
only its `evidence` subdirectory. It verifies a unique guest completion receipt
and the archive hash; UTM's early or zero-error exit alone is not proof of
completion. It does not collect agent credentials or the probe's configuration
home. Review collected artifacts before publishing them.

The probes also repeat setup and verify exactly one managed command for every
installed Claude/Codex event. Fresh and main runs seed controlled user settings
and an unrelated hook for each provider. Upgrade and repeat runs require those
values and the original interpreter to survive.

`.github/workflows/windows-installation.yml` supplies the native x64 CI version
of the probes, with separate fresh and main-upgrade jobs and retained artifacts
after failures. It is manual/reusable while Windows support is being measured;
unsupported service setup remains a failure. It has not run for these new,
uncommitted changes. CI does not authenticate paid agent accounts or establish
desktop, real reboot, removable-volume, or physical-device coverage.

## Coverage still required

- Real mounted test-volume arrival/removal and physical USB/LED behavior.
- Native x64 hardware and execution of the new CI workflows.
- Unwrapped Codex plan dismissal and older CLI versions.

Signed-in native Windows Claude and Codex fixture tasks and independently
labeled screen comparisons passed as recorded above. Keep replay, authenticated
agent execution, screenshots, and physical USB/LED observations separate; the
passing filesystem fixtures do not establish physical device behavior.

## Terminal rendering in UTM

One x64 test clone displayed a blank Windows Terminal pane despite completed
probe logs. Setting `experimental.rendering.software` to `true` in that clone's
user Terminal settings and restarting Terminal made a normal command prompt and
test marker visible. Preserve the original settings and screenshot before
changing this test-only rendering preference. See Microsoft's
[rendering settings documentation](https://learn.microsoft.com/en-us/windows/terminal/customize-settings/rendering).
This console check does not count as an agent-screen comparison.
