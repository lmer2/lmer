// The per-project usage rollup (issue 315, spec §5.8): one row per period from the
// saved records, a provisional line when this host's live numbers run ahead of
// them, and a refetch whenever the selection changes.
//
// Run with `npm test` from web/ (see web/docs/TESTING.md).

import { beforeEach, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import UsageRollup from '../src/components/UsageRollup.vue'
import { fetchUsageRollup } from '../src/api.js'
import { mountPane } from './mount.js'

vi.mock('../src/api.js', () => ({ fetchUsageRollup: vi.fn() }))

const runs = [
  { host: 'forge.test', project: 'acme/widget', slug: 'develop-1' },
  { host: 'forge.test', project: 'acme/widget', slug: 'review-2' },
  { host: 'forge.test', project: 'acme/gadget', slug: 'develop-3' },
  // A session that has not committed a run identity yet names no repository.
  { host: '—', project: '—', slug: 'pending' },
]
const buckets = (output) => ({
  requests: 9, input: 1200, cache_create: 3400, cache_read: 77000, output, thinking: 500,
})

function period(name, output, overrides = {}) {
  return {
    period: name, totals: buckets(output), by_model: [], by_harness: [], by_taskdef: [],
    top_runs: [], cost: null, provisional: null, ...overrides,
  }
}

function reply(overrides = {}) {
  return {
    project: 'forge.test/acme/gadget', period: 'day', from: '2026-11-01', to: '2026-11-05',
    periods: [
      period('2026-11-04', 21000),
      period('2026-11-05', 8000, {
        provisional: buckets(1500),
        cost: { currency: 'USD', total: 3.2, partial: false, table_hash: 'sha256:aa', unpriced_models: [] },
      }),
    ],
    records: 6, invalid_records: 1, priced: true, table_hash: 'sha256:aa', mirror_sha: 'abc123',
    prices_problem: null, ...overrides,
  }
}

async function mountRollup() {
  const wrapper = mountPane(UsageRollup, { runs })
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  fetchUsageRollup.mockReset()
  fetchUsageRollup.mockResolvedValue(reply())
})

it('reads the first repository the fleet is against, by day', async () => {
  await mountRollup()
  expect(fetchUsageRollup).toHaveBeenCalledTimes(1)
  expect(fetchUsageRollup).toHaveBeenCalledWith({
    project: 'forge.test/acme/gadget', period: 'day', from: '', to: '',
  })
})

it('renders one row per period with its totals and cost', async () => {
  const wrapper = await mountRollup()
  const rows = wrapper.findAll('tr.usage-period')
  expect(rows.map((row) => row.find('td').text())).toEqual(['2026-11-04', '2026-11-05'])
  expect(rows[0].text()).toContain('21k')
  expect(rows[0].text()).toContain('77k')
  expect(rows[1].text()).toContain('USD 3.20')
  expect(wrapper.text()).toContain('6 record(s)')
  expect(wrapper.text()).toContain('1 invalid and not counted')
})

it('labels the provisional numbers and keeps them out of the period row', async () => {
  const wrapper = await mountRollup()
  const provisional = wrapper.findAll('tr.usage-provisional')
  expect(provisional).toHaveLength(1)
  expect(provisional[0].text()).toContain('provisional')
  expect(provisional[0].text()).toContain('1.5k')
  const day = wrapper.findAll('tr.usage-period')[1]
  expect(day.text()).toContain('8k')
  expect(day.text()).not.toContain('1.5k')
})

it('refetches when the period changes', async () => {
  const wrapper = await mountRollup()
  fetchUsageRollup.mockResolvedValue(reply({ period: 'week', periods: [period('2026-W45', 99000)] }))
  await wrapper.findAll('button').find((button) => button.text() === 'week').trigger('click')
  await flushPromises()
  expect(fetchUsageRollup).toHaveBeenCalledTimes(2)
  expect(fetchUsageRollup).toHaveBeenLastCalledWith({
    project: 'forge.test/acme/gadget', period: 'week', from: '', to: '',
  })
  expect(wrapper.findAll('tr.usage-period').map((row) => row.find('td').text())).toEqual(['2026-W45'])
})

it('refetches with a from date when one is set', async () => {
  const wrapper = await mountRollup()
  const [from] = wrapper.findAll('input[type="date"]')
  await from.setValue('2026-11-02')
  await flushPromises()
  expect(fetchUsageRollup).toHaveBeenLastCalledWith({
    project: 'forge.test/acme/gadget', period: 'day', from: '2026-11-02', to: '',
  })
})

it('renders nothing when no run names a repository', async () => {
  const wrapper = mountPane(UsageRollup, { runs: [runs[3]] })
  await flushPromises()
  expect(wrapper.find('.usage-rollup').exists()).toBe(false)
  expect(fetchUsageRollup).not.toHaveBeenCalled()
})

it('stays away from the landing screen when the first read fails', async () => {
  fetchUsageRollup.mockRejectedValue(Object.assign(new Error('Not Found'), { status: 404 }))
  const wrapper = await mountRollup()
  expect(fetchUsageRollup).toHaveBeenCalledTimes(1)
  expect(wrapper.find('.usage-rollup').exists()).toBe(false)
  expect(wrapper.find('.v-alert').exists()).toBe(false)
})

it('says quietly that a refresh failed and keeps the table it had', async () => {
  const wrapper = await mountRollup()
  fetchUsageRollup.mockRejectedValue(new Error('from must be on or before to'))
  await wrapper.findAll('button').find((button) => button.text() === 'month').trigger('click')
  await flushPromises()
  expect(wrapper.get('.usage-rollup-error').text()).toBe('could not refresh: from must be on or before to')
  expect(wrapper.find('.v-alert').exists()).toBe(false)
  expect(wrapper.findAll('tr.usage-period')).toHaveLength(2)
})
