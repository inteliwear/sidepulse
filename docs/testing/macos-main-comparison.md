# Current working tree versus the official main install

Initial comparison performed on 2026-10-26 in UTM, macOS 27.2 arm64.
The 2026-10-27 Codex fix and its new evidence are recorded below; the original
comparison results remain unchanged.

The repository uses `main`, not `master`. The official installation command is:

```sh
curl -fsSL https://sidepulse.io/setup.sh | bash
```

The website script downloads `main/scripts/setup.sh`, which builds and installs
from `https://github.com/inteliwear/sidepulse.git`. The actual installer log
resolved commit `ffb1d3c2f3eed3b7e80e32d7876a7e1d67bd5dfb`; the installed version
is `0.2.4.dev6+gffb1d3c2f`. No local package override was passed to this install.
The installed wheel's provenance and hash are preserved in `direct_url.json`.

The development side is `codex/osc7501-integration` at
`4735acb1f3770400ab5da96f4aef2817d9dbc2f1`, **including uncommitted product
changes**. Its test version is `0+vmtest`. At the time of that comparison, every
`src/` file matched the branch-run manifest byte for byte. That evidence
describes the tree before the subsequent Codex fix.

## Test method

The official install ran in a new stopped clone of **SidePulse Agents Ready**,
named **SidePulse Official Main Comparison**
(`989B2B54-CD63-4F4B-B03C-2BE25984A38B`). This is an update/install comparison
using an already authenticated fixture, **not a clean-OS installation pass**.
The OS, developer tools, agent versions, sign-ins, and screen recording
permission came from the same fixture as the branch test. Saved baselines were
not modified. The host device writer was stopped during guest testing.

Both versions ran real Claude Code 2.1.311 and Codex CLI 0.164.0 with the same
read-only fixture task and the same red/blue interactive-question scenario.
Screenshot labels were assigned from images before comparing recorded states.
The branch observer queries the running daemon. Main predates that API, so its
observer reads the live monitor's persisted `latest.json` directly; it does not
reconstruct detection from hooks. These observation sources are named in each
frame's evidence. Old sessions cannot satisfy a new session's assertions.

The initial official run passed both live tasks and both hook replays, but the
cloned guest had lost its `/Volumes/SidePulse` alias. Restoring that alias fixed
the mount issue. The next discovery check exposed a harness incompatibility:
main has no `sidepulse list --json`. The observer now uses the installed
`discover_devices()` API supported by both versions. The subsequent seven-check
installation/replay run passed completely. Original failures remain preserved.

## Results

| Check | Current working tree | Official main |
| --- | --- | --- |
| Setup/service/menu bar/live state | Passed | Passed |
| Shared filesystem discovery and permission | Passed | Passed after restoring guest alias |
| Claude and Codex hook replays | Passed | Passed |
| Real Claude and Codex fixture tasks | Both passed | Both passed |
| Claude sampled visual comparison | Passed, 8 reviewed frames | Passed, 6 reviewed frames |
| Codex sampled visual comparison | Failed on question wait | Failed on the same question wait, 2 of 6 reviewed frames |

Physical LED appearance, uninterrupted file delivery, native USB, raw READ2ME,
and eject behavior are not established by this shared-filesystem comparison.
The branch's earlier LED-file reads were intermittent and are retained as such.

The official-main Codex recording `20261026-233100-visual-codex-c5d81e`
contains 157 frames. Reviewed frames 90 and 105 show a pending question while
the live monitor reports `tool_running`. Working, tool running, and completed
frames match. Claude recording `20261026-232201-visual-claude-fae345` contains
157 frames; all six reviewed frames match across the four tested states.
The failure is preserved, not converted into a passing expected-failure result.

The comparison VM is stopped, and the host SidePulse service was restored.

## Codex fix verified on 2026-10-27

The branch now recognizes Codex's blocking and asynchronous question tools.
An asynchronous `{"accepted":true}` response acknowledges queuing a question;
it does not mean the user answered it. Subsequent background tools preserve
the question wait in the same session. User input, completion, cancellation,
session lifecycle, errors, and explicit status overrides can release it.
The producer uses a local `unless_kind: "question"` guard, checked atomically
by the daemon; forwarded messages contain the resulting status without the
guard. The legacy collector implements the same transition rule.

