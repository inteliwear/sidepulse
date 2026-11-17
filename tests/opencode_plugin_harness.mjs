// Loads the SidePulse OpenCode plugin the way OpenCode does, with a fake
// context: `sessions` answer ctx.session.get and `events` are delivered
// through ctx.event.subscribe. `copies` sets how many copies of the plugin
// load at once, like OpenCode does for each open project. With `reload`, a
// second instance of the module loads too, and copies of both receive events,
// as during a hot reload. Reports reach the `sidepulse` command given as the
// second argument. With `exitEarly`, the process exits as soon as events are
// delivered. Used by tests/test_opencode_provider.py.
import { existsSync, readFileSync } from "node:fs"
import { pathToFileURL } from "node:url"

const [pluginPath, command] = process.argv.slice(2)
const { sessions, events, copies = 1, exitEarly = false, reload = false, reloadAfter, firstStarted } = JSON.parse(readFileSync(0, "utf8"))
const url = pathToFileURL(pluginPath).href

let delivered = 0
const context = (input) => ({
  options: { command },
  session: {
    async get({ sessionID }) {
      const session = sessions[sessionID]
      if (!session) throw new Error(`unknown session ${sessionID}`)
      // `failures` makes the first lookups fail, like a transient API error.
      if (session.failures > 0) {
        session.failures--
        throw new Error("lookup failed")
      }
      return { id: sessionID, ...session }
    },
  },
  event: {
    async *subscribe({ types }) {
      await new Promise((resolve) => setTimeout(resolve, 10))
      try {
        for (const event of input) if (types.includes(event.type)) yield event
      } finally {
        delivered++
      }
    },
  },
})

const setup = ({ default: plugin }, input) => Array.from({ length: copies }, () => plugin.setup(context(input)))
const cleanups = setup(await import(url + "?first"), events.slice(0, reloadAfter ?? events.length))
const shared = globalThis[Symbol.for("sidepulse.opencode.state")]
const waitForDelivery = async () => {
  while (delivered < cleanups.length) await new Promise((resolve) => setTimeout(resolve, 5))
}

if (reloadAfter != null) {
  await waitForDelivery()
  await shared.queue
}
if (reload || reloadAfter != null) {
  cleanups.push(...setup(await import(url + "?second"), events.slice(reloadAfter ?? 0)))
}

await waitForDelivery()
await shared.queue
// With exitEarly, exit while reports are still queued, like `opencode run`.
if (exitEarly) {
  // Let the initial child start while leaving its report undelivered.
  while (firstStarted && !existsSync(firstStarted)) {
    await new Promise((resolve) => setTimeout(resolve, 5))
  }
  process.exit(0)
}
await shared.reporter.idle()
cleanups.forEach((cleanup) => cleanup())
