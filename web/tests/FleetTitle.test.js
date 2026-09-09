import { beforeEach, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import App from '../src/App.vue'
import { fetchState } from '../src/api.js'
import { fleetTitle } from '../src/fleetTitle.js'
import { mountPane } from './mount.js'

vi.mock('../src/api.js', () => ({
  fetchState: vi.fn(), forgetRun: vi.fn(), prune: vi.fn(), rescan: vi.fn(),
}))

const state = (extra = {}) => ({ runs: [], attention: [], totals: { runs: 0, live: 0, attention: 0 }, ...extra })

beforeEach(() => {
  vi.clearAllMocks()
  window.localStorage.clear()
  window.location.hash = ''
  document.title = 'lmer'
})

it.each([
  [state(), false, '⚠ offline — lmer'],
  [state(), true, '· idle — lmer'],
  [state({ totals: { live: 2 } }), true, '▶ working — lmer'],
  [state({ totals: { live: 2, attention: 1 } }), true, '⏳ waiting — lmer'],
  [state({ attention: [{ slug: 'waiting' }], totals: { live: 1 } }), true, '⏳ waiting — lmer'],
  [state({ attention: [{}], totals: { live: 2 } }), false, '⚠ offline — lmer'],
])('uses fleet precedence for %j, online=%s', (payload, online, title) => {
  expect(fleetTitle(payload, online)).toBe(title)
})

it('updates the real tab title only when polled state changes and recovers after a failed poll', async () => {
  vi.useFakeTimers()
  fetchState.mockResolvedValue(state())
  const writes = vi.spyOn(document, 'title', 'set')
  mountPane(App)
  await flushPromises()
  expect(document.title).toBe('· idle — lmer')
  const count = writes.mock.calls.length
  await vi.advanceTimersByTimeAsync(10000)
  await flushPromises()
  expect(writes).toHaveBeenCalledTimes(count)
  fetchState.mockResolvedValue(state({ totals: { live: 2 } }))
  await vi.advanceTimersByTimeAsync(10000)
  await flushPromises()
  expect(document.title).toBe('▶ working — lmer')
  fetchState.mockRejectedValueOnce(new Error('Network unavailable'))
  await vi.advanceTimersByTimeAsync(10000)
  await flushPromises()
  expect(document.title).toBe('⚠ offline — lmer')
  await vi.advanceTimersByTimeAsync(10000)
  await flushPromises()
  expect(document.title).toBe('▶ working — lmer')
})
