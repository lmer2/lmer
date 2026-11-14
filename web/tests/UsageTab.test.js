// The run view's usage section (issue 315, spec §5.8): session → model/agent rows →
// buckets, beside the state of the record each session saved.
//
// Run with `npm test` from web/ (see web/docs/TESTING.md).

import { beforeEach, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import UsageTab from '../src/components/UsageTab.vue'
import { fetchRunUsage } from '../src/api.js'
import { mountPane } from './mount.js'

vi.mock('../src/api.js', () => ({ fetchRunUsage: vi.fn() }))

const NOW = Date.parse('2026-11-05T12:00:00Z')
const at = (minutes) => new Date(NOW + minutes * 60000).toISOString()
const run = { host: 'forge.test', project: 'acme/widget', slug: 'develop-1', uid: null }
const buckets = (output) => ({
  requests: 4, input: 1000, cache_create: 2000, cache_read: 50000, output, thinking: 300,
})
const row = (overrides) => ({
  harness: 'claude', model: 'model-a', agent: null, status: 'collected', ...buckets(5000), ...overrides,
})

function validRecord(overrides) {
  return {
    valid: true, url: 'https://forge.test/work/-/blob/main/runs/develop-1/ai-usage/s-1.yaml',
    updated_at: at(-10), final: false, partial: false, status: 'running',
    totals: buckets(5000), models: [row({})], started_at: at(-90), ended_at: null,
    active_seconds: 4800, harness: 'claude', harness_reported_cost: null, cost: null,
    ...overrides,
  }
}

function live(overrides) {
  return {
    status: 'collected', reason: null, totals: buckets(9000), burn: buckets(100),
    models: [row({})], started_at: at(-90), ended_at: at(-5), active_seconds: 5100,
    live: false, harness_reported_cost: {}, cost: null, ...overrides,
  }
}

function reply(sessions) {
  return {
    run: { host: run.host, project: run.project, slug: run.slug }, present: true,
    invalid_records: 0, priced: false, table_hash: null, prices_problem: null, sessions,
  }
}

async function mountTab(answer) {
  fetchRunUsage.mockResolvedValue(answer)
  const wrapper = mountPane(UsageTab, { run, now: NOW })
  await flushPromises()
  return wrapper
}

function session(wrapper, id) {
  return wrapper.findAll('.usage-session').find((section) => section.text().includes(id))
}

beforeEach(() => {
  fetchRunUsage.mockReset()
})

it('reads the run once, by its key, when it opens', async () => {
  await mountTab(reply([]))
  expect(fetchRunUsage).toHaveBeenCalledTimes(1)
  expect(fetchRunUsage).toHaveBeenCalledWith(run)
})

it('shows how far an unfinalised record is behind the live read', async () => {
  const wrapper = await mountTab(reply([{
    session: 's-killed', record: validRecord({}), live: live({}),
    record_behind: { behind: buckets(4000), unpriced_weight_behind: 4321 },
  }]))
  const state = session(wrapper, 's-killed').get('.usage-record-state')
  expect(state.text()).toContain('saved 10m ago')
  expect(state.text()).toContain('unfinalised: 4,321 tokens behind')
  expect(state.text()).not.toContain('final ·')
})

it('says final for a finalised record, and links the record in the forge', async () => {
  const record = validRecord({ final: true, status: 'ended', ended_at: at(-5) })
  const wrapper = await mountTab(reply([{ session: 's-done', record, live: null, record_behind: null }]))
  const section = session(wrapper, 's-done')
  expect(section.get('.usage-record-state').text()).toBe('saved 10m ago · final · ended')
  expect(section.get('.usage-record-link').attributes('href')).toBe(record.url)
  // No live read, so the rows and the wall clock are the record's.
  expect(section.text()).toContain('active 1h20m')
  // format.js's ago(), so 90 minutes rounds the way every other age on the page does.
  expect(section.text()).toContain('started 2h ago')
  expect(section.text()).toContain('ended 5m ago')
  expect(section.text()).toContain('from the saved record')
})

it('shows an invalid record with its errors', async () => {
  const wrapper = await mountTab(reply([{
    session: 's-bad',
    record: { valid: false, errors: ['totals do not equal rows', 'unknown version 9'], url: null },
    live: null, record_behind: null,
  }]))
  const section = session(wrapper, 's-bad')
  expect(section.get('.usage-record-state').text()).toBe('invalid record')
  const errors = section.findAll('.usage-record-errors li').map((item) => item.text())
  expect(errors).toEqual(['totals do not equal rows', 'unknown version 9'])
  expect(section.find('.usage-rows').exists()).toBe(false)
})

it('organises a session into model and agent rows by bucket, and marks fan-out children', async () => {
  const wrapper = await mountTab(reply([{
    session: 's-fan', record: null, record_behind: null,
    live: live({ models: [
      row({ model: 'model-a', output: 7000 }),
      row({ harness: 'codex', model: 'model-b', agent: 'reviewer', child_session: 's-child-1', output: 2000 }),
    ] }),
  }]))
  const rows = session(wrapper, 's-fan').findAll('.usage-rows tbody tr')
  expect(rows).toHaveLength(2)
  expect(rows[0].classes()).not.toContain('usage-child')
  expect(rows[0].find('.usage-child-mark').exists()).toBe(false)
  expect(rows[0].text()).toContain('claude · model-a')
  expect(rows[0].text()).toContain('7k')
  expect(rows[1].classes()).toContain('usage-child')
  const mark = rows[1].get('.usage-child-mark')
  expect(mark.text()).toBe('child')
  expect(mark.attributes('title')).toContain('s-child-1')
  expect(rows[1].text()).toContain('codex · model-b · reviewer')
  const headers = session(wrapper, 's-fan').findAll('.usage-rows th').map((cell) => cell.text())
  expect(headers).toEqual(['model', 'input', 'cache write', 'output', 'thinking', 'cache read', 'requests'])
  expect(session(wrapper, 's-fan').get('.usage-record-state').text()).toBe('no saved record')
})

it('keeps the harness-reported cost apart from the estimated cost', async () => {
  const cost = { currency: 'USD', total: 1.5, partial: false, table_hash: 'sha256:feed', unpriced_models: [] }
  const wrapper = await mountTab(reply([{
    session: 's-pi', record: validRecord({ harness: 'pi', harness_reported_cost: [{ currency: 'USD', total: 0.42 }] }),
    live: live({ harness_reported_cost: { USD: 0.42 }, cost }), record_behind: null,
  }]))
  const section = session(wrapper, 's-pi')
  const reported = section.get('.usage-harness-reported')
  const estimated = section.get('.usage-cost')
  expect(reported.text()).toBe('harness-reported USD 0.42')
  expect(estimated.text()).toBe('estimated cost USD 1.50')
  expect(estimated.attributes('title')).toContain('sha256:feed')
  expect(estimated.text()).not.toContain('0.42')
  expect(reported.text()).not.toContain('1.50')
})

it('shows no cost at all when nothing is priced', async () => {
  const wrapper = await mountTab(reply([{ session: 's-1', record: validRecord({}), live: null, record_behind: null }]))
  expect(wrapper.find('.usage-cost').exists()).toBe(false)
  expect(wrapper.find('.usage-harness-reported').exists()).toBe(false)
})

it('says a failed read failed rather than showing an empty run', async () => {
  fetchRunUsage.mockRejectedValue(new Error('HTTP 500'))
  const wrapper = mountPane(UsageTab, { run, now: NOW })
  await flushPromises()
  expect(wrapper.text()).toContain('HTTP 500')
  expect(wrapper.text()).not.toContain('No usage has been collected')
})
