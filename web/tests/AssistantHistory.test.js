import { beforeEach, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import Chat from '../src/components/Chat.vue'
import { fetchAssistantHistory, fetchSessionMessages, sessionUploadUrl } from '../src/api.js'
import { historyWithoutOutboundCopies, mergeHistory } from '../src/assistantHistory.js'
import { mountPane, deferred } from './mount.js'
import { onRunRef } from '../src/runref.js'

vi.mock('../src/api.js', () => ({
  fetchAssistantHistory: vi.fn(), fetchSessionMessages: vi.fn(),
  sendSessionInput: vi.fn(), sessionUploadUrl: vi.fn(), uploadSessionFile: vi.fn(),
}))

const rotation = 'Assistant rotated. Messages above are kept for your reference. The new assistant received a handover; it does not have this conversation verbatim in its current context.'
const row = (n, seq, text, extra = {}) => ({
  id: `s-${n}:${seq}`, session: `s-${n}`, generation: n, seq,
  role: 'assistant', kind: 'said', via: 'comms', text, tools: [],
  at: `2026-09-22T12:0${n}:00Z`, source: { channel: 'agent' }, ...extra,
})
const data = (n) => [
  ...(n > 1 ? [row(n, 0, rotation, { role: 'platform', comms_kind: 'rotation' })] : []),
  row(n, n > 1 ? 1 : 0, `Decision ${n}`, { role: 'user' }),
  row(n, n > 1 ? 2 : 1, `Confirmed ${n}`),
]
const pages = new Map()

function page(n, request = {}) {
  const all = pages.get(n) || data(n)
  let end = all.length
  let start
  const limit = request.limit || 40
  if (request.before) {
    end = all.findIndex((message) => message.id === request.before)
    start = Math.max(0, end - limit)
  } else {
    const since = request.since ?? -40
    start = since < 0 ? Math.max(0, all.length + since) : since
  }
  const messages = all.slice(start, Math.min(end, start + limit))
  return {
    session: `s-${n}`, history: { conversation: 'one-conversation', previous_session: n > 1 ? `s-${n - 1}` : null },
    messages, from: start, cursor: start + messages.length, total: all.length,
    live: n === 3, sessions: [], note: null,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  pages.clear()
  fetchAssistantHistory.mockImplementation((session, request) => Promise.resolve(page(Number(session.slice(2)), request)))
  sessionUploadUrl.mockImplementation((session, name) => `/files/${session}/${name}`)
})

async function open(n = 3) {
  const wrapper = mountPane(Chat, { sessionId: `s-${n}`, assistantHistory: true, view: 'conversation' })
  await flushPromises()
  await vi.dynamicImportSettled()
  await flushPromises()
  return wrapper
}

it('reloads all three recent generations in order with exactly two explanatory separators', async () => {
  const wrapper = await open()
  const turns = wrapper.findAll('.turn')
  expect(turns).toHaveLength(8)
  expect(wrapper.findAll('.turn-rotation')).toHaveLength(2)
  expect(turns.map((turn) => turn.text()).join('\n')).toMatch(/Decision 1[\s\S]*Confirmed 1[\s\S]*Decision 2[\s\S]*Confirmed 2[\s\S]*Decision 3/)
  expect(wrapper.text().split(rotation)).toHaveLength(3)
  expect(fetchSessionMessages).not.toHaveBeenCalled()
  wrapper.unmount()
  const reloaded = await open()
  expect(reloaded.findAll('.turn')).toHaveLength(8)
  expect(reloaded.findAll('.turn-rotation')).toHaveLength(2)
})

it('keeps the loaded exchange through two rotations and repeated polls', async () => {
  vi.useFakeTimers()
  const wrapper = await open(1)
  await wrapper.setProps({ sessionId: 's-2' })
  await flushPromises()
  await wrapper.setProps({ sessionId: 's-3' })
  await flushPromises()
  await vi.advanceTimersByTimeAsync(15000)
  expect(wrapper.findAll('.turn')).toHaveLength(8)
  expect(wrapper.findAll('.turn-rotation')).toHaveLength(2)
  expect(wrapper.text()).toContain('Decision 1')
})

it.each(['conversation', 'transcript'])('reconciles late replies in every loaded predecessor in %s view', async (view) => {
  vi.useFakeTimers()
  const wrapper = await open(1)
  await wrapper.setProps({ view })
  await flushPromises()
  await wrapper.setProps({ sessionId: 's-2' })
  await flushPromises()
  await wrapper.setProps({ sessionId: 's-3' })
  await flushPromises()
  pages.set(1, [...data(1), row(1, 2, 'late reply from generation 1')])
  pages.set(2, [...data(2), row(2, 3, 'late reply from generation 2')])
  fetchAssistantHistory.mockClear()
  await vi.advanceTimersByTimeAsync(10000)
  for (const number of [1, 2]) {
    const turns = wrapper.findAll('.turn').filter((turn) => turn.text().includes(`late reply from generation ${number}`))
    expect(turns).toHaveLength(1)
    expect(turns[0].text()).toContain(`generation ${number}`)
  }
  expect(wrapper.findAll('.turn-rotation')).toHaveLength(2)
  expect(fetchAssistantHistory.mock.calls).toHaveLength(4)
  expect(fetchAssistantHistory.mock.calls.every(([, request]) => request.limit <= 500 && request.view === view)).toBe(true)
  await vi.advanceTimersByTimeAsync(10000)
  expect(wrapper.findAll('.turn')).toHaveLength(10)
})

it('pages backward without duplicates and reconciles one bounded predecessor tail per poll', async () => {
  vi.useFakeTimers()
  pages.set(3, Array.from({ length: 100 }, (_, i) => row(3, i, `row ${i}`)))
  const wrapper = await open()
  expect(wrapper.findAll('.turn')).toHaveLength(40)
  const earlier = () => wrapper.findAll('button').find((button) => button.text() === 'earlier messages')
  await earlier().trigger('click')
  await flushPromises()
  expect(wrapper.findAll('.turn')).toHaveLength(80)
  await earlier().trigger('click')
  await flushPromises()
  expect(wrapper.findAll('.turn')).toHaveLength(100)
  await earlier().trigger('click')
  await flushPromises()
  expect(wrapper.text()).toContain('Decision 2')
  fetchAssistantHistory.mockClear()
  await vi.advanceTimersByTimeAsync(5000)
  expect(fetchAssistantHistory.mock.calls.map(([session]) => session)).toEqual(['s-3', 's-2'])
  expect(fetchAssistantHistory.mock.calls.every(([, request]) => request.limit <= 500)).toBe(true)
  expect(new Set(wrapper.findAll('.turn').map((turn) => turn.text())).size).toBe(wrapper.findAll('.turn').length)
})

it('keeps the tail cursor when an older page finishes after polling advanced it', async () => {
  vi.useFakeTimers()
  pages.set(3, Array.from({ length: 100 }, (_, i) => row(3, i, `row ${i}`)))
  const wrapper = await open()
  const delayed = deferred()
  let requested
  fetchAssistantHistory.mockImplementationOnce((session, request) => {
    requested = page(3, request)
    return delayed.promise
  })
  await wrapper.findAll('button').find((button) => button.text() === 'earlier messages').trigger('click')
  pages.set(3, Array.from({ length: 200 }, (_, i) => row(3, i, `row ${i}`)))
  await vi.advanceTimersByTimeAsync(5000)
  expect(wrapper.text()).toContain('row 199')
  delayed.resolve(requested)
  await flushPromises()
  expect(requested.total).toBe(100)
  expect(wrapper.text()).toContain('row 20')
  pages.set(3, [...pages.get(3), row(3, 200, 'late reply')])
  fetchAssistantHistory.mockClear()
  await vi.advanceTimersByTimeAsync(10000)
  const tail = fetchAssistantHistory.mock.calls.filter(([session]) => session === 's-3')
  expect(tail.length).toBeGreaterThan(0)
  expect(tail[0][1].since).toBe(200 - 40)
  expect(wrapper.text()).toContain('late reply')
  const texts = wrapper.findAll('.turn').map((turn) => turn.text())
  expect(new Set(texts).size).toBe(texts.length)
})

it('ignores an old response that arrives after rotation', async () => {
  const pending = deferred()
  fetchAssistantHistory.mockImplementationOnce(() => pending.promise)
  const wrapper = mountPane(Chat, { sessionId: 's-1', assistantHistory: true, view: 'conversation' })
  await wrapper.setProps({ sessionId: 's-3' })
  await flushPromises()
  pending.resolve({ ...page(1), messages: [row(1, 0, 'stale response')] })
  await flushPromises()
  expect(wrapper.text()).not.toContain('stale response')
  expect(wrapper.text()).toContain('Decision 3')
})

it('ignores a predecessor tail response after the history view changes', async () => {
  vi.useFakeTimers()
  const wrapper = await open()
  const pending = deferred()
  fetchAssistantHistory.mockImplementationOnce((session, request) => Promise.resolve(page(3, request)))
  fetchAssistantHistory.mockImplementationOnce(() => pending.promise)
  await vi.advanceTimersByTimeAsync(5000)
  await wrapper.setProps({ view: 'transcript' })
  await flushPromises()
  pending.resolve({ ...page(2), messages: [row(2, 0, 'stale predecessor tail')] })
  await flushPromises()
  expect(wrapper.text()).not.toContain('stale predecessor tail')
  expect(wrapper.text()).toContain('Decision 2')
})

it('catches up large predecessor tails in bounded pages without starving the active generation', async () => {
  vi.useFakeTimers()
  const wrapper = await open()
  pages.set(2, [...data(2), ...Array.from({ length: 600 }, (_, i) => row(2, i + 3, `late backlog ${i}`))])
  fetchAssistantHistory.mockClear()
  await vi.advanceTimersByTimeAsync(15000)
  expect(fetchAssistantHistory.mock.calls.map(([session]) => session)).toEqual(['s-3', 's-2', 's-3', 's-1', 's-3', 's-2'])
  expect(fetchAssistantHistory.mock.calls[4][1].since).toBe(0)
  expect(fetchAssistantHistory.mock.calls[5][1].since).toBe(460)
  expect(wrapper.text()).toContain('late backlog 599')
  expect(wrapper.findAll('.turn')).toHaveLength(608)
  expect(wrapper.findAll('.turn-rotation')).toHaveLength(2)
})

it('fills a busy replacement and a skipped generation without hiding the gap', async () => {
  const wrapper = await open(1)
  pages.set(3, [
    row(3, 0, rotation, { role: 'platform', comms_kind: 'rotation' }),
    ...Array.from({ length: 100 }, (_, i) => row(3, i + 1, `busy ${i}`)),
  ])
  await wrapper.setProps({ sessionId: 's-3' })
  await flushPromises()
  expect(wrapper.text()).toContain('Decision 1')
  expect(wrapper.text()).toContain('fill the gap')
  for (let count = 0; count < 3; count += 1) {
    await wrapper.findAll('button').find((button) => button.text() === 'earlier messages').trigger('click')
    await flushPromises()
  }
  expect(wrapper.findAll('.turn')).toHaveLength(106)
  expect(wrapper.findAll('.turn-rotation')).toHaveLength(2)
  expect(wrapper.text()).toContain('Decision 2')
  expect(wrapper.text()).not.toContain('fill the gap')
})

it('keeps working transcript content hidden and reloads the selected view', async () => {
  pages.set(3, [...data(3), row(3, 3, 'hidden tool output', { via: null })])
  const wrapper = await open()
  expect(wrapper.text()).not.toContain('hidden tool output')
  await wrapper.setProps({ view: 'transcript' })
  await flushPromises()
  expect(fetchAssistantHistory.mock.calls.some(([, request]) => request.view === 'transcript')).toBe(true)
  expect(wrapper.find('.turn-transcript').text()).toContain('hidden tool output')
})

it('keeps equal transcript filenames attached to their original session', async () => {
  pages.set(1, [row(1, 0, '[lmer upload] /home/developer/.lmer-uploads/photo.png (image/png, 100 bytes)', { role: 'user', origin: 'same' })])
  const wrapper = await open()
  expect(wrapper.text()).toContain('photo.png')
  expect(sessionUploadUrl.mock.calls.some(([session]) => session === 's-1')).toBe(true)
})

it('reconciles replacement rows within one generation without losing earlier generations', () => {
  const old = [row(1, 0, 'kept'), row(2, 0, 'old prose'), row(2, 1, 'reply')]
  const page = { session: 's-2', from: 0, cursor: 1, total: 1, messages: [row(2, 0, 'new bus record', { id: 'new' })] }
  expect(mergeHistory(old, page).map((message) => message.text)).toEqual(['kept', 'new bus record'])
})

it('never spends a new generation’s post on an old generation’s tool call', () => {
  const call = row(1, 0, '', { via: null, tools: [{ name: 'Bash', command: 'lmer-signal post "same"', detail: 'lmer-signal post "same"', status: 'ok' }] })
  const post = row(2, 0, 'same', { comms_kind: 'message' })
  expect(historyWithoutOutboundCopies([call, post])[0].tools).toHaveLength(1)
})

it('draws platform digests as compact, attributed rows that open their run', async () => {
  const run = { host: 'gitlab.example.com', project: 'group/app', slug: 'run-1', label: 'Fix the widget' }
  const body = 'gitlab.example.com/group/app/run-1 needs you — live_question: May I proceed?\n\nThis needs your approval because it is project-wide.'
  pages.set(3, [
    ...data(3),
    row(3, 2, body, { id: 'digest-1', role: 'platform', source: { channel: 'platform' }, comms_kind: 'digest: live_question',
      digest: { kind: 'live_question', run } }),
    row(3, 3, 'old record without a run', { id: 'digest-2', role: 'platform', source: { channel: 'platform' },
      comms_kind: 'digest: complete', digest: { kind: 'complete', run: null } }),
  ])
  const opened = []
  const stop = onRunRef((ref) => opened.push(ref.key))
  const wrapper = await open()
  const digests = wrapper.findAll('.turn-digest')
  expect(digests).toHaveLength(2)
  const header = digests[0].find('.d-flex').text()
  expect(header).toContain('lmer platform')
  expect(header).not.toMatch(/\byou\b/)
  expect(header).toContain('digest: live_question')
  const toggle = digests[0].find('button.digest-toggle')
  const full = digests[0].find('p.said')
  expect(toggle.text()).toBe('needs you — live_question: May I proceed?')
  expect(toggle.attributes('aria-expanded')).toBe('false')
  expect(full.isVisible()).toBe(false)
  await toggle.trigger('click')
  expect(toggle.attributes('aria-expanded')).toBe('true')
  expect(full.isVisible()).toBe(true)
  expect(full.text()).toContain('This needs your approval')
  await toggle.trigger('click')
  expect(full.isVisible()).toBe(false)
  await digests[0].find('button.digest-run').trigger('click')
  expect(opened).toEqual(['gitlab.example.com/group/app/run-1'])
  expect(digests[1].find('button.digest-run').exists()).toBe(false)
  expect(digests[1].find('.d-flex').text()).toContain('lmer platform')
  expect(wrapper.findAll('.turn').filter((turn) => turn.classes('turn-digest')).every(
    (turn) => !turn.classes('ground-operator'))).toBe(true)
  stop()
})
