// SidePulse status for OpenCode.
// Managed by SidePulse: `sidepulse setup` replaces this file and
// `sidepulse setup opencode --uninstall` removes it.
//
// Installed into OpenCode's global plugins folder. It
// maps OpenCode session events to SidePulse program-status reports and submits
// each one with `sidepulse report --json`. Reports carry the session state,
// title and directory, plus a short permission or question message. Prompts,
// tool input and tool output are never sent.
//
// The file has no imports outside Node's standard library, so it loads from
// the plugins folder without installing packages. The default export is its
// only export: OpenCode 1 would call any other exported function as a plugin.

import { spawn } from "node:child_process"
import { existsSync } from "node:fs"
import { homedir } from "node:os"
import { join } from "node:path"

const EVENT_TYPES = [
  "session.execution.started",
  "session.execution.succeeded",
  "session.execution.failed",
  "session.execution.interrupted",
  "permission.asked",
  "permission.replied",
  "form.created",
  "form.replied",
  "form.cancelled",
  "session.renamed",
  "session.deleted",
]

const MAX_MESSAGE_CHARS = 240
const REPORT_TIMEOUT_MS = 5000

type State = "idle" | "working" | "blocked" | "done" | "error" | "clear"

type Waiting = { kind: "permission" | "question"; msg?: string }

type Tracked = {
  state: State
  waiting: Map<string, Waiting>
  title?: string
  cwd?: string
}

type SessionInfo = {
  id: string
  parentID?: string
  title?: string
  location?: { directory?: string }
}

type Event = { id: string; type: string; data: any }

type Context = {
  options?: Record<string, unknown>
  session: { get(input: { sessionID: string }): Promise<SessionInfo> }
  event: {
    subscribe(options: { types: string[]; signal: AbortSignal }): AsyncIterable<Event>
  }
}

function sidepulseCommand(option: unknown): string {
  if (typeof option === "string" && option) return option
  if (process.env.SIDEPULSE_BIN) return process.env.SIDEPULSE_BIN
  // OpenCode's background service may not have ~/.local/bin on its PATH.
  const installed = join(homedir(), ".local", "bin", "sidepulse")
  return existsSync(installed) ? installed : "sidepulse"
}

function clip(text: string | undefined): string | undefined {
  const value = text?.replace(/[\u0000-\u001f\u007f-\u009f]+/g, " ").trim()
  if (!value) return undefined
  return value.length > MAX_MESSAGE_CHARS ? value.slice(0, MAX_MESSAGE_CHARS - 1) + "…" : value
}

function permissionMessage(data: any): string | undefined {
  if (typeof data.message === "string" && data.message.trim()) return clip(data.message)
  const resources = Array.isArray(data.resources) ? data.resources.filter((r: unknown) => typeof r === "string") : []
  return clip([data.action, resources.join(", ")].filter(Boolean).join(": "))
}

