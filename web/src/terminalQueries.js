// Terminal queries this viewer must leave unanswered.
//
// xterm.js answers a program's questions about the terminal itself ("what are you",
// "where is the cursor", "do you support mode N", "what colour is the background")
// and delivers the answer through onData, the callback keystrokes arrive on. Here
// that callback is wired to the session's PTY, so a reply is typed into the harness:
// and the log is replayed on every attach, so every reload would type the same
// replies again. The question was already answered, once, by the PTY's real
// terminal (the supervisor's).
//
// Registering a handler that returns true also keeps xterm's own handler from
// running, which matters for DECRQM: that one throws in the production bundle
// (its minified enum is assigned without a declaration), and an exception inside a
// parser handler abandons the rest of the write it arrived in.
//
// Only sequences that make xterm 6 reply, measured on an opened terminal (the
// colour queries are answered only once it has a theme), are listed. XTVERSION,
// OSC 52 and the window-size reports are not answered, so they need nothing.
//
// Not covered: the focus report xterm types when a program turns on mode 1004. That
// is a mode being set, which xterm has to keep, and not a query.
const SILENCED_CSI = [
  { final: 'c' },
  { prefix: '>', final: 'c' },
  { final: 'n' },
  { prefix: '?', final: 'n' },
  { intermediates: '$', final: 'p' },
  { prefix: '?', intermediates: '$', final: 'p' },
]

const SILENCED_DCS = [
  { intermediates: '$', final: 'q' },
]

// Palette, foreground, background and cursor colour: the same OSC numbers set a
// colour and ask for it, the asking slot being a literal `?`.
const COLOR_OSC = [4, 10, 11, 12]

// Only the asking form is taken, so a program that sets a colour still reaches
// xterm's own handler. A payload that sets one slot and asks about another is
// dropped whole, which costs the set.
export function isColorQuery(payload) {
  return payload.split(';').includes('?')
}

// Before the first write: a handler registered after the replay has started would
// miss the queries in it.
export function silenceTerminalQueries(term) {
  for (const id of SILENCED_CSI) term.parser.registerCsiHandler(id, () => true)
  for (const id of SILENCED_DCS) term.parser.registerDcsHandler(id, () => true)
  for (const id of COLOR_OSC) term.parser.registerOscHandler(id, isColorQuery)
}
