// Issue 646 item 2: the header's ask-every-assistant-to-rotate modal.
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import RotateAllAssistants from '../src/components/RotateAllAssistants.vue'
import { fetchRotateAll, requestRotateAll } from '../src/api.js'
import { mountPane } from './mount.js'

vi.mock('../src/api.js', () => ({ fetchRotateAll: vi.fn(), requestRotateAll: vi.fn() }))

const target = (id, name, generation, running = true) => ({
  workspace_id: id, project_name: name, session_id: running ? `s-${id}` : null,
  generation, lifecycle: running ? 'running' : 'stopped', running,
})
const listing = (alpha = 1, beta = 1) => ({
  targets: [target('alpha', 'Alpha', alpha), target('beta', 'Beta', beta), target('general', 'General', 0, false)],
  reason_limit: 500,
})
let wrapper

beforeEach(() => {
  vi.clearAllMocks()
  fetchRotateAll.mockResolvedValue(listing())
  requestRotateAll.mockImplementation(async (reason) => ({
    request: 'abcd1234', reason: reason || null,
    targets: listing().targets.map((row) => ({
      ...row, outcome: row.running ? 'requested' : 'not_running', message_id: row.running ? `m-${row.workspace_id}` : null,
      detail: null,
    })),
  }))
})

afterEach(() => {
  wrapper?.unmount()
  document.body.innerHTML = ''
  vi.useRealTimers()
})

const dialog = () => document.querySelector('.rotate-all-dialog')
const action = (label) => [...dialog().querySelectorAll('button')].find((node) => node.textContent.trim() === label)

async function openModal() {
  wrapper = mountPane(RotateAllAssistants, {})
  await wrapper.get('button.rotate-all').trigger('click')
  await flushPromises()
}

it('is a labelled header button that lists the instances it will reach', async () => {
  await openModal()
  expect(wrapper.get('button.rotate-all').attributes('aria-label')).toBe('Ask every Ratchet to rotate')
  const text = dialog().textContent
  expect(text).toContain('Alpha · generation 1')
  expect(text).toContain('Beta · generation 1')
  expect(text).toContain('General · not running, not asked')
  expect(action('ask 2 to rotate')).toBeDefined()
})

it('sends the reason and shows each instance requested, then rotated once its generation moves', async () => {
  vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
  await openModal()
  const field = dialog().querySelector('textarea')
  field.value = 'new standing orders'
  field.dispatchEvent(new Event('input'))
  await flushPromises()
  action('ask 2 to rotate').click()
  await flushPromises()
  expect(requestRotateAll).toHaveBeenCalledWith('new standing orders')
  const states = () => Object.fromEntries([...dialog().querySelectorAll('[data-state]')]
    .map((row) => [row.textContent.trim().split(' ·')[0], row.dataset.state]))
  expect(states()).toEqual({ Alpha: 'pending', Beta: 'pending', General: 'not_running' })

  fetchRotateAll.mockResolvedValue(listing(2, 1))
  vi.advanceTimersByTime(5000)
  await flushPromises()
  expect(states()).toEqual({ Alpha: 'rotated', Beta: 'pending', General: 'not_running' })
  expect(dialog().textContent).toContain('to generation 2')

  fetchRotateAll.mockResolvedValue(listing(2, 2))
  vi.advanceTimersByTime(5000)
  await flushPromises()
  expect(states()).toEqual({ Alpha: 'rotated', Beta: 'rotated', General: 'not_running' })
  const calls = fetchRotateAll.mock.calls.length
  vi.advanceTimersByTime(15000)
  await flushPromises()
  expect(fetchRotateAll.mock.calls.length).toBe(calls)
})

it('says why an instance was not asked', async () => {
  requestRotateAll.mockResolvedValue({
    request: 'abcd1234', reason: null,
    targets: [{ ...target('alpha', 'Alpha', 1), outcome: 'not_reached', message_id: null, detail: 'this session has no message bus' }],
  })
  await openModal()
  action('ask 2 to rotate').click()
  await flushPromises()
  expect(requestRotateAll).toHaveBeenCalledWith('')
  expect(dialog().textContent).toContain('not reached')
  expect(dialog().textContent).toContain('no message bus')
})

it('cannot send when nothing is running', async () => {
  fetchRotateAll.mockResolvedValue({ targets: [target('general', 'General', 0, false)], reason_limit: 500 })
  await openModal()
  expect(action('ask 0 to rotate').disabled).toBe(true)
})

it('starts no poll when the dialog was closed before the request came back', async () => {
  vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
  let answer
  const reply = requestRotateAll.getMockImplementation()
  requestRotateAll.mockImplementation((reason) => new Promise((resolve) => { answer = () => resolve(reply(reason)) }))
  await openModal()
  action('ask 2 to rotate').click()
  await flushPromises()
  action('cancel').click()
  await flushPromises()
  answer()
  await flushPromises()
  const calls = fetchRotateAll.mock.calls.length
  vi.advanceTimersByTime(30000)
  await flushPromises()
  expect(fetchRotateAll.mock.calls.length).toBe(calls)
  expect(vi.getTimerCount()).toBe(0)
})

it('starts no poll when it was unmounted before the request came back', async () => {
  vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
  let answer
  const reply = requestRotateAll.getMockImplementation()
  requestRotateAll.mockImplementation((reason) => new Promise((resolve) => { answer = () => resolve(reply(reason)) }))
  await openModal()
  action('ask 2 to rotate').click()
  await flushPromises()
  wrapper.unmount()
  wrapper = null
  answer()
  await flushPromises()
  const calls = fetchRotateAll.mock.calls.length
  vi.advanceTimersByTime(30000)
  await flushPromises()
  expect(fetchRotateAll.mock.calls.length).toBe(calls)
  expect(vi.getTimerCount()).toBe(0)
})

it('is a labelled button in a page body when asked to be', async () => {
  wrapper = mountPane(RotateAllAssistants, { block: true })
  expect(wrapper.get('button.rotate-all').text()).toBe('Ask every Ratchet to rotate')
  await wrapper.get('button.rotate-all').trigger('click')
  await flushPromises()
  expect(dialog().textContent).toContain('Alpha · generation 1')
})
