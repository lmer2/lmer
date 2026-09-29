import { beforeEach, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import { nextTick } from 'vue'
import ThreadLinks from '../src/components/ThreadLinks.vue'
import { addSessionReference, connectSessionThread, disconnectSessionThread, openSessionThread } from '../src/api.js'
import { deferred, mountPane } from './mount.js'

vi.mock('../src/api.js', () => ({
  addSessionReference: vi.fn(), connectSessionThread: vi.fn(),
  disconnectSessionThread: vi.fn(), openSessionThread: vi.fn(), removeSessionReference: vi.fn(),
}))

const thread = {
  id: 'slack:one', channel: 'slack', label: 'Design thread', running: true,
  permalink: 'https://example.slack.com/archives/C123/p1234567890123456',
}
beforeEach(() => { vi.clearAllMocks() })

async function openMenu(wrapper) {
  await flushPromises()
  await wrapper.get('[aria-label="Slack threads"]').trigger('click')
  await vi.waitFor(() => expect(document.querySelector('.v-list-item')).toBeTruthy())
}

function clickText(selector, text) {
  const element = [...document.querySelectorAll(selector)].find((item) => item.textContent.trim() === text)
  expect(element, text).toBeTruthy()
  element.click()
  return nextTick()
}

it('shows disconnecting before the request finishes and holds it until the next fleet state', async () => {
  const request = deferred()
  disconnectSessionThread.mockReturnValue(request.promise)
  const wrapper = mountPane(ThreadLinks, { sessionId: 's-1', live: true, threads: [thread] })
  await openMenu(wrapper)
  await clickText('.v-list-item', 'Disconnect Design thread')
  expect(disconnectSessionThread).toHaveBeenCalledWith('s-1', thread.id, undefined)
  expect(document.body.textContent).toContain('disconnecting…')
  expect(document.querySelector('[data-thread-action]').getAttribute('aria-disabled')).toBe('true')
  document.querySelector('[data-thread-action]').click()
  await nextTick()
  expect(disconnectSessionThread).toHaveBeenCalledTimes(1)
  request.resolve({})
  await flushPromises()
  expect(wrapper.emitted('changed')).toHaveLength(1)
  expect(document.body.textContent).toContain('disconnecting…')
  expect(document.querySelector('[data-thread-action]').getAttribute('aria-disabled')).toBe('true')
  await wrapper.setProps({ threads: [{ ...thread }] })
  expect(document.body.textContent).toContain('disconnecting…')
  await wrapper.setProps({ threads: [] })
  expect(document.body.textContent).not.toContain('disconnecting…')
  expect(wrapper.get('[aria-label="connect a Slack thread"]').exists()).toBe(true)
})

it('reverts a refused disconnect and shows the daemon error', async () => {
  disconnectSessionThread.mockRejectedValue(new Error('The daemon refused this disconnect'))
  const wrapper = mountPane(ThreadLinks, { sessionId: 's-1', live: true, threads: [thread] })
  await openMenu(wrapper)
  await clickText('.v-list-item', 'Disconnect Design thread')
  await flushPromises()
  expect(document.body.textContent).toContain('The daemon refused this disconnect')
  expect(document.body.textContent).not.toContain('disconnecting…')
  expect(wrapper.emitted('changed')).toBeUndefined()
})

it.each([false, true])('holds connecting until a matching fleet entry arrives (open=%s)', async (opening) => {
  const request = deferred()
  const api = opening ? openSessionThread : connectSessionThread
  api.mockReturnValue(request.promise)
  const wrapper = mountPane(ThreadLinks, { sessionId: 's-1', live: true, threads: [] })
  await wrapper.get('[aria-label="connect a Slack thread"]').trigger('click')
  await flushPromises()
  const input = document.querySelector('input')
  input.value = opening ? '#design' : thread.permalink
  input.dispatchEvent(new Event('input', { bubbles: true }))
  await nextTick()
  await clickText('button', opening ? 'open' : 'connect')
  expect(api).toHaveBeenCalled()
  expect(document.body.textContent).toContain('connecting…')
  request.resolve({ thread })
  await flushPromises()
  expect(wrapper.emitted('changed')).toHaveLength(1)
  expect(document.body.textContent).toContain('connecting…')
  await wrapper.setProps({ threads: [] })
  expect(document.body.textContent).toContain('connecting…')
  await wrapper.setProps({ threads: [thread] })
  expect(document.body.textContent).not.toContain('connecting…')
})

it('restores a refused connect and keeps the daemon error in the dialog', async () => {
  connectSessionThread.mockRejectedValue(new Error('Thread is held by another session'))
  const wrapper = mountPane(ThreadLinks, { sessionId: 's-1', live: true })
  await wrapper.get('[aria-label="connect a Slack thread"]').trigger('click')
  await flushPromises()
  const input = document.querySelector('input')
  input.value = thread.permalink
  input.dispatchEvent(new Event('input', { bubbles: true }))
  await nextTick()
  await clickText('button', 'connect')
  await flushPromises()
  expect(document.body.textContent).toContain('Thread is held by another session')
  expect(document.body.textContent).not.toContain('connecting…')
  expect(input.disabled).toBe(false)
  expect(wrapper.emitted('changed')).toBeUndefined()
})

it('allows a reference save while an accepted connect awaits fleet confirmation', async () => {
  connectSessionThread.mockResolvedValue({ thread })
  addSessionReference.mockResolvedValue({})
  const wrapper = mountPane(ThreadLinks, { sessionId: 's-1', live: true })
  await wrapper.get('[aria-label="connect a Slack thread"]').trigger('click')
  await flushPromises()
  let input = document.querySelector('input')
  input.value = thread.permalink
  input.dispatchEvent(new Event('input', { bubbles: true }))
  await nextTick()
  await clickText('button', 'connect')
  await flushPromises()
  expect(document.body.textContent).toContain('connecting…')
  await wrapper.setProps({ references: [] })
  await openMenu(wrapper)
  await clickText('.v-list-item', 'Add a thread as reference')
  await flushPromises()
  input = document.querySelector('.v-dialog input')
  expect(input.disabled).toBe(false)
  input.value = thread.permalink
  input.dispatchEvent(new Event('input', { bubbles: true }))
  await nextTick()
  await clickText('button', 'save')
  await flushPromises()
  expect(addSessionReference).toHaveBeenCalledWith('s-1', thread.permalink, '', undefined)
  expect(document.body.textContent).toContain('connecting…')
})

it.each(['resolve', 'reject'])('ignores an old session disconnect %s after switching sessions', async (outcome) => {
  const request = deferred()
  disconnectSessionThread.mockReturnValue(request.promise)
  const wrapper = mountPane(ThreadLinks, { sessionId: 's-1', live: true, threads: [thread] })
  await openMenu(wrapper)
  await clickText('.v-list-item', 'Disconnect Design thread')
  await wrapper.setProps({ sessionId: 's-2', threads: [] })
  request[outcome](outcome === 'reject' ? new Error('Old refusal') : {})
  await flushPromises()
  expect(wrapper.emitted('changed')).toBeUndefined()
  expect(document.body.textContent).not.toContain('disconnecting…')
  expect(document.body.textContent).not.toContain('Old refusal')
})

it('accepts a confirming fleet poll that arrives before the disconnect response', async () => {
  const request = deferred()
  disconnectSessionThread.mockReturnValue(request.promise)
  const wrapper = mountPane(ThreadLinks, { sessionId: 's-1', live: true, threads: [thread] })
  await openMenu(wrapper)
  await clickText('.v-list-item', 'Disconnect Design thread')
  await wrapper.setProps({ threads: [] })
  expect(document.body.textContent).toContain('disconnecting…')
  request.resolve({})
  await flushPromises()
  expect(document.body.textContent).not.toContain('disconnecting…')
})