function createSidePulse(ctx: Context, submit: (report: object) => Promise<void>) {
  const tracked = new Map<string, Tracked>()
  const sessions = new Map<string, Promise<SessionInfo | undefined>>()

  const lookup = (sessionID: string) => {
    let info = sessions.get(sessionID)
    if (!info) {
      info = ctx.session.get({ sessionID }).catch(() => undefined)
      sessions.set(sessionID, info)
    }
    return info
  }

  // Subagent sessions report through their top-level session, so one OpenCode
  // conversation is one SidePulse session.
  const rootOf = async (sessionID: string): Promise<SessionInfo | undefined> => {
    let info = await lookup(sessionID)
    for (let depth = 0; info?.parentID && depth < 16; depth++) info = await lookup(info.parentID)
    return info
  }

  const session = (root: SessionInfo) => {
    let entry = tracked.get(root.id)
    if (!entry) {
      entry = { state: "idle", waiting: new Map() }
      tracked.set(root.id, entry)
    }
    entry.title = clip(root.title) ?? entry.title
    entry.cwd = root.location?.directory ?? entry.cwd
    return entry
  }

  const send = (event: Event, rootID: string, entry: Tracked) => {
    const status: Record<string, unknown> = { state: entry.state, app: "opencode" }
    const waiting = [...entry.waiting.values()].at(-1)
    if (entry.state === "blocked" && waiting) {
      status.kind = waiting.kind
      if (waiting.msg) status.msg = waiting.msg
    }
    if (entry.title && entry.state !== "clear") status.title = entry.title
    const context: Record<string, string> = { provider: "opencode", session_id: rootID, event_name: event.type }
    if (entry.cwd) context.cwd = entry.cwd
    if (entry.title) context.title = entry.title
    return submit({
      v: 1,
      type: "program_status",
      scope: `opencode-${rootID}`,
      request_id: `opencode-${event.id}`,
      status,
      context,
    })
  }

  // The state that remains after a pending question or permission resolves.
  const settle = (entry: Tracked) => {
    entry.state = entry.waiting.size ? "blocked" : "working"
  }

  return async function handle(event: Event): Promise<void> {
    const data = event.data ?? {}
    const sessionID: string | undefined = data.sessionID ?? data.form?.sessionID
    if (!sessionID) return

    if (event.type === "session.deleted") {
      sessions.delete(sessionID)
      const entry = tracked.get(sessionID)
      if (!entry) return
      tracked.delete(sessionID)
      entry.state = "clear"
      entry.waiting.clear()
      return send(event, sessionID, entry)
    }

    const root = await rootOf(sessionID)
    if (!root) return
    const isRoot = root.id === sessionID

    if (event.type === "session.renamed") {
      if (!isRoot) return
      sessions.set(sessionID, Promise.resolve({ ...root, title: data.title }))
      const entry = tracked.get(sessionID)
      if (!entry) return
      entry.title = clip(data.title)
      return send(event, root.id, entry)
    }

    const replies = ["permission.replied", "form.replied", "form.cancelled"]
    if (replies.includes(event.type) && !tracked.has(root.id)) return
    const entry = session(root)
    switch (event.type) {
      case "session.execution.started":
        if (!isRoot) return
        entry.waiting.clear()
        entry.state = "working"
        break
      case "session.execution.succeeded":
      case "session.execution.failed":
      case "session.execution.interrupted":
        if (!isRoot) return
        entry.waiting.clear()
        entry.state =
          event.type === "session.execution.succeeded"
            ? "done"
            : event.type === "session.execution.failed"
              ? "error"
              : data.reason === "shutdown"
                ? "clear"
                : "idle"
        break
      case "permission.asked":
        entry.waiting.set(data.id, { kind: "permission", msg: permissionMessage(data) })
        entry.state = "blocked"
        break
      case "form.created":
        entry.waiting.set(data.form.id, { kind: "question", msg: clip(data.form.title) })
        entry.state = "blocked"
        break
      case "permission.replied":
        if (!entry.waiting.delete(data.requestID)) return
        settle(entry)
        break
      case "form.replied":
      case "form.cancelled":
        if (!entry.waiting.delete(data.id)) return
        settle(entry)
        break
      default:
        return
    }
    return send(event, root.id, entry)
  }
}

function reportWith(command: string) {
  let warned = false
  return (report: object) =>
    new Promise<void>((resolve) => {
      // `opencode run` exits as soon as the session finishes, which would kill
      // a normal child before the final "done" report lands. A detached child
      // in its own process group outlives OpenCode.
      const child = spawn(command, ["report", "--json"], {
        detached: true,
        stdio: ["pipe", "ignore", "ignore"],
        windowsHide: true,
      })
      child.stdin?.on("error", () => {})
      child.stdin?.end(JSON.stringify(report))
      const timer = setTimeout(() => child.kill(), REPORT_TIMEOUT_MS)
      const finish = (failure?: string) => {
        clearTimeout(timer)
        // SidePulse is optional: a missing or stopped daemon must never affect
        // OpenCode, so failures are logged once and otherwise ignored.
        if (failure && !warned) {
          warned = true
          console.warn(`[sidepulse] status report failed (${failure}); is \`sidepulse service start\` running?`)
        }
        resolve()
      }
      child.once("error", (error) => finish(error.message))
      child.once("exit", (code, signal) => finish(code === 0 ? undefined : signal ?? `exit ${code}`))
      child.unref()
    })
}

// OpenCode loads a plugin once per open project, and every copy receives
// every event. The first live copy in this server reports; the others stand
// by and take over if it unloads. Session state is shared, so a takeover
// keeps track of open sessions.
type Copy = { ctx: Context; submit: (report: object) => Promise<void> }
type Shared = { copies: Copy[]; handle?: (event: Event) => Promise<void>; queue: Promise<void> }
const shared: Shared = ((globalThis as any)[Symbol.for("sidepulse.opencode")] ??= { copies: [], queue: Promise.resolve() })

export default {
  id: "sidepulse",
  setup(ctx: Context) {
    const self: Copy = { ctx, submit: reportWith(sidepulseCommand(ctx.options?.command)) }
    shared.copies.push(self)
    shared.handle ??= createSidePulse(
      { session: { get: (input) => shared.copies[0].ctx.session.get(input) } } as Context,
      async (report) => {
        // Reports go out one at a time so the daemon sees them in event order.
        const leader = shared.copies[0]
        shared.queue = shared.queue.then(() => leader?.submit(report))
      },
    )
    const controller = new AbortController()
    void (async () => {
      for await (const event of ctx.event.subscribe({ types: EVENT_TYPES, signal: controller.signal })) {
        if (shared.copies[0] !== self) continue
        await shared.handle!(event).catch((error) => console.warn(`[sidepulse] ${error}`))
      }
    })().catch(() => {})
    return () => {
      controller.abort()
      shared.copies.splice(shared.copies.indexOf(self), 1)
      if (!shared.copies.length) shared.handle = undefined
    }
  },
}
