// Loads the SidePulse OpenCode plugin the way OpenCode does, with a fake
// context: `sessions` answer ctx.session.get and `events` are delivered
// through ctx.event.subscribe. `copies` sets how many copies of the plugin
// load at once, like OpenCode does for each open project. Reports reach the
// `sidepulse` command given as the second argument.
// Used by tests/test_opencode_provider.py.
import { readFileSync } from "node:fs"
import { pathToFileURL } from "node:url"

const [pluginPath, command] = process.argv.slice(2)
const { default: plugin } = await import(pathToFileURL(pluginPath).href)
const { sessions, events, copies = 1 } = JSON.parse(readFileSync(0, "utf8"))

const delivered = []
const contexts = Array.from({ length: copies }, () => ({
  options: { command },
  session: {
    async get({ sessionID }) {
      if (!sessions[sessionID]) throw new Error(`unknown session ${sessionID}`)
      return { id: sessionID, ...sessions[sessionID] }
    },
  },
  event: {
    async *subscribe({ types }) {
      try {
        for (const event of events) if (types.includes(event.type)) yield event
      } finally {
        delivered.push(true)
      }
    },
  },
}))
const cleanups = contexts.map((ctx) => plugin.setup(ctx))

const shared = globalThis[Symbol.for("sidepulse.opencode")]
while (delivered.length < copies) await new Promise((resolve) => setTimeout(resolve, 5))
await shared.queue
cleanups.forEach((cleanup) => cleanup())
