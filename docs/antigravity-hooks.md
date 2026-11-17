# Antigravity Lifecycle Hooks (`agy` 1.5.9)

Empirical findings from probing Google Antigravity (`agy` 1.5.9) in isolated
scratch `HOME` directories and temporary git workspaces on macOS.

## 1. Mechanisms Tested

Each candidate hook configuration mechanism was tested independently in an
isolated scratch environment:

| Mechanism | Path | Fires in `agy`? | Notes |
| --- | --- | --- | --- |
| Project Gemini settings | `<repo>/.gemini/settings.json` | No | 0 events emitted. |
| Global Gemini CLI settings | `~/.gemini/settings.json` | No | 0 events emitted in `agy`; used by Gemini CLI (`BeforeAgent`, `AfterAgent`, `Notification`, etc.). |
| Antigravity CLI settings | `~/.gemini/antigravity-cli/settings.json` | No | Configures CLI preferences, not `"hooks"`. |
| Workspace Antigravity hooks | `<repo>/.agents/hooks.json` | **Yes** | Discovered walking up from CWD to the `.git` root. |
| Global Antigravity hooks | `~/.gemini/config/hooks.json` | **Yes** | Shared across `agy` CLI (interactive and `-p` print mode), Antigravity 2.0 App, and Antigravity IDE. |
| Antigravity plugin hooks | `~/.gemini/config/plugins/<name>/hooks.json` | **Yes** | Validated with `agy plugin validate --strict`; sets `PLUGIN_ROOT` and `PLUGIN_DATA` environment variables and runs with `cwd` set to the plugin directory. |

### Interactive Terminal OSC 7501 Support

In interactive TUI sessions (`agy` / `agy -i`), `agy` 1.5.9 also queries
`\x1b]7501;?\x07` at startup and emits native OSC 7501 reports (`app=antigravity`)
when wrapped with `sidepulse wrap agy`, including `state=working`,
`state=blocked:kind=auth`, `state=blocked:kind=permission` (with a base64
approval summary in `msg`), `state=done`, and `state=idle`. Because OSC 7501
requires launching `agy` inside an OSC 7501-capable terminal or `sidepulse wrap`
and does not cover `agy -p` print mode, Antigravity App, or Antigravity IDE,
SidePulse installs global command hooks in `~/.gemini/config/hooks.json`.

## 2. `hooks.json` Format and Coexistence

`~/.gemini/config/hooks.json` is a JSON object whose top-level keys are **named
hook bundles** (for example `"moshi-hook"` and `"sidepulse"`). Antigravity
merges enabled bundles and runs their handlers sequentially:

```json
{
  "moshi-hook": {
    "PreInvocation": [
      {
        "command": "'/opt/homebrew/opt/moshi-hook/bin/moshi-hook' antigravity-hook PreInvocation",
        "timeout": 5,
        "type": "command"
      }
    ],
    "Stop": [
      {
        "command": "'/opt/homebrew/opt/moshi-hook/bin/moshi-hook' antigravity-hook Stop",
        "timeout": 5,
        "type": "command"
      }
    ],
    "enabled": true
  },
  "sidepulse": {
    "enabled": true,
    "SessionStart": [
      {
        "type": "command",
        "command": "..."
      }
    ],
    "PreInvocation": [
      {
        "type": "command",
        "command": "..."
      }
    ],
    "PreToolUse": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "..."
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "..."
          }
        ]
      }
    ],
    "Stop": [
      {
        "type": "command",
        "command": "..."
      }
    ]
  }
}
```

* **Flat events** (`SessionStart`, `PreInvocation`, `PostInvocation`, `Stop`)
  take a list of handler objects directly (`{"type": "command", "command": "..."}`).
* **Tool events** (`PreToolUse`, `PostToolUse`) wrap handler lists in matcher
  groups (`{"matcher": "*", "hooks": [...]}`).

## 3. Input/Output Contract and `PreToolUse` Security Requirement

Hook commands receive a single camelCase JSON object on `stdin` and **must**
write a JSON object to `stdout`:

* **`PreToolUse` stdout (`{"decision": "ask"}`):** `PreToolUse` requires a
  `"decision"` string (`"allow"`, `"deny"`, `"ask"`, or `"force_ask"`).
  Empirically, printing `{}` (or empty stdout) causes Antigravity to **deny**
  every tool call with `tool call denied by pre-tool hook`. Printing
  `{"decision": "ask"}` defers to Antigravity's own permission policy and
  `"Always Allow"` cache: operations already permitted by policy (such as
  `read_file` inside the workspace) run automatically without prompting, while
  restricted operations (such as `write_file` or `read_file` outside the
  workspace) still prompt the user normally and are never auto-approved.
* **All other events (`{}`):** `SessionStart`, `PreInvocation`,
  `PostInvocation`, `PostToolUse`, and `Stop` accept `{}` on `stdout` (on
  `Stop`, only `"decision": "continue"` blocks stopping; `{}` allows normal
  termination).

## 4. Events That Fired and Payload Fields

`agy plugin validate --strict` and runtime probes confirm exactly six supported
hook event names: `SessionStart`, `PreInvocation`, `PostInvocation`,
`PreToolUse`, `PostToolUse`, and `Stop`.

### Common Payload Fields (`stdin`)

Every event payload includes:

