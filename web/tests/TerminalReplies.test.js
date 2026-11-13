import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import Terminal from '../src/components/Terminal.vue'
import { fetchSessionLog, mintTtyTicket } from '../src/api.js'
import { mountPane } from './mount.js'

vi.mock('../src/api.js', async (importOriginal) => ({
  ...(await importOriginal()),
  fetchSessionLog: vi.fn(),
  mintTtyTicket: vi.fn(),
}))

const ESC = '\x1b'

class FakeSocket {
  static OPEN = 1
  static instances = []

  constructor(url) {
    this.url = url
    this.readyState = FakeSocket.OPEN
    this.sent = []
    FakeSocket.instances.push(this)
  }

  send(data) { this.sent.push(JSON.parse(data)) }

  close() { this.readyState = 3 }

  deliver(frame) { this.onmessage({ data: JSON.stringify(frame) }) }

  inputFrames() { return this.sent.filter((frame) => frame.type === 'input') }
}

const b64 = (text) => btoa(text)

function chunk(text) {
  return {
    data: b64(text), offset: 0, size: text.length, next_offset: text.length,
    live: true, source: 'container',
  }
}

function row(wrapper, y) {
  return wrapper.vm.term.buffer.active.getLine(y)?.translateToString(true)
}

let realWebSocket
beforeEach(() => {
  // xterm's open() subscribes to the device pixel ratio with the legacy
  // addListener, which the shared setup's matchMedia does not have.
  const media = (query) => ({
    media: query, matches: false,
    addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {},
  })
  vi.stubGlobal('matchMedia', media)
  window.matchMedia = media
  realWebSocket = globalThis.WebSocket
  globalThis.WebSocket = FakeSocket
  FakeSocket.instances = []
  vi.mocked(mintTtyTicket).mockResolvedValue({ ticket: 't' })
})

afterEach(() => {
  globalThis.WebSocket = realWebSocket
})

async function attach(replay) {
  vi.mocked(fetchSessionLog).mockResolvedValue(chunk(replay))
  const wrapper = mountPane(Terminal, { sessionId: 's1' })
  await flushPromises()
  const socket = FakeSocket.instances.at(-1)
  socket.deliver({ type: 'open', live: true })
  return { wrapper, socket }
}

it('renders past a DECRQM in the replay and types nothing into a live session', async () => {
  const { wrapper, socket } = await attach(
    `x\r\n${ESC}[?2026$p${ESC}[?2027$p${ESC}[c${ESC}]10;?\x07after`,
  )
  await vi.waitFor(() => expect(row(wrapper, 1)).toBe('after'))
  expect(socket.inputFrames()).toEqual([])
})

it('types nothing for a query that arrives on the live stream', async () => {
  const { wrapper, socket } = await attach('start')
  const live = `\r\n${ESC}[6n${ESC}[>c${ESC}]11;?${ESC}\\${ESC}[?2026$plive`
  socket.deliver({
    type: 'data', data: b64(live), next_offset: 5 + live.length, source: 'container',
  })
  await vi.waitFor(() => expect(row(wrapper, 1)).toBe('live'))
  expect(socket.inputFrames()).toEqual([])
})

it('still sends what the operator types', async () => {
  const { wrapper, socket } = await attach('start')
  wrapper.vm.term.input('a', true)
  expect(socket.inputFrames()).toEqual([
    { type: 'input', data: 'a', append_newline: false },
  ])
})
