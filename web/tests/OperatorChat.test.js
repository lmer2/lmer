import { beforeEach, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import RunDetail from '../src/components/RunDetail.vue'
import AskHistory from '../src/components/AskHistory.vue'
import { fetchAssistantConfig, fetchSessionAsk } from '../src/api.js'
import { applyChatSettings, retainedEntries } from '../src/operatorChat.js'
import { mountPane, deferred } from './mount.js'

vi.mock('../src/api.js', () => ({
  fetchSessionAsk: vi.fn(), fetchAssistantConfig: vi.fn(),
  forgetRun: vi.fn(), resumeRun: vi.fn(),
}))
const entries = (count) => Array.from({ length: count }, (_, index) => ({
  id: String(index + 1), kind: 'note', text: `Note ${index + 1}`,
}))
let session = 0
beforeEach(() => {
  vi.clearAllMocks()
  window.localStorage.clear()
  window.localStorage.setItem('lmer.run.tab', 'lmer')
  window.localStorage.setItem('lmer.run.pane', 'conversation')
  Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' })
  applyChatSettings({ chat: { keep_last: { value: 0 } } })
  fetchAssistantConfig.mockResolvedValue({ chat: { keep_last: { value: 0 } } })
  fetchSessionAsk.mockResolvedValue({ entries: entries(2) })
  session += 1
})

async function detail(id = `session-${session}`) {
  const wrapper = mountPane(RunDetail, { run: {
    host: 'gitlab.example.com', project: 'acme/widget', slug: id,
    last_session_id: id, state: 'in-progress', live: true, questions: [],
  } }, { global: { stubs: {
    Chat: true, Terminal: true, RelatedRuns: true, RunMeta: true, ThreadLinks: true,
  } } })
  await flushPromises()
  await vi.dynamicImportSettled()
  await flushPromises()
  return wrapper
}

it('keeps every entry by default and never mutates the feed when limiting it', () => {
  const feed = entries(4)
  expect(retainedEntries(feed, 0)).toEqual(feed)
  expect(retainedEntries(feed, 2).map((entry) => entry.id)).toEqual(['3', '4'])
  expect(feed).toHaveLength(4)
})

it('mounts retained history, hides older entries, and restores them when disabled', async () => {
  const feed = entries(4)
  fetchSessionAsk.mockResolvedValue({ entries: feed })
  const wrapper = mountPane(AskHistory, { sessionId: 'history', keepLast: 2 })
  await flushPromises()
  await vi.dynamicImportSettled()
  await flushPromises()
  expect(wrapper.findAll('.entry')).toHaveLength(2)
  expect(wrapper.text()).not.toContain('Note 1')
  expect(wrapper.text()).toContain('Note 4')
  await wrapper.setProps({ keepLast: 0 })
  expect(wrapper.findAll('.entry')).toHaveLength(4)
  expect(feed).toHaveLength(4)
})

it('shows a count before the operator tab opens, clears unread on view, and remembers it on remount', async () => {
  const wrapper = await detail()
  const tab = wrapper.get('.operator-chat-tab')
  expect(tab.text()).toContain('2')
  expect(tab.find('[aria-label="Unread messages"]').exists()).toBe(true)
  await tab.trigger('click')
  await flushPromises()
  expect(tab.find('[aria-label="Unread messages"]').exists()).toBe(false)
  wrapper.unmount()
  window.localStorage.setItem('lmer.run.pane', 'conversation')
  const again = await detail()
  expect(again.get('.operator-chat-tab').find('[aria-label="Unread messages"]').exists()).toBe(false)
  again.unmount()
  const other = await detail('another-session')
  expect(other.get('.operator-chat-tab').find('[aria-label="Unread messages"]').exists()).toBe(true)
})

it('counts retained entries and does not clear new arrivals while the browser is hidden', async () => {
  vi.useFakeTimers()
  const wrapper = await detail()
  const tab = wrapper.get('.operator-chat-tab')
  await tab.trigger('click')
  await flushPromises()
  fetchAssistantConfig.mockResolvedValue({ chat: { keep_last: { value: 1 } } })
  applyChatSettings({ chat: { keep_last: { value: 1 } } })
  Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'hidden' })
  document.dispatchEvent(new Event('visibilitychange'))
  fetchSessionAsk.mockResolvedValue({ entries: entries(3) })
  await vi.advanceTimersByTimeAsync(5000)
  await flushPromises()
  expect(tab.text()).toContain('1')
  expect(tab.find('[aria-label="Unread messages"]').exists()).toBe(true)
  Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' })
  document.dispatchEvent(new Event('visibilitychange'))
  await flushPromises()
  expect(tab.find('[aria-label="Unread messages"]').exists()).toBe(false)
})

it('clears the previous session from history while its replacement is loading', async () => {
  const wrapper = mountPane(AskHistory, { sessionId: 'old' })
  await flushPromises()
  await vi.dynamicImportSettled()
  await flushPromises()
  expect(wrapper.text()).toContain('Note 1')
  const pending = deferred()
  fetchSessionAsk.mockReturnValueOnce(pending.promise)
  await wrapper.setProps({ sessionId: 'new' })
  expect(wrapper.text()).not.toContain('Note 1')
  pending.resolve({ entries: [{ id: '1', kind: 'note', text: 'New session' }] })
  await flushPromises()
  expect(wrapper.text()).toContain('New session')
})

it('marks a new answer to a previously viewed question unread', async () => {
  vi.useFakeTimers()
  const question = { id: 'q', kind: 'question', text: 'Proceed?' }
  fetchSessionAsk.mockResolvedValue({ entries: [question] })
  const wrapper = await detail()
  const tab = wrapper.get('.operator-chat-tab')
  await tab.trigger('click')
  await flushPromises()
  const conversation = wrapper.findAll('.v-tab').find((item) => item.text().trim() === 'conversation')
  await conversation.trigger('click')
  fetchSessionAsk.mockResolvedValue({ entries: [{ ...question, answered: true,
    answer: { text: 'Yes', answered_at: '2026-09-09T11:00:00Z' },
  }] })
  await vi.advanceTimersByTimeAsync(5000)
  await flushPromises()
  expect(tab.find('[aria-label="Unread messages"]').exists()).toBe(true)
})
