import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import IngressStatus from '../src/components/IngressStatus.vue'
import { mountPane } from './mount.js'

const at = '2026-09-17T12:00:00Z'
let wrapper
const reply = (ingress) => ({ ok: true, json: async () => ({ ingress }) })
const snapshot = {
  slack: { up: true, state: 'up', last_event_at: at, last_error: null },
  matrix: { up: false, state: 'retrying', last_event_at: null, last_error: 'M_UNAVAILABLE' },
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(reply(snapshot)))
})
afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

it('renders transport health, last event and last error from the health API', async () => {
  wrapper = mountPane(IngressStatus, { now: Date.parse(at) + 10000 })
  await flushPromises()
  expect(fetch).toHaveBeenCalledWith('api/health', { credentials: 'same-origin' })
  expect(wrapper.text()).toContain('slack: Up')
  expect(wrapper.text()).toContain('matrix: Retrying')
  await wrapper.get('button').trigger('click')
  await flushPromises()
  expect(wrapper.text()).toContain('Last event: 10s ago')
  expect(wrapper.text()).toContain('None received')
  expect(wrapper.text()).toContain('M_UNAVAILABLE')
})

it('polls recovery and stops polling on unmount', async () => {
  wrapper = mountPane(IngressStatus, { now: Date.parse(at) })
  await flushPromises()
  fetch.mockResolvedValue(reply({ ...snapshot, matrix: { ...snapshot.matrix, up: true, state: 'up' } }))
  await vi.advanceTimersByTimeAsync(10000)
  await flushPromises()
  expect(wrapper.text()).toContain('matrix: Up')
  wrapper.unmount()
  wrapper = null
  const calls = fetch.mock.calls.length
  await vi.advanceTimersByTimeAsync(20000)
  expect(fetch).toHaveBeenCalledTimes(calls)
})

it('does not present stale health as current after a failed refresh', async () => {
  wrapper = mountPane(IngressStatus, { now: Date.parse(at) })
  await flushPromises()
  fetch.mockRejectedValue(new Error('offline'))
  await vi.advanceTimersByTimeAsync(10000)
  await flushPromises()
  expect(wrapper.text()).toContain('Status unavailable')
  await wrapper.get('button').trigger('click')
  await flushPromises()
  expect(wrapper.text()).toContain('Could not refresh ingress status: offline')
  expect(wrapper.text()).toContain('Unknown (last known status below)')
})