The fixed tree was installed through `scripts/setup.sh` with the local package
override in the existing comparison VM. This was **not** a new official-main
install or a clean-OS pass. All 96 product files match the guest's preserved
source manifest. The seven installation/replay checks passed.

Real Codex recording `20261027-084314-visual-codex-17d32d` contains 247 frames
and 18 original hook events. Codex asked red/blue, queued the question, then
ran four `clocksleep` calls while awaiting an answer. SidePulse preserved
`waiting_for_input`. After the real answer `red`, Codex reported completion
and SidePulse reported `completed`. All **16 independently labeled sampled
frames passed**, covering working, tool running, question waiting, and
completion; 231 other frames are not independently scored. Screenshot hashes
were verified. This does not establish physical LED or native USB behavior.

The relevant local suite passed **386 tests**, with 9 skipped and 59 subtests
passing. The Codex regression file passed 19 tests after final test assertions
were added. It replays the original failure through both the legacy collector
and the program-status sender/forwarded receiver, with additional release,
error, isolation, and explicit-override cases.

Evidence root:
`build/macos-vm/codex-fix/sidepulse-vm-results/codex-fix-20261027/`.
The original before-fix branch and official-main failures remain preserved.
The comparison VM was shut down and the host service restored after testing.
At the time of this run, changes were uncommitted on `codex/osc7501-integration`.

## Structured dialog follow-up, 2026-10-27

The async-question pass above does not cover every native Codex dialog. A
separate recording of the unchanged fixed product,
`20261027-085321-visual-codex-37a351`, exercises `/plan`, a blocking
`request_user_input` with two questions, and the subsequent implementation
confirmation. It contains 124 frames; nine independently labeled samples
were reviewed, with **two failures**:

| Visible screen | Observed SidePulse state | Result |
| --- | --- | --- |
| Color selector, first question | `waiting_for_input` | Passed, frames 45 and 55 |
| Report-style selector, second question still unanswered | `waiting_for_input` | Passed, frame 65 |
| Both dialog answers submitted; agent resumes | `working` | Passed, frame 75 |
| “Implement this plan?” selector | `completed` | **Failed**, frames 85 and 95 |
| Implementation finished after accepting the plan | `completed` | Passed, frame 115 |

The blocking tool's `PostToolUse` contains both selected answers and correctly
releases its question wait. The plan confirmation is different: the saved
hook stream contains no question or permission event for that selector.
Instead, `Stop` has `last_assistant_message: null` and
`permission_mode: "default"` even though the UI is in Plan mode. The synthetic
session transcript separately retains a Plan-mode turn context and an
assistant `<proposed_plan>` response, followed by `task_complete` with a null
message. SidePulse at the time treated that Stop as completed. A null Stop alone
is insufficient evidence to classify every such event as a question.

This was an **unfixed finding** at the time, distinct from the repaired async-question
state. No product code was changed during this follow-up. A future fix must
also account for accepting, dismissing, or staying in Plan mode, rather than
just changing every empty Stop into a permanent wait.

To reproduce, start a real Codex visual recording, let the fixture task finish,
enter `/plan`, then ask:

> Use the blocking request_user_input tool to ask exactly two questions in one
> structured dialog: first choose Red or Blue for the test color, then Compact
> or Detailed for report style. Wait for my choices. After receiving both, run
> sleep 5 using your shell tool and then report the chosen values and say the
> test is complete. Do not modify files.

Pause on each question, select Red and Detailed, then pause on “Implement this
plan?” before accepting it. Preserve and independently label that confirmation
as `waiting_for_input`; do not label it completed to obtain a pass.

Evidence root:
`build/macos-vm/dialog-check/sidepulse-vm-results/dialog-check-20261027/`.
The test VM was shut down and the host service restored after testing.

## Plan-confirmation fix verification, 2026-11-05

