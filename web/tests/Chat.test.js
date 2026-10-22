import { beforeEach, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import Chat from '../src/components/Chat.vue'
import { fetchSessionMessages } from '../src/api.js'
import { deferred, mountPane } from './mount.js'

vi.mock('../src/api.js', () => ({
  fetchSessionMessages: vi.fn(), sendSessionInput: vi.fn(),
  sessionUploadUrl: vi.fn(), uploadSessionFile: vi.fn(),
}))

const text = 'The pane is ready'
const tool = (command, extra = {}) => ({ name: 'Bash', detail: command.split('\n')[0].slice(0, 160), command, status: 'ok', ...extra })
const turn = (extra = {}) => ({
  id: 'transcript', seq: 0, role: 'assistant', kind: 'said', text: '',
  tools: [tool(`lmer-signal post "${text}"`)], ...extra,
})
const record = (extra = {}) => turn({
  id: 'bus', seq: 1, via: 'comms', source: { channel: 'agent' },
  comms_kind: 'message', text, tools: [], ...extra,
})

beforeEach(() => { vi.clearAllMocks() })

async function open(messages) {
  fetchSessionMessages.mockResolvedValue({ messages, total: messages.length, cursor: messages.length })
  const wrapper = mountPane(Chat, { sessionId: 's-1' })
  await flushPromises()
  await vi.dynamicImportSettled()
  await flushPromises()
  expect(fetchSessionMessages).toHaveBeenCalled()
  return wrapper
}

it('mounts the pane and renders an outbound post once, keeping closing prose', async () => {
  const wrapper = await open([
    turn(), record(), turn({ id: 'closing', seq: 2, text: 'I have finished the pane.', tools: [] }),
  ])
  expect(wrapper.findAll('.tools')).toHaveLength(0)
  expect(wrapper.text().split(text)).toHaveLength(2)
  expect(wrapper.text()).toContain('I have finished the pane.')
})

it.each([
  ['lmer-signal note "The   pane is ready"', 'note', 'agent'],
  ["lmer-signal ask 'The pane is ready' --option Yes", 'question', 'uber-lmer'],
  ['/usr/local/bin/lmer-signal post --thread https://example.org/thread "The pane is ready"', 'message', 'agent'],
])('hides the matching outbound command %s', async (detail, kind, channel) => {
  const wrapper = await open([
    turn({ tools: [tool(detail)] }), record({ comms_kind: kind, source: { channel } }),
  ])
  expect(wrapper.findAll('.tools')).toHaveLength(0)
})

it.each([
  ['lmer-signal post "different words"', {}],
  ['lmer-signal post --stdin', {}],
  ['lmer-signal post --message-file /tmp/body.md', {}],
  ['lmer-signal post "The pane is ready', {}],
  ['echo lmer-signal post "The pane is ready"', {}],
  ['lmer-signal post "The pane is ready"; do-other-work', {}],
  ['lmer-signal post "The pane is ready"', { source: { channel: 'slack' } }],
  ['lmer-signal post "The pane is ready"', { comms_kind: 'milestone' }],
])('keeps unmatched or uncertain command %s (%j)', async (detail, extra) => {
  const wrapper = await open([turn({ tools: [tool(detail)] }), record(extra)])
  expect(wrapper.findAll('.tools')).toHaveLength(1)
})

it('preserves failed calls, unrelated tools, and prose on a mixed turn', async () => {
  const wrapper = await open([
    turn({ text: 'Checking the build.', tools: [tool(`lmer-signal post "${text}"`), tool('npm test')] }),
    record(), turn({ id: 'failed', seq: 2, tools: [tool(`lmer-signal post "${text}"`, { status: 'failed' })] }),
  ])
  expect(wrapper.text()).toContain('Checking the build.')
  expect(wrapper.text()).toContain('npm test')
  expect(wrapper.findAll('.tools > div')).toHaveLength(2)
})

it('keeps a command when its bus record is outside the loaded window', async () => {
  const wrapper = await open([turn()])
  expect(wrapper.text()).toContain('lmer-signal post')
})

it('does not treat a non-shell tool detail as a command', async () => {
  const wrapper = await open([turn({ tools: [tool(`lmer-signal post "${text}"`, { name: 'Read' })] }), record()])
  expect(wrapper.findAll('.tools')).toHaveLength(1)
})

it('does not use an inbound uber-lmer message to hide an outbound call', async () => {
  const wrapper = await open([
    turn(), record({ role: 'user', source: { channel: 'uber-lmer' }, comms_kind: null }),
  ])
  expect(wrapper.findAll('.tools')).toHaveLength(1)
})

it.each([
  `lmer-signal post "${text}"\n./scripts/deploy.sh production`,
  `lmer-signal post "${text}"${' '.repeat(160)}; ./scripts/deploy.sh production`,
])('keeps the whole tool row when a matching summary omits other work: %s', async (command) => {
  const wrapper = await open([turn({ tools: [tool(command)] }), record()])
  expect(wrapper.findAll('.tools')).toHaveLength(1)
})

it.each([null, undefined])('keeps a matching summary without a complete command (%s)', async (command) => {
  const wrapper = await open([turn({ tools: [tool(`lmer-signal post "${text}"`, { command })] }), record()])
  expect(wrapper.findAll('.tools')).toHaveLength(1)
})

it('matches a complete command longer than its displayed summary', async () => {
  const body = 'a'.repeat(200)
  const wrapper = await open([turn({ tools: [tool(`lmer-signal post "${body}"`)] }), record({ text: body })])
  expect(wrapper.findAll('.tools')).toHaveLength(0)
})

it('spends one bus record on one matching tool call', async () => {
  const wrapper = await open([
    turn(), turn({ id: 'second', seq: 1 }), turn({ id: 'third', seq: 2 }), record({ seq: 3 }),
  ])
  expect(wrapper.findAll('.tools')).toHaveLength(2)
})

it.each([
  ['lmer-slack post "The pane is ready"', 0],
  ['lmer-slack ask "The pane is ready"', 1],
  ['lmer-slack note "The pane is ready"', 1],
])('restricts lmer-slack to post: %s', async (command, count) => {
  const wrapper = await open([turn({ tools: [tool(command)] }), record()])
  expect(wrapper.findAll('.tools')).toHaveLength(count)
  expect(wrapper.findAll('.turn').some((row) => row.text().includes(text))).toBe(true)
})

it.each([
  ['lmer-signal ask --no-wait "The pane is ready"', 0],
  ['lmer-signal post --no-wait "The pane is ready"', 1],
  ['lmer-signal note --no-wait "The pane is ready"', 1],
])('restricts --no-wait to ask: %s', async (command, count) => {
  const wrapper = await open([turn({ tools: [tool(command)] }), record()])
  expect(wrapper.findAll('.tools')).toHaveLength(count)
})

// --- the two views of the uber lmer pane (issue #488) ------------------------
//
// Shaped after one five-minute stretch of a real uber lmer session: a spool
// take, a watch firing, a hook re-prompt with the filler line it forced, working
// prose, and one post to the operator. Seventeen bubbles then; one message.

const prose = 'Progress from develop-487 is in its own thread; nothing owed, nothing to request.'
const posted = 'Three runs up, one waiting on you: develop-487 asks which branch.'
const said = (id, seq, extra = {}) => ({ id, seq, role: 'assistant', kind: 'said', text: '', tools: [], ...extra })
const session = () => [
  said('prose', 0, { text: prose }),
  said('spool', 1, { tools: [tool('curl -sS -X POST http://127.0.0.1:8700/api/assistant/pending')] }),
  { id: 'watch', seq: 2, role: 'monitor', kind: 'said', via: 'monitor', text: 'Ratchet spool: pending digests > 0\nSPOOL pending=1', tools: [] },
  { id: 'hook', seq: 3, role: 'user', kind: 'injected', text: 'Stop hook feedback: Reply-routing check: this turn produced visible reply text…', tools: [] },
  said('filler', 4, { text: 'Working output only.' }),
  said('post', 5, { via: 'comms', source: { channel: 'uber-lmer' }, comms_kind: 'message', text: posted }),
]

async function openView(messages, view) {
  fetchSessionMessages.mockResolvedValue({ messages, total: messages.length, cursor: messages.length })
  const wrapper = mountPane(Chat, { sessionId: 's-1', view })
  await flushPromises()
  await vi.dynamicImportSettled()
  await flushPromises()
  return wrapper
}

it('the conversation view draws exactly one bubble for that stretch: the post', async () => {
  const wrapper = await openView(session(), 'conversation')
  const turns = wrapper.findAll('.turn')
  expect(turns).toHaveLength(1)
  expect(turns[0].text()).toContain(posted)
  expect(turns[0].classes()).not.toContain('turn-transcript')
  expect(wrapper.findAll('.tools')).toHaveLength(0)
  for (const words of [prose, 'Working output only.', 'Stop hook feedback', 'SPOOL pending', 'api/assistant/pending']) {
    expect(wrapper.text()).not.toContain(words)
  }
  expect(wrapper.text()).toContain('5 transcript turns not shown')
  expect(wrapper.findAll('button').some((node) => /show \d+ internal/.test(node.text()))).toBe(false)
})

it('the hidden count is what the switch would reveal, not what the route served', async () => {
  // The post's own tool row is hidden in the transcript view too (outbound
  // rule), so it must not be counted as a turn the switch would show.
  const wrapper = await openView([
    said('prose', 0, { text: prose }),
    said('call', 1, { tools: [tool(`lmer-signal post "${posted}"`)] }),
    said('post', 2, { via: 'comms', source: { channel: 'uber-lmer' }, comms_kind: 'message', text: posted }),
  ], 'conversation')
  expect(wrapper.text()).toContain('1 transcript turns not shown')
})

it('the conversation view keeps the operator\'s own bus messages', async () => {
  const wrapper = await openView([
    ...session(),
    { id: 'op', seq: 6, role: 'user', kind: 'said', via: 'comms', source: { channel: 'platform', author: 'operator' }, comms_kind: null, text: 'sitrep please', tools: [] },
    { id: 'ask', seq: 7, role: 'user', kind: 'said', via: 'ask', text: 'prep-release', tools: [] },
  ], 'conversation')
  expect(wrapper.findAll('.turn')).toHaveLength(3)
  expect(wrapper.text()).toContain('sitrep please')
  expect(wrapper.text()).toContain('prep-release')
})

it('the transcript view draws the rest muted and chipped, on their own grounds', async () => {
  const wrapper = await openView(session(), 'transcript')
  const turns = wrapper.findAll('.turn')
  expect(turns).toHaveLength(6)
  const muted = wrapper.findAll('.turn.turn-transcript')
  expect(muted).toHaveLength(5)
  const post = turns.find((row) => row.text().includes(posted))
  expect(post.classes()).not.toContain('turn-transcript')
  expect(post.classes()).toContain('ground-agent')
  for (const row of muted) expect(row.text()).toContain('· transcript')
  const watch = turns.find((row) => row.text().includes('SPOOL pending'))
  expect(watch.classes()).toContain('ground-action')
  expect(watch.find('.watch').exists()).toBe(true)
  const hook = turns.find((row) => row.text().includes('Stop hook feedback'))
  expect(hook.classes()).toContain('ground-action')
  expect(hook.text()).toContain('· internal')
  expect(wrapper.text()).toContain('muted rows are the working transcript')
  expect(wrapper.findAll('button').some((node) => /show \d+ internal/.test(node.text()))).toBe(false)
})

it('the run chat keeps its rendering when no view is named', async () => {
  const wrapper = await openView(session(), undefined)
  expect(wrapper.findAll('.turn')).toHaveLength(5)
  expect(wrapper.findAll('.turn-transcript')).toHaveLength(0)
  expect(wrapper.text()).toContain('show 1 internal')
  expect(wrapper.text()).not.toContain('Stop hook feedback')
})

// --- what a turn is, and nothing of it is lost (issue #481) ------------------

it('a turn of prose, a tool call, more prose and a post renders each once', async () => {
  const first = 'Saved the note to memory.'
  const second = 'The 657 developer pushed round 2; I turn the reviewer now.'
  const wrapper = await openView([
    said('p1', 0, { text: first }),
    said('t1', 1, { tools: [tool('sed -i s/a/b/ memory.md')] }),
    said('p2', 2, { text: second }),
    said('post', 3, { via: 'comms', source: { channel: 'uber-lmer' }, comms_kind: 'message', text: posted }),
  ], 'transcript')
  for (const words of [first, second, posted]) expect(wrapper.text().split(words)).toHaveLength(2)
  expect(wrapper.findAll('.tools')).toHaveLength(1)
})

it('a post whose words differ from the prose keeps both', async () => {
  const wrapper = await openView([
    said('p1', 0, { text: 'I will post the sitrep now.' }),
    said('post', 1, { via: 'comms', source: { channel: 'uber-lmer' }, comms_kind: 'message', text: posted }),
  ], 'transcript')
  expect(wrapper.text()).toContain('I will post the sitrep now.')
  expect(wrapper.text()).toContain(posted)
})

it('a record holding prose and a tool call together keeps its prose', async () => {
  const wrapper = await openView([
    said('combined', 0, { text: 'Noted — saved to persistent memory.', tools: [tool('sed -i s/a/b/ memory.md')] }),
    said('post', 1, { via: 'comms', source: { channel: 'uber-lmer' }, comms_kind: 'message', text: posted }),
  ], 'transcript')
  const combined = wrapper.findAll('.turn').find((row) => row.text().includes('saved to persistent memory'))
  expect(combined).toBeDefined()
  expect(combined.find('.tools').exists()).toBe(true)
})

it.each([
  ['lmer-signal ask --option=Yes "The pane is ready"', 0],
  ['lmer-signal post --thread=https://example.org/thread "The pane is ready"', 0],
  ['lmer-signal ask --timeout=2 --interval=1 "The pane is ready"', 0],
  ['lmer-signal post --dir=/tmp/channel "The pane is ready"', 1],
  ['lmer-signal post --unknown=value "The pane is ready"', 1],
])('accepts only known equals flags: %s', async (command, count) => {
  const wrapper = await open([turn({ tools: [tool(command)] }), record()])
  expect(wrapper.findAll('.tools')).toHaveLength(count)
})

it.each([
  ['lmer-signal post "The pane is ready"', 0],
  ['lmer-signal milestone "The pane is ready"', 1],
  ['lmer-signal "The pane is ready"', 1],
])('excludes milestone and bare verbs: %s', async (command, count) => {
  const wrapper = await open([turn({ tools: [tool(command)] }), record()])
  expect(wrapper.findAll('.tools')).toHaveLength(count)
})


const execWrapper = (extra = {}) => ({
  name: 'exec', wrapper: true, detail: null, error: null, status: 'ok',
  raw_input: 'text("raw input")', raw_output: 'Script completed', ...extra,
})

it('collapses empty Codex wrappers while retaining inspectable raw details', async () => {
  const wrapper = await open([turn({ tools: [execWrapper()] })])
  expect(wrapper.findAll('.turn')).toHaveLength(0)
  const details = wrapper.find('.wrapper-details')
  expect(details.find('summary').text()).toContain('1 empty tool wrappers')
  expect(details.attributes('open')).toBeUndefined()
  expect(details.text()).toContain('raw input')
  expect(details.text()).toContain('Script completed')
})

it.each([
  { text: 'Meaningful prose', tools: [execWrapper()] },
  { tools: [execWrapper({ detail: '14 passed' })] },
  { tools: [execWrapper({ error: 'ReferenceError', status: 'failed' })] },
  { tools: [execWrapper({ wrapper: false })] },
  { tools: [execWrapper(), tool('pytest')] },
])('keeps prose, results, failures and unrelated exec tools (%j)', async (extra) => {
  const wrapper = await open([turn(extra)])
  expect(wrapper.findAll('.turn')).toHaveLength(1)
})


it.each([false, true])('reconciles late bus posts across polls (combined=%s)', async (combined) => {
  vi.useFakeTimers()
  const prose = turn({ text, tools: combined ? [tool('lmer-signal post --stdin')] : [] })
  const call = turn({ id: 'call', seq: 1 })
  let posted = false
  fetchSessionMessages.mockImplementation(async (_id, { since, limit }) => {
    const rows = posted
      ? [combined ? { ...prose, text: '' } : { ...call, seq: 0 }, record()]
      : (combined ? [prose] : [prose, call])
    const start = since < 0 ? Math.max(0, rows.length + since) : Math.min(since, rows.length)
    const messages = rows.slice(start, start + limit)
    return { messages, from: start, total: rows.length, cursor: start + messages.length }
  })
  const wrapper = mountPane(Chat, { sessionId: 's-1', view: 'conversation' })
  try {
    await flushPromises()
    expect(wrapper.text()).not.toContain(text)
    posted = true
    await vi.advanceTimersByTimeAsync(5000)
    await flushPromises()
    expect(wrapper.text().split(text)).toHaveLength(2)
    await wrapper.setProps({ view: 'transcript' })
    expect(wrapper.text().split(text)).toHaveLength(2)
    await vi.advanceTimersByTimeAsync(5000)
    await flushPromises()
    expect(wrapper.text().split(text)).toHaveLength(2)
  } finally {
    wrapper.unmount()
    vi.useRealTimers()
  }
})

it('keeps the run view\'s row above an empty transcript, and drops it only in fill mode', async () => {
  const runView = await open([])
  const first = (wrapper) => wrapper.get('.v-card-text').element.firstElementChild
  expect(first(runView).classList.contains('mb-3')).toBe(true)
  expect(first(runView).classList.contains('d-flex')).toBe(true)
  fetchSessionMessages.mockResolvedValue({ messages: [], total: 0, cursor: 0 })
  const filled = mountPane(Chat, { sessionId: 's-1', fill: true })
  await flushPromises()
  expect(first(filled).classList.contains('d-flex')).toBe(false)
})

// Late responses outside `before` pages (issue 544 item 3), on the session read.
// Each holds a stale response across poll intervals while the session moves on.
const closed = 'Recorded conversation, not a live one'
const numbered = (count) => Array.from({ length: count }, (_, i) => said(`r${i}`, i, { text: `row ${i}` }))
const serve = (state, since, limit) => {
  const start = since < 0 ? Math.max(0, state.rows.length + since) : Math.min(since, state.rows.length)
  const messages = state.rows.slice(start, start + limit)
  return {
    messages, from: start, total: state.rows.length, cursor: start + messages.length,
    live: state.live, note: state.note,
  }
}

it('holds polling until the first tail lands, so the late tail cannot rewind a poll', async () => {
  vi.useFakeTimers()
  const state = { rows: numbered(100), live: false, note: 'starting' }
  const delayed = deferred()
  let requested
  fetchSessionMessages.mockImplementation(async (_id, { since, limit }) => serve(state, since, limit))
  fetchSessionMessages.mockImplementationOnce((_id, { since, limit }) => {
    requested = serve(state, since, limit)
    return delayed.promise
  })
  const wrapper = mountPane(Chat, { sessionId: 's-1' })
  try {
    Object.assign(state, { rows: numbered(120), live: true, note: null })
    await vi.advanceTimersByTimeAsync(15000)
    // A poll now would read from cursor 0: the head of the conversation, not its
    // end — drawn, then rewound by the tail landing behind it.
    expect(fetchSessionMessages).toHaveBeenCalledTimes(1)
    expect(wrapper.findAll('.turn')).toHaveLength(0)
    delayed.resolve(requested)
    await flushPromises()
    await vi.dynamicImportSettled()
    await flushPromises()
    expect(wrapper.findAll('.turn')).toHaveLength(40)
    await vi.advanceTimersByTimeAsync(5000)
    expect(fetchSessionMessages.mock.calls.at(-1)[1].since).toBe(100)
    expect(wrapper.text()).toContain('row 119')
    expect(wrapper.text()).toContain('120 messages')
    expect(wrapper.text()).not.toContain(closed)
    expect(wrapper.vm.$.setupState.note).toBe(null)
    const texts = wrapper.findAll('.turn').map((row) => row.text())
    expect(texts).toHaveLength(60)
    expect(new Set(texts).size).toBe(texts.length)
    await vi.advanceTimersByTimeAsync(5000)
    expect(fetchSessionMessages.mock.calls.at(-1)[1].since).toBe(120)
  } finally {
    wrapper.unmount()
    vi.useRealTimers()
  }
})

it('keeps what a poll said about the end when an earlier page lands late', async () => {
  vi.useFakeTimers()
  const state = { rows: numbered(100), live: true, note: null }
  fetchSessionMessages.mockImplementation(async (_id, { since, limit }) => serve(state, since, limit))
  const wrapper = mountPane(Chat, { sessionId: 's-1' })
  try {
    await flushPromises()
    await vi.dynamicImportSettled()
    await flushPromises()
    expect(wrapper.text()).not.toContain(closed)
    const delayed = deferred()
    let requested
    fetchSessionMessages.mockImplementationOnce((_id, { since, limit }) => {
      requested = serve(state, since, limit)
      return delayed.promise
    })
    await wrapper.findAll('button').find((button) => button.text() === 'earlier messages').trigger('click')
    // The session writes twenty more turns and exits while the earlier page is in flight.
    Object.assign(state, { rows: numbered(120), live: false, note: 'This session has exited.' })
    await vi.advanceTimersByTimeAsync(5000)
    expect(wrapper.text()).toContain('row 119')
    expect(wrapper.text()).toContain(closed)
    delayed.resolve(requested)
    await flushPromises()
    await vi.dynamicImportSettled()
    await flushPromises()
    expect(requested).toMatchObject({ total: 100, live: true, note: null })
    expect(wrapper.text()).toContain('row 20')
    expect(wrapper.text()).toContain(closed)
    expect(wrapper.text()).toContain('120 messages')
    expect(wrapper.vm.$.setupState.note).toBe('This session has exited.')
    const texts = wrapper.findAll('.turn').map((row) => row.text())
    expect(texts).toHaveLength(100)
    expect(new Set(texts).size).toBe(texts.length)
    await vi.advanceTimersByTimeAsync(5000)
    expect(fetchSessionMessages.mock.calls.at(-1)[1].since).toBe(120)
  } finally {
    wrapper.unmount()
    vi.useRealTimers()
  }
})