```json
{
  "conversationId": "00000000-0000-4000-8000-000000000001",
  "cwd": "/work/project",
  "workspacePaths": ["/work/project"],
  "transcriptPath": "/Users/example/.gemini/antigravity-cli/brain/00000000-0000-4000-8000-000000000001/.system_generated/logs/transcript_full.jsonl",
  "artifactDirectoryPath": "/Users/example/.gemini/antigravity-cli/brain/00000000-0000-4000-8000-000000000001",
  "modelName": "gemini-3.8-flash-low",
  "lastUserInput": "Run the unit tests",
  "hookEventName": "PreInvocation"
}
```

* `transcriptPath` and `artifactDirectoryPath` identify the Antigravity host
  surface: `/.gemini/antigravity-cli/` (`agy` CLI), `/.gemini/antigravity/`
  (Antigravity App), or `/.gemini/antigravity-ide/` (Antigravity IDE).
* In interactive TUI mode (`agy` / `agy -i`), `cwd` and `workspacePaths` in the
  JSON payload can be `""` and `[]` when no explicit workspace has been added,
  while `agy` spawns global `hooks.json` commands with their process working
  directory set to the directory where `agy` was launched.
* Antigravity-specific environment variable names passed to hook processes:
  `ANTIGRAVITY_AGENT`, `ANTIGRAVITY_AGENTAPI_EXE`, `ANTIGRAVITY_APP_DATA_DIR`,
  `ANTIGRAVITY_CONVERSATION_ID`, `ANTIGRAVITY_CSRF_TOKEN`,
  `ANTIGRAVITY_LS_ADDRESS`, `ANTIGRAVITY_LS_VERSION`, `ANTIGRAVITY_PROJECT_ID`,
  `ANTIGRAVITY_SOURCE_METADATA`, and `ANTIGRAVITY_TRAJECTORY_ID` (plus
  `PLUGIN_ROOT` and `PLUGIN_DATA` when invoked from a plugin).

### Per-Event Fields and Timing

1. **`SessionStart`**
   * Fires once when a new conversation starts.
   * Payload: common fields only.
2. **`PreInvocation`**
   * Fires before each model call in the execution loop.
   * Extra fields: `invocationNum` (`0` on initial prompt submission, `1+` on
     follow-up steps after tool calls), `initialNumSteps`.
3. **`PreToolUse`**
   * Fires before a tool step runs (and **before** any interactive permission
     prompt is answered).
   * Extra fields: `stepIdx` (integer), `toolCall` (`{"id": "...", "name": "...", "args": {...}}`).
   * When the model asks a structured user question, `toolCall.name` is
     `"ask_question"` and `toolCall.args.questions` contains
     `[{"question": "...", "options": [...], "is_multi_select": false}]`.
4. **`PostToolUse`**
   * Fires after a tool step completes.
   * Extra fields: `stepIdx`, `toolCall`, `error` (`""` when the tool succeeded,
     or an error string such as `"file does not exist"` when tool execution
     failed; shell commands that exit non-zero return their exit status in tool
     output with `error: ""`).
5. **`PostInvocation`**
   * Fires after a single model/tool step finishes, immediately before either
     the next `PreInvocation` or `Stop`.
   * Extra fields: `invocationNum`, `initialNumSteps`. SidePulse omits
     `PostInvocation` during installation because `PreInvocation`,
     `PreToolUse`, `PostToolUse`, and `Stop` already cover every state change.
6. **`Stop`**
   * Fires when the turn's execution loop terminates.
   * Extra fields:
     * `executionNum` (integer)
     * `continuationActive` (boolean)
     * `fullyIdle` (boolean; `true` when background tasks are also idle)
     * `error` (string; `""` on normal completion)
     * `terminationReason` (string):
       * `"NO_TOOL_CALL"`: normal turn completion
       * `"USER_CANCELED"`: turn canceled by the user
       * `"ERROR"`, `"TERMINAL_CUSTOM_HOOK"`, `"MAX_INVOCATIONS"`,
         `"MAX_FORCED_INVOCATIONS"`, `"MAX_TOKEN_BUDGET_EXCEEDED"`: abnormal or
         errored termination
       * `"TERMINAL_STEP_TYPE"`, `"INJECTED_RESPONSE"`: terminal step completion

## 5. What Did Not Fire

* **Gemini CLI and Claude event names:** `BeforeAgent`, `AfterAgent`,
  `BeforeModel`, `AfterModel`, `BeforeToolSelection`, `BeforeTool`, `AfterTool`,
  `PreCompress`, `Notification`, `SessionEnd`, `UserPromptSubmit`,
  `PermissionRequest`, `PermissionDenied`, `PostToolUseFailure`, `PreCompact`,
  `PostCompact`, `SubagentStart`, `SubagentStop`, `StopFailure`, and `Interrupt`
  do not fire in `hooks.json` and produce unknown-event warnings under
  `agy plugin validate --strict`.
* **Interactive permission wait:** While `agy` waits at a TUI tool-approval
  prompt, no additional hook fires between `PreToolUse` and `PostToolUse`
  (interactive TUI sessions emit `state=blocked:kind=permission` on OSC 7501).
* **Hard CLI abort / Escape denial:** Pressing `Esc` on a TUI permission prompt
  or sending `SIGINT` in `-p` print mode aborts the turn immediately before
  `PostToolUse` or `Stop` runs, and CLI argument validation failures (such as an
  invalid `--model` flag) exit before any session or hook is started.
