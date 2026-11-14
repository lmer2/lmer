import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { Terminal } from '@xterm/xterm'
import { isColorQuery, silenceTerminalQueries } from '../src/terminalQueries.js'

const ESC = '\x1b'

// What agy sends at startup: DECRQM for synchronized output and grapheme clustering.
const AGY_STARTUP = `x\r\n${ESC}[?2026$p${ESC}[?2027$pafter`

// Every sequence that makes xterm 6.0.0 answer, found by writing candidates into an
// unsilenced, opened terminal and watching onData (the control below repeats that,
// so a sequence that stops replying fails there rather than passing here for
// nothing). Opened matters: the colour queries are answered only once it has a theme.
const QUERIES = {
  'DA1': `${ESC}[c`,
  'DA1 with a parameter': `${ESC}[0c`,
  'DA2': `${ESC}[>c`,
  'DSR operating status': `${ESC}[5n`,
  'DSR cursor position': `${ESC}[6n`,
  'DECXCPR': `${ESC}[?6n`,
  'DECRQM ANSI mode': `${ESC}[4$p`,
  'DECRQM private mode': `${ESC}[?2026$p`,
  'DECRQSS': `${ESC}P$qm${ESC}\\`,
  'OSC 4 palette colour': `${ESC}]4;1;?${ESC}\\`,
  'OSC 10 foreground colour': `${ESC}]10;?${ESC}\\`,
  'OSC 11 background colour, BEL-terminated': `${ESC}]11;?\x07`,
  'OSC 12 cursor colour': `${ESC}]12;?${ESC}\\`,
  'OSC 10 with two slots': `${ESC}]10;?;?${ESC}\\`,
}

// Questions xterm 6.0.0 does not answer at all. Pinned so that a version that starts
// answering one is noticed here, not in a harness prompt.
const UNANSWERED = {
  'XTVERSION': `${ESC}[>q`,
  'kitty keyboard flags': `${ESC}[?u`,
  'window size in pixels': `${ESC}[14t`,
  'window size in characters': `${ESC}[18t`,
  'OSC 52 clipboard': `${ESC}]52;c;?${ESC}\\`,
}

const terminals = []

// xterm's open() subscribes to the device pixel ratio with the legacy addListener,
// which the shared setup's matchMedia does not have.
beforeEach(() => {
  window.matchMedia = (query) => ({
    media: query, matches: false,
    addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {},
  })
})

function terminal({ silenced }) {
  const term = new Terminal({ allowProposedApi: true })
  terminals.push(term)
  const replies = []
  term.onData((data) => replies.push(data))
  if (silenced) silenceTerminalQueries(term)
  term.open(document.body.appendChild(document.createElement('div')))
  return { term, replies }
}

function write(term, data) {
  return new Promise((resolve) => term.write(data, resolve))
}

function row(term, y) {
  return term.buffer.active.getLine(y)?.translateToString(true)
}

afterEach(() => {
  while (terminals.length) terminals.pop().dispose()
})

describe('without the silencer (control)', () => {
  it('answers agy\'s startup queries, so the cases below exercise them', async () => {
    const { term, replies } = terminal({ silenced: false })
    await write(term, AGY_STARTUP)
    expect(replies).toHaveLength(2)
    expect(replies[0]).toMatch(/^\x1b\[\?2026;\d\$y$/)
    expect(replies[1]).toMatch(/^\x1b\[\?2027;\d\$y$/)
  })

  it.each(Object.entries(QUERIES))('answers %s', async (_name, query) => {
    const { term, replies } = terminal({ silenced: false })
    await write(term, query)
    expect(replies.length).toBeGreaterThan(0)
  })

  it.each(Object.entries(UNANSWERED))('does not answer %s', async (_name, query) => {
    const { term, replies } = terminal({ silenced: false })
    await write(term, query)
    expect(replies).toEqual([])
  })
})

describe('with the silencer', () => {
  it('sends nothing for agy\'s startup and renders the text after it', async () => {
    const { term, replies } = terminal({ silenced: true })
    await write(term, AGY_STARTUP)
    expect(replies).toEqual([])
    expect(row(term, 0)).toBe('x')
    expect(row(term, 1)).toBe('after')
  })

  it.each(Object.entries(QUERIES))('sends nothing for %s and keeps going', async (_name, query) => {
    const { term, replies } = terminal({ silenced: true })
    await write(term, `before${query}after`)
    expect(replies).toEqual([])
    expect(row(term, 0)).toBe('beforeafter')
  })

  it('still runs the other sequences a query is mixed in with', async () => {
    const { term, replies } = terminal({ silenced: true })
    await write(term, `abc${ESC}[2D${ESC}[?2026$p${ESC}[31mX`)
    expect(replies).toEqual([])
    expect(row(term, 0)).toBe('aXc')
  })

  it('still lets a mode be set, which is a different sequence from asking about it', async () => {
    const { term } = terminal({ silenced: true })
    await write(term, `${ESC}[?2004h`)
    expect(term.modes.bracketedPasteMode).toBe(true)
  })

  it.each([
    ['asking', '?', true],
    ['asking among slots', '1;?;2;rgb:00/00/00', true],
    ['setting a colour', 'rgb:11/22/33', false],
    ['setting a palette entry', '1;#ff0000', false],
    ['resetting', '', false],
  ])('treats a colour payload %s as a query: %s -> %s', (_name, payload, query) => {
    expect(isColorQuery(payload)).toBe(query)
  })

  it('still sends what is typed', async () => {
    const { term, replies } = terminal({ silenced: true })
    term.input('a', true)
    expect(replies).toEqual(['a'])
  })
})
