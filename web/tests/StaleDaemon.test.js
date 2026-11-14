import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import StaleDaemon from '../src/components/StaleDaemon.vue'
import { daemonOutdated } from '../src/daemon.js'
import { mountPane } from './mount.js'

let wrapper
const reply = (body) => ({ ok: true, json: async () => body })

beforeEach(() => {
  vi.useFakeTimers()
  vi.stubGlobal('fetch', vi.fn())
})
afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

it.each([
  [{ ok: true, ui: { stale: false } }, false],
  [{ ok: true, ui: { stale: true } }, true],
  // A daemon from before the field existed (#650's own case).
  [{ ok: true }, true],
  [null, false],
])('daemonOutdated(%j) is %s', (health, expected) => {
  expect(daemonOutdated(health)).toBe(expected)
})

it('warns while the daemon is stale and clears once it is restarted', async () => {
  fetch.mockResolvedValue(reply({ ok: true, ui: { stale: true } }))
  wrapper = mountPane(StaleDaemon, {})
  await flushPromises()
  expect(fetch).toHaveBeenCalledWith('api/health', { credentials: 'same-origin' })
  expect(wrapper.get('.stale-daemon').text()).toContain('Restart the daemon')
  fetch.mockResolvedValue(reply({ ok: true, ui: { stale: false } }))
  await vi.advanceTimersByTimeAsync(30000)
  await flushPromises()
  expect(wrapper.find('.stale-daemon').exists()).toBe(false)
})

it('warns about a daemon too old to report its UI at all', async () => {
  fetch.mockResolvedValue(reply({ ok: true, schema: 1 }))
  wrapper = mountPane(StaleDaemon, {})
  await flushPromises()
  expect(wrapper.find('.stale-daemon').exists()).toBe(true)
})

it('says nothing when health cannot be read', async () => {
  fetch.mockRejectedValue(new Error('offline'))
  wrapper = mountPane(StaleDaemon, {})
  await flushPromises()
  expect(wrapper.find('.stale-daemon').exists()).toBe(false)
})
