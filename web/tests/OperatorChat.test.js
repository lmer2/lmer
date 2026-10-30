import { beforeEach, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import RunDetail from '../src/components/RunDetail.vue'
import AskHistory from '../src/components/AskHistory.vue'
import { fetchAssistantConfig, fetchSessionAsk, resumeRun } from '../src/api.js'
import { applyChatSettings } from '../src/operatorChat.js'
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
  applyChatSettings({ dock: { keep_last_notes: { value: null } } })
  fetchAssistantConfig.mockResolvedValue({ dock: { keep_last_notes: { value: null } } })
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

// The dock's retention (issue 465) never reaches the record: whatever the dock
// keeps, the tab is the whole channel.
it('mounts the whole history whatever the dock keeps', async () => {
  for (const value of [0, 1, null]) {
    applyChatSettings({ dock: { keep_last_notes: { value } } })
    fetchSessionAsk.mockResolvedValue({ entries: entries(4) })
    const wrapper = mountPane(AskHistory, { sessionId: `history-${value}` })
    await flushPromises()
    await vi.dynamicImportSettled()
    await flushPromises()
    expect(wrapper.findAll('.entry')).toHaveLength(4)
    expect(wrapper.text()).toContain('Note 1')
    expect(wrapper.text()).toContain('Note 4')
    wrapper.unmount()
  }
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

it('counts the full history whatever the dock keeps, and does not clear new arrivals while the browser is hidden', async () => {
  vi.useFakeTimers()
  const wrapper = await detail()
  const tab = wrapper.get('.operator-chat-tab')
  await tab.trigger('click')
  await flushPromises()
  fetchAssistantConfig.mockResolvedValue({ dock: { keep_last_notes: { value: 1 } } })
  applyChatSettings({ dock: { keep_last_notes: { value: 1 } } })
  Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'hidden' })
  document.dispatchEvent(new Event('visibilitychange'))
  fetchSessionAsk.mockResolvedValue({ entries: entries(3) })
  await vi.advanceTimersByTimeAsync(5000)
  await flushPromises()
  expect(tab.get('.operator-count').text()).toMatch(/^3\b/)
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

it('binds fresh continuation approval to the refused source UID', async () => {
  window.localStorage.setItem('lmer.run.tab', 'overview')
  const refusal = Object.assign(new Error(
    'source has no final sign-out (operator=alice, host=build.example)',
  ), {
    code: 'unsigned_session',
    source: { uid: 'a'.repeat(32), operator: 'alice', hostname: 'build.example' },
  })
  resumeRun.mockRejectedValueOnce(refusal).mockResolvedValueOnce({
    session: { session_id: 's-new' }, note: 'fresh continuation started',
  })
  const wrapper = mountPane(RunDetail, { run: {
    host: 'gitlab.example.com', project: 'acme/widget', slug: 'develop-1',
    taskdef: 'develop', state: 'yielded', live: false, questions: [],
  } }, { global: { stubs: {
    Chat: true, Terminal: true, RelatedRuns: true, RunMeta: true, ThreadLinks: true,
  } } })
  await flushPromises()

  const continueButton = wrapper.findAll('button').find(
    (button) => button.text().includes('continue this run'),
  )
  await continueButton.trigger('click')
  await flushPromises()
  expect(wrapper.text()).toContain('source has no final sign-out')

  const freshButton = wrapper.findAll('button').find(
    (button) => button.text().includes('continue fresh'),
  )
  await freshButton.trigger('click')
  await flushPromises()
  expect(resumeRun).toHaveBeenLastCalledWith(expect.any(Object), expect.objectContaining({
    allowUnsignedSource: 'a'.repeat(32),
  }))
})

it('cancels an unsigned continuation without losing direction or retrying', async () => {
  window.localStorage.setItem('lmer.run.tab', 'overview')
  const refusal = Object.assign(new Error('source has no final sign-out'), {
    code: 'unsigned_session', source: { uid: 'a'.repeat(32) },
  })
  resumeRun.mockRejectedValueOnce(refusal)
  const wrapper = mountPane(RunDetail, { run: {
    host: 'gitlab.example.com', project: 'acme/widget', slug: 'develop-1',
    taskdef: 'develop', state: 'yielded', live: false, questions: [],
  } }, { global: { stubs: {
    Chat: true, Terminal: true, RelatedRuns: true, RunMeta: true, ThreadLinks: true,
  } } })
  await flushPromises()

  const direction = wrapper.get('textarea')
  await direction.setValue('Wait for the prior execution')
  const continueButton = wrapper.findAll('button').find(
    (button) => button.text().includes('continue this run'),
  )
  await continueButton.trigger('click')
  await flushPromises()

  const cancelButton = wrapper.findAll('button').find(
    (button) => button.text().includes('cancel and wait'),
  )
  await cancelButton.trigger('click')
  await flushPromises()

  expect(resumeRun).toHaveBeenCalledTimes(1)
  expect(direction.element.value).toBe('Wait for the prior execution')
  expect(wrapper.text()).not.toContain('source has no final sign-out')
})

it('scrolls to and focuses the open question when the shell asks for it', async () => {
  const question = { id: 'q1', kind: 'question', text: 'Ship it?', options: ['yes', 'no'], answered: false, closed: false }
  fetchSessionAsk.mockResolvedValue({ entries: [question] })
  const scrolled = vi.fn()
  Element.prototype.scrollIntoView = scrolled
  const run = { host: 'gitlab.example.com', project: 'acme/widget', slug: 'uber', orchestrator: true,
    last_session_id: 'uber-session', state: 'waiting_on_you', live: true, questions: [question] }
  const stubs = { Chat: true, Terminal: true, RelatedRuns: true, RunMeta: true, ThreadLinks: true }
  const quiet = mountPane(RunDetail, { run }, { global: { stubs } })
  await flushPromises()
  expect(scrolled).not.toHaveBeenCalled()
  quiet.unmount()
  const wrapper = mountPane(RunDetail, { run, focusQuestions: 1 }, { global: { stubs } })
  await flushPromises()
  await vi.dynamicImportSettled()
  await flushPromises()
  expect(wrapper.text()).toContain('Ship it?')
  expect(scrolled).toHaveBeenCalledTimes(1)
  expect(document.activeElement.tagName).toBe('TEXTAREA')
  expect(wrapper.element.contains(document.activeElement)).toBe(true)
  document.activeElement.blur()
  await wrapper.setProps({ focusQuestions: 2 })
  await flushPromises()
  expect(scrolled).toHaveBeenCalledTimes(2)
  expect(document.activeElement.tagName).toBe('TEXTAREA')
  delete Element.prototype.scrollIntoView
})

it('focuses the reply box, not a code-copy button, and waits for a question the fleet poll has not carried yet', async () => {
  document.activeElement?.blur?.()
  const question = { id: 'q1', kind: 'question', text: 'Run this?\n\n```\nmake deploy\n```', options: [], answered: false, closed: false }
  const pending = deferred()
  fetchSessionAsk.mockReturnValue(pending.promise)
  Element.prototype.scrollIntoView = vi.fn()
  const run = { host: 'gitlab.example.com', project: 'acme/widget', slug: 'uber-late', orchestrator: true,
    last_session_id: 'uber-late-session', state: 'in-progress', live: true, questions: [] }
  const wrapper = mountPane(RunDetail, { run, focusQuestions: 1 }, { global: { stubs: {
    Chat: true, Terminal: true, RelatedRuns: true, RunMeta: true, ThreadLinks: true,
  } } })
  await flushPromises()
  expect(document.activeElement.tagName).not.toBe('TEXTAREA')
  pending.resolve({ entries: [question] })
  await flushPromises()
  await vi.dynamicImportSettled()
  await flushPromises()
  expect(wrapper.text()).toContain('make deploy')
  expect(document.activeElement.tagName).toBe('TEXTAREA')
  expect(wrapper.element.contains(document.activeElement)).toBe(true)
  delete Element.prototype.scrollIntoView
})

it('gives up a focus request that no question answers within a poll or two', async () => {
  document.activeElement?.blur?.()
  vi.useFakeTimers({ toFake: ['Date'] })
  const pending = deferred()
  fetchSessionAsk.mockReturnValue(pending.promise)
  Element.prototype.scrollIntoView = vi.fn()
  const run = { host: 'gitlab.example.com', project: 'acme/widget', slug: 'uber-much-later', orchestrator: true,
    last_session_id: 'uber-much-later-session', state: 'in-progress', live: true, questions: [] }
  mountPane(RunDetail, { run, focusQuestions: 1 }, { global: { stubs: {
    Chat: true, Terminal: true, RelatedRuns: true, RunMeta: true, ThreadLinks: true,
  } } })
  await flushPromises()
  vi.setSystemTime(Date.now() + 60000)
  pending.resolve({ entries: [{ id: 'q9', kind: 'question', text: 'Much later?', options: [], answered: false, closed: false }] })
  await flushPromises()
  await vi.dynamicImportSettled()
  await flushPromises()
  expect(document.activeElement.tagName).not.toBe('TEXTAREA')
  vi.useRealTimers()
  delete Element.prototype.scrollIntoView
})
