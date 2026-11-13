// Runs a minified xterm bundle (the module on stdin) against one write and prints
// what happened as JSON. A separate process because the failure it exists to catch
// is an exception thrown from xterm's own write loop, which a test runner reports as
// an unhandled error of the whole run instead of as a result.
//
//   node terminalBundleRun.mjs <silenced|control> <data as JSON string>
import { JSDOM } from 'jsdom'

const [mode, dataJson] = process.argv.slice(2)
const chunks = []
for await (const chunk of process.stdin) chunks.push(chunk)
const source = Buffer.concat(chunks).toString('utf8')

const dom = new JSDOM('<!doctype html><body></body>', { pretendToBeVisual: true })
globalThis.window = dom.window
globalThis.document = dom.window.document
Object.defineProperty(globalThis, 'navigator', {
  value: dom.window.navigator, configurable: true,
})

// open() reaches for these, and the colour queries are answered only by an opened
// terminal.
dom.window.HTMLCanvasElement.prototype.getContext = () => null
dom.window.matchMedia = (query) => ({
  media: query, matches: false,
  addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {},
})
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }

const uncaught = []

const bundle = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`)
const term = new bundle.Terminal({ allowProposedApi: true })
const replies = []
term.onData((data) => replies.push(data))
if (mode === 'silenced') bundle.silenceTerminalQueries(term)
term.open(document.body.appendChild(document.createElement('div')))
// Armed after setup, so a harness fault above fails this process instead of being
// reported as the xterm failure under test.
process.on('uncaughtException', (exc) => uncaught.push(String(exc)))

// A write whose callback never fires is how the crash shows up, so the wait is
// bounded and "finished" is reported rather than assumed.
const finished = await Promise.race([
  new Promise((resolve) => term.write(JSON.parse(dataJson), () => resolve(true))),
  new Promise((resolve) => setTimeout(() => resolve(false), 1500)),
])
const rows = []
for (let y = 0; y < term.buffer.active.length; y += 1) {
  const text = term.buffer.active.getLine(y)?.translateToString(true)
  if (text) rows.push(text)
}
process.stdout.write(JSON.stringify({ finished, uncaught, replies, rows }))
process.exit(0)