The local `codex/osc7501-integration` tree based on `031c581` now recovers a
pending plan from a bounded, regular local rollout file when a root Codex Stop
arrives. It requires the matching session filename, current turn, Plan-mode
context, and a nonempty final `<proposed_plan>` block. Ordinary answers, quoted
examples, aborted turns, subagents, and missing or unsupported evidence retain
their normal classification. At most 2 MiB is read, and transcript contents
are not forwarded to receivers.

A separate clone of **SidePulse Agents Ready**, named **SidePulse Plan
Confirmation 2026-11-05**, ran the fixed package with real signed-in Codex CLI
**0.164.0** on macOS 27.2. This is subsequent evidence, not part of the earlier
fresh-install/main-upgrade matrix. Seven installation/replay checks passed.
The real recording contains 419 screenshots, intact screenshot hashes, no
recording error, and an agent exit code of zero. Seventeen samples were labeled
from the terminal screenshots before reading the detected modes:

| Scenario | Observed result |
| --- | --- |
| Three native “Implement this plan?” menus | `waiting_for_input`, matching all sampled menus |
| “No, stay in Plan mode” and Escape | Pending plan remains `waiting_for_input` |
| New instruction followed by an ordinary `ready` answer | Returns to `completed` in both cases |
| “Yes, implement this plan” | Starts execution; real `sleep 3` runs; final answer is `completed` |

The strict visual review is **16/17 matching, failed overall**: frame 400 was
independently labeled `working` from the Working footer, while SidePulse
reported `tool_running` during the background shell command. This separate
phase mismatch is preserved, not relabeled to obtain a green report. All eight
sampled pending-plan states and all seven sampled completions matched.

Codex emits no hook when Escape or “No” merely dismisses the menu. The indicator
therefore represents the pending plan awaiting the next instruction, rather
than exact popup visibility. A new prompt or implementation starts work, and
an ordinary final answer completes it. The implementation does not poll the
screen or change Codex. Fresh-context acceptance and newer Codex versions were
not visually retested. Physical LED appearance and native USB were not tested.

Evidence: `build/macos-vm/plan-fix-20261105/`, including the frozen payload and
manifest, predefined `scenarios.json`, and collected recording
`guest/sidepulse-vm-results/plan-fix-20261105/20261105-104312-visual-codex-e2a0ec/`.
The original failed October recording remains unchanged. Regression validation
passed **384 tests and 59 subtests**, including **24 plan tests**. The VM is
stopped; the host service and tray are running again. Changes are local and
uncommitted.

## Relevant code differences before the fix

- **Status delivery:** main sends raw provider hook events to the menu bar and
  relay service. This branch translates hooks into versioned program-status
  messages and gives a daemon ownership of status, ordering, history, output,
  and forwarding. New OSC 7501 and transport modules implement that path.
- **UI and output ownership:** main's menu bar/service each contain monitoring
  and output logic. The branch moves that responsibility into the daemon and
  changes the menu bar into its client; it also moves sleep/output policy there.
- **Installer and CLI:** the branch adds an early Command Line Tools error with
  installation instructions, introduces the `sp` alias, and removes the old
  `agent-monitor`/`agent-status-bar` entry points. Installed hook commands omit
  the legacy `agent-monitor` subcommand.
- **Devices:** the branch adds device event monitoring (`watchdog`), expands
  USB/storage control and identity discovery, and retains libusb contexts to
  avoid a macOS shutdown/discovery hang. Those native USB differences require
  separate hardware testing.
- **Provider interpretation:** the shared collector differs only in defensive
  handling of remote project paths. The branch additionally normalizes
  `toolUseId` to `tool_use_id`. The actual official-main recording reproduces the same asynchronous Codex
  question bug, so this tested failure is pre-existing, not introduced by the
  branch's transport changes.

## Evidence

Local artifact roots:

- Branch live report: `build/macos-vm/passing-live-guest/sidepulse-vm-results/20261026-220514-d5bf61/`.
- Branch screens and reviewed labels: `build/macos-vm/visual-evidence/sidepulse-vm-results/`.
- Official install, package provenance, checks, and screen recordings:
  `build/macos-vm/main-comparison/sidepulse-vm-results/`.

Each visual run contains `review.html`, `review-labels.json`,
`visual-result.json`, original screenshots, terminal output, hook payloads,
and timestamped observed state. Capture alone is never a visual pass.
