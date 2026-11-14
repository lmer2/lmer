// The fleet header's usage strip (issue 315, spec §5.8): window totals by bucket, the
// cost when priced, and one headroom chip per (harness, operator, window).
//
// Run with `npm test` from web/ (see web/docs/TESTING.md).

import { beforeEach, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import UsageStrip from '../src/components/UsageStrip.vue'
import { fetchUsage } from '../src/api.js'
import { mountPane } from './mount.js'

vi.mock('../src/api.js', () => ({ fetchUsage: vi.fn() }))

const NOW = Date.parse('2026-11-05T12:00:00Z')
const at = (minutes) => new Date(NOW + minutes * 60000).toISOString()
const buckets = (output) => ({
  requests: 12, input: 3400, cache_create: 5600, cache_read: 980000, output, thinking: 2000,
})

function headroom(overrides) {
  return {
    harness: 'claude', operator: 'alice', window: 'five_hour', approximate: true,
    percent: 43, resets_at: at(130), age_seconds: 60, stale: false,
    rate_pct_per_hour: 6.5, projected_full_at: null, fills_before_reset: false,
    readings: 7, reason: null, ...overrides,
  }
}

function reply(overrides = {}) {
  return {
    window: '1h', generated_at: at(0), totals: buckets(42000),
    by_harness: [], by_model: [], cost: null, ranking: 'unpriced_token_weight',
    top_runs: [], sessions: { collected: 3, not_collected: 2 },
    headroom: [headroom({})], collector: { last_tick_at: at(0), last_error: null },
    enabled: true, prices: { configured: false, problem: null, table_hash: null },
    ...overrides,
  }
}

async function mountStrip(answer) {
  fetchUsage.mockResolvedValue(answer)
  const wrapper = mountPane(UsageStrip, { now: NOW })
  await flushPromises()
  return wrapper
}

function chips(wrapper) {
  return wrapper.findAllComponents({ name: 'VChip' })
}

beforeEach(() => {
  fetchUsage.mockReset()
})

it('shows the window totals by bucket, cache reads apart, and the not-collected count', async () => {
  const wrapper = await mountStrip(reply())
  expect(fetchUsage).toHaveBeenCalledWith('1h')
  const text = wrapper.text()
  expect(text).toContain('input 3.4k')
  expect(text).toContain('cache write 5.6k')
  expect(text).toContain('output 42k')
  expect(text).toContain('of which thinking 2k')
  expect(text).toContain('cache read 980k')
  expect(text).toContain('2 not collected')
  expect(text).not.toMatch(/total tokens/)
})

it('reads a headroom chip as harness · operator · window percent · reset', async () => {
  const wrapper = await mountStrip(reply())
  const [chip] = chips(wrapper)
  expect(chip.text()).toBe('claude · alice · 5h 43% · resets in 2h10m')
  expect(chip.props('color')).toBeUndefined()
  expect(chip.classes()).not.toContain('usage-stale')
  expect(chip.attributes('title')).toContain('rising 6.5%/h over 7 readings')
  expect(chip.attributes('title')).toContain('approximation of the account')
})

it('names an unknown window by its own key and seven_day as 7d', async () => {
  const wrapper = await mountStrip(reply({ headroom: [
    headroom({ harness: 'codex', window: 'seven_day' }),
    headroom({ harness: 'codex', window: 'codex:primary' }),
  ] }))
  const [week, other] = chips(wrapper)
  expect(week.text()).toContain('codex · alice · 7d 43%')
  expect(other.text()).toContain('codex · alice · codex:primary 43%')
})

it('fades a stale chip', async () => {
  const wrapper = await mountStrip(reply({ headroom: [headroom({ stale: true, age_seconds: 3600 })] }))
  const [chip] = chips(wrapper)
  expect(chip.classes()).toContain('usage-stale')
  expect(chip.attributes('title')).toContain('stale')
})

it('warns at 80% and over, and when the window fills before it resets', async () => {
  const wrapper = await mountStrip(reply({ headroom: [
    headroom({ percent: 79 }),
    headroom({ operator: 'bob', percent: 80 }),
    headroom({ operator: 'carol', percent: 55, fills_before_reset: true, projected_full_at: at(40) }),
  ] }))
  const [calm, full, filling] = chips(wrapper)
  expect(calm.props('color')).toBeUndefined()
  expect(full.props('color')).toBe('warning')
  expect(filling.props('color')).toBe('warning')
  expect(filling.text()).toContain('at current burn fills in ~40m, before reset in 2h10m')
  expect(full.text()).not.toContain('at current burn')
})

it('says why there is no rate when the rate is null', async () => {
  const wrapper = await mountStrip(reply({ headroom: [
    headroom({ rate_pct_per_hour: null, readings: 1, reason: 'too few readings' }),
  ] }))
  const title = chips(wrapper)[0].attributes('title')
  expect(title).toContain('no rate: too few readings')
  expect(title).not.toContain('%/h')
  expect(title).toContain('approximation of the account')
})

it('hides the cost when it is null', async () => {
  const wrapper = await mountStrip(reply({ cost: null }))
  expect(wrapper.find('.usage-cost').exists()).toBe(false)
  expect(wrapper.text()).not.toContain('cost')
})

it('shows a priced cost, says partial, and names the price table', async () => {
  const wrapper = await mountStrip(reply({ cost: {
    currency: 'USD', total: 12.345, partial: true, table_hash: 'sha256:abc', unpriced_models: ['odd-model'],
  } }))
  const cost = wrapper.get('.usage-cost')
  expect(cost.text()).toBe('cost USD 12.35 partial')
  expect(cost.attributes('title')).toContain('sha256:abc')
  expect(cost.attributes('title')).toContain('odd-model')
})

it('renders nothing when usage is switched off', async () => {
  const wrapper = await mountStrip(reply({ enabled: false }))
  expect(wrapper.find('.usage-strip').exists()).toBe(false)
  expect(wrapper.text()).toBe('')
})

it('renders nothing for a daemon that has no usage route', async () => {
  fetchUsage.mockRejectedValue(Object.assign(new Error('Not Found'), { status: 404 }))
  const wrapper = mountPane(UsageStrip, { now: NOW })
  await flushPromises()
  expect(wrapper.find('.usage-strip').exists()).toBe(false)
})

it('notes a collector error quietly', async () => {
  const wrapper = await mountStrip(reply({ collector: { last_tick_at: at(-5), last_error: 'cache unreadable' } }))
  const note = wrapper.get('.usage-collector-error')
  expect(note.text()).toContain('cache unreadable')
  expect(note.classes()).toContain('text-medium-emphasis')
})

it('refetches for the window the operator picks', async () => {
  const wrapper = await mountStrip(reply())
  fetchUsage.mockResolvedValue(reply({ window: '24h', totals: buckets(900000) }))
  await wrapper.findAll('button').find((button) => button.text() === '24h').trigger('click')
  await flushPromises()
  expect(fetchUsage).toHaveBeenLastCalledWith('24h')
  expect(wrapper.text()).toContain('output 900k')
})

it('refreshes on its own timer and no faster than every 10 seconds', async () => {
  vi.useFakeTimers()
  fetchUsage.mockResolvedValue(reply())
  mountPane(UsageStrip, { now: NOW })
  await flushPromises()
  expect(fetchUsage).toHaveBeenCalledTimes(1)
  await vi.advanceTimersByTimeAsync(10000)
  expect(fetchUsage).toHaveBeenCalledTimes(1)
  await vi.advanceTimersByTimeAsync(30000)
  expect(fetchUsage.mock.calls.length).toBeGreaterThan(1)
})
