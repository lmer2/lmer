import { beforeEach, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import AskChannel from '../src/components/AskChannel.vue'
import { fetchSessionAsk } from '../src/api.js'
import { applyChatSettings, dockKeepLastNotes } from '../src/operatorChat.js'
import { mountPane } from './mount.js'

vi.mock('../src/api.js', () => ({
  fetchSessionAsk: vi.fn(), answerSessionQuestion: vi.fn(), fetchAssistantConfig: vi.fn(),
}))

// The dock's retention (issue 465): one platform setting, the newest N notes beside
// every open question, 0 for open questions only, unset for everything.
const feed = [
  { id: 'n1', kind: 'note', text: 'Oldest note' },
  { id: 'q1', kind: 'question', text: 'Answered question?', answered: true,
    answer: { text: 'Yes', answered_at: '2026-10-29T10:00:00Z' } },
  { id: 'q2', kind: 'question', text: 'Still open question?', answered: false, closed: false },
  { id: 'n2', kind: 'note', text: 'Middle note' },
  { id: 'n3', kind: 'note', text: 'Newest note' },
]
let session = 0

function keep(value) {
  applyChatSettings({ dock: { keep_last_notes: { value, stored: value, source: 'config.json' } } })
}

async function dock(live = true) {
  session += 1
  const wrapper = mountPane(AskChannel, { sessionId: `dock-${session}`, live })
  await flushPromises()
  await vi.dynamicImportSettled()
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  vi.clearAllMocks()
  keep(null)
  fetchSessionAsk.mockResolvedValue({ entries: feed })
})

it('shows every note when the setting is unset', async () => {
  expect(dockKeepLastNotes.value).toBeNull()
  const text = (await dock()).text()
  for (const said of ['Oldest note', 'Answered question?', 'Still open question?', 'Middle note', 'Newest note']) {
    expect(text).toContain(said)
  }
})

it('keeps the newest N notes and every open question', async () => {
  keep(1)
  const text = (await dock()).text()
  expect(text).toContain('Newest note')
  expect(text).toContain('Still open question?')
  for (const gone of ['Oldest note', 'Answered question?', 'Middle note']) expect(text).not.toContain(gone)
})

it('shows open questions only at 0, answerable while the session is live', async () => {
  keep(0)
  const wrapper = await dock()
  expect(wrapper.text()).toContain('Still open question?')
  expect(wrapper.find('textarea').exists()).toBe(true)
  for (const gone of ['Oldest note', 'Answered question?', 'Middle note', 'Newest note']) {
    expect(wrapper.text()).not.toContain(gone)
  }
  expect(wrapper.text()).not.toContain('clear from here')
})

it('keeps an unanswerable open question at 0 too, with the sentence that says why', async () => {
  keep(0)
  const text = (await dock(false)).text()
  expect(text).toContain('Still open question?')
  expect(text).toContain('nothing is reading the channel any more')
  expect(text).not.toContain('Newest note')
})

it('renders nothing when retention leaves nothing and no question is open', async () => {
  keep(0)
  fetchSessionAsk.mockResolvedValue({ entries: feed.filter((entry) => entry.id !== 'q2') })
  const wrapper = await dock()
  expect(wrapper.text()).toBe('')
})

it('keeps clear from here working, without bringing older notes back', async () => {
  keep(2)
  const wrapper = await dock()
  expect(wrapper.text()).toContain('Middle note')
  expect(wrapper.text()).toContain('Newest note')
  const clear = wrapper.findAll('button').find((button) => button.text().includes('clear from here'))
  await clear.trigger('click')
  await flushPromises()
  const text = wrapper.text()
  expect(text).toContain('Still open question?')
  for (const gone of ['Oldest note', 'Answered question?', 'Middle note', 'Newest note']) expect(text).not.toContain(gone)
  expect(text).toContain('2 cleared from here this visit')
})

it('follows a setting change without a remount', async () => {
  const wrapper = await dock()
  expect(wrapper.text()).toContain('Oldest note')
  keep(1)
  await flushPromises()
  expect(wrapper.text()).not.toContain('Oldest note')
  expect(wrapper.text()).toContain('Newest note')
  keep(null)
  await flushPromises()
  expect(wrapper.text()).toContain('Oldest note')
})
