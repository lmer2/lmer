import { beforeEach, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import Chat from '../src/components/Chat.vue'
import { fetchSessionMessages } from '../src/api.js'
import { mountPane } from './mount.js'

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
