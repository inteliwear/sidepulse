// Loads the SidePulse OpenCode plugin the way OpenCode does, with a fake
// context: `sessions` answer ctx.session.get and `events` are delivered
// through ctx.event.subscribe. `copies` sets how many copies of the plugin
// load at once, like OpenCode does for each open project. With `reload`, a
// second instance of the module loads too, and copies of both receive events,
// as during a hot reload. Reports reach the `sidepulse` command given as the
// second argument. With `exitEarly`, the process exits as soon as events are
// delivered. Used by tests/test_opencode_provider.py.
import { readFileSync } from "node:fs"
import { pathToFileURL } from "node:url"

const [pluginPath, command] = process.argv.slice(2)
const { sessions, events, copies = 1, exitEarly = false, reload = false } = JSON.parse(readFileSync(0, "utf8"))
const url = pathToFileURL(pluginPath).href

let delivered = 0
const context = () => ({
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
        for (const event of events) if (types.includes(event.type)) yield event
      } finally {
        delivered++
      }
    },
  },
})

const modules = [await import(url + "?first")]
if (reload) modules.push(await import(url + "?second"))
const cleanups = modules.flatMap(({ default: plugin }) =>
  Array.from({ length: copies }, () => plugin.setup(context())),
)

while (delivered < cleanups.length) await new Promise((resolve) => setTimeout(resolve, 5))
const shared = globalThis[Symbol.for("sidepulse.opencode.state")]
await shared.queue
// With exitEarly, exit while reports are still queued, like `opencode run`.
if (exitEarly) process.exit(0)
await shared.reporter.idle()
cleanups.forEach((cleanup) => cleanup())
