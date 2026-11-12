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

import { spawn, spawnSync, type ChildProcess } from "node:child_process"
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

// A pending permission or question, remembered with the session that asked.
type Waiting = { kind: "permission" | "question"; sessionID: string; msg?: string }

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

function createSidePulse(ctx: Context, submit: (report: object) => void) {
  const tracked = new Map<string, Tracked>()
  const sessions = new Map<string, Promise<SessionInfo | undefined>>()
  const roots = new Map<string, string>()

  const lookup = (sessionID: string) => {
    let info = sessions.get(sessionID)
    if (!info) {
      info = ctx.session.get({ sessionID }).catch(() => {
        // Forget a failed lookup so a later event can retry it.
        sessions.delete(sessionID)
        return undefined
      })
      sessions.set(sessionID, info)
    }
    return info
  }

  // Subagent sessions report through their top-level session, so one OpenCode
  // conversation is one SidePulse session.
  const rootOf = async (sessionID: string): Promise<SessionInfo | undefined> => {
    let info = await lookup(sessionID)
    for (let depth = 0; info?.parentID && depth < 16; depth++) info = await lookup(info.parentID)
    if (info) roots.set(sessionID, info.id)
    return info
  }

  const forget = (rootID: string) => {
    tracked.delete(rootID)
    for (const [sessionID, root] of roots) {
      if (root !== rootID) continue
      roots.delete(sessionID)
      sessions.delete(sessionID)
    }
    sessions.delete(rootID)
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
    if (entry.title) {
      context.title = entry.title
      // OpenCode titles belong to the session, so SidePulse shows renames.
      context.title_source = "session"
    }
    submit({
      v: 1,
      type: "program_status",
      scope: `opencode-${rootID}`,
      request_id: `opencode-${event.id}`,
      status,
      context,
    })
  }

  // Drops the prompts a finished or deleted session left open. Returns whether
  // the root session's state changed.
  const dropWaitingFrom = (entry: Tracked, sessionID: string) => {
    const before = entry.waiting.size
    for (const [id, waiting] of entry.waiting) if (waiting.sessionID === sessionID) entry.waiting.delete(id)
    if (entry.waiting.size === before) return false
    if (entry.state === "blocked") settle(entry)
    return true
  }

  // The state that remains after a pending question or permission resolves.
  const settle = (entry: Tracked) => {
    entry.state = entry.waiting.size ? "blocked" : "working"
  }

  const finalState = (event: Event): State | undefined => {
    if (event.type === "session.execution.succeeded") return "done"
    if (event.type === "session.execution.failed") return "error"
    // A superseded execution is replaced by one that reports its own state.
    if (event.data.reason === "superseded") return undefined
    return event.data.reason === "shutdown" ? "clear" : "idle"
  }

  return async function handle(event: Event): Promise<void> {
    const data = event.data ?? {}
    const sessionID: string | undefined = data.sessionID ?? data.form?.sessionID
    if (!sessionID) return

    if (event.type === "session.deleted") {
      const entry = tracked.get(sessionID)
      if (entry) {
        forget(sessionID)
        entry.state = "clear"
        entry.waiting.clear()
        return send(event, sessionID, entry)
      }
      // A deleted subagent can never answer the prompts it left open.
      const rootID = roots.get(sessionID)
      roots.delete(sessionID)
      sessions.delete(sessionID)
      const root = rootID ? tracked.get(rootID) : undefined
      if (rootID && root && dropWaitingFrom(root, sessionID)) send(event, rootID, root)
      return
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
      case "session.execution.interrupted": {
        if (!isRoot) {
          if (!dropWaitingFrom(entry, sessionID)) return
          break
        }
        const state = finalState(event)
        if (!state) return
        entry.waiting.clear()
        entry.state = state
        break
      }
      case "permission.asked":
        entry.waiting.set(data.id, { kind: "permission", sessionID, msg: permissionMessage(data) })
        entry.state = "blocked"
        break
      case "form.created":
        entry.waiting.set(data.form.id, { kind: "question", sessionID, msg: clip(data.form.title) })
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

// Sends reports one at a time, in event order, so the daemon sees them in the
// order they happened.
class Reporter {
  private pending: string[] = []
  private running?: ChildProcess
  private runningBody?: string
  private warned = false
  private idleWaiters: (() => void)[] = []
  private command: string

  constructor(command: string) {
    this.command = command
    // `opencode run` exits as soon as its session finishes. Reports that are
    // still queued then would be lost, so they are sent before exiting.
    process.once("exit", () => this.flushSync())
  }

  push(report: object) {
    this.pending.push(JSON.stringify(report))
    this.pump()
  }

  // Resolves once every queued report has been sent.
  idle(): Promise<void> {
    if (!this.running && !this.pending.length) return Promise.resolve()
    return new Promise((resolve) => this.idleWaiters.push(resolve))
  }

  private pump() {
    if (this.running) return
    const body = this.pending.shift()
    if (body === undefined) {
      this.idleWaiters.splice(0).forEach((resolve) => resolve())
      return
    }
    // A detached child in its own process group outlives OpenCode.
    const child = spawn(this.command, ["report", "--json"], {
      detached: true,
      stdio: ["pipe", "ignore", "ignore"],
      windowsHide: true,
    })
    this.running = child
    this.runningBody = body
    child.stdin?.on("error", () => {})
    child.stdin?.end(body)
    const timer = setTimeout(() => child.kill(), REPORT_TIMEOUT_MS)
    let done = false
    const finish = (failure?: string) => {
      if (done) return
      done = true
      clearTimeout(timer)
      this.fail(failure)
      this.running = undefined
      this.runningBody = undefined
      this.pump()
    }
    child.once("error", (error) => finish(error.message))
    child.once("exit", (code, signal) => finish(code === 0 ? undefined : signal ?? `exit ${code}`))
    child.unref()
  }

  private flushSync() {
    if (!this.pending.length) return
    // The report already on its way is sent again first, so the queued ones
    // cannot overtake it. The daemon ignores a repeated request_id, so the
    // detached copy changes nothing if it lands later.
    const bodies = [...(this.runningBody ? [this.runningBody] : []), ...this.pending.splice(0)]
    for (const body of bodies) {
      const result = spawnSync(this.command, ["report", "--json"], {
        input: body,
        stdio: ["pipe", "ignore", "ignore"],
        timeout: REPORT_TIMEOUT_MS,
        windowsHide: true,
      })
      this.fail(result.error?.message ?? (result.status === 0 ? undefined : `exit ${result.status}`))
    }
  }

  private fail(failure: string | undefined) {
    // SidePulse is optional: a missing or stopped daemon must never affect
    // OpenCode, so failures are logged once and otherwise ignored.
    if (!failure || this.warned) return
    this.warned = true
    console.warn(`[sidepulse] status report failed (${failure}); is \`sidepulse service start\` running?`)
  }
}

// OpenCode loads a plugin once per open project, and every copy receives
// every event, so copies share one state and report each event once. When the
// plugin file changes, OpenCode loads the new module before it unloads the old
// copies: the newest module takes over and older copies go quiet.
const MAX_SEEN_EVENTS = 1000
const MODULE = {}
type Shared = {
  module: object
  contexts: Context[]
  handle: (event: Event) => Promise<void>
  reporter: Reporter
  seen: Set<string>
  queue: Promise<void>
}
const SHARED = Symbol.for("sidepulse.opencode.state")
const global = globalThis as { [SHARED]?: Shared }

function sharedState(ctx: Context): Shared {
  const current = global[SHARED]
  if (current?.module === MODULE) return current
  const contexts: Context[] = []
  const reporter = new Reporter(sidepulseCommand(ctx.options?.command))
  const lookup = { session: { get: (input: { sessionID: string }) => contexts[0].session.get(input) } }
  return (global[SHARED] = {
    module: MODULE,
    contexts,
    handle: createSidePulse(lookup as Context, (report) => reporter.push(report)),
    reporter,
    seen: new Set(),
    queue: Promise.resolve(),
  })
}

export default {
  id: "sidepulse",
  setup(ctx: Context) {
    const shared = sharedState(ctx)
    shared.contexts.push(ctx)
    const controller = new AbortController()
    void (async () => {
      for await (const event of ctx.event.subscribe({ types: EVENT_TYPES, signal: controller.signal })) {
        if (global[SHARED] !== shared || shared.seen.has(event.id)) continue
        shared.seen.add(event.id)
        if (shared.seen.size > MAX_SEEN_EVENTS) shared.seen.delete(shared.seen.values().next().value!)
        // One queue for all copies keeps events in the order they arrived.
        shared.queue = shared.queue.then(() => shared.handle(event)).catch((error) => console.warn(`[sidepulse] ${error}`))
      }
    })().catch(() => {})
    return () => {
      controller.abort()
      shared.contexts.splice(shared.contexts.indexOf(ctx), 1)
    }
  },
}
