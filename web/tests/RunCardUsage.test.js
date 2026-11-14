// The fleet card's burn reading and the "most expensive now" ranking (issue 315,
// spec §5.8, docs/USAGE-TRACKING.md "Ranking the expensive run").
//
// Run with `npm test` from web/ (see web/docs/TESTING.md).

import { describe, expect, it } from 'vitest'
import RunCard from '../src/components/RunCard.vue'
import { costLabel, mostExpensiveFirst, usageComparator, usageRanking } from '../src/usage.js'
import { mountPane } from './mount.js'

const stubs = { global: { stubs: { ThreadLinks: true, StatusStrip: true } } }
const buckets = (output, extra = {}) => ({
  requests: 3, input: 0, cache_create: 0, cache_read: 0, output, thinking: 0, ...extra,
})

function usage(overrides = {}) {
  return {
    totals: buckets(340000), burn: buckets(12000), models: ['model-a'],
    collected_at: '2026-11-05T12:00:00Z', status: 'collected', sessions: 1, ...overrides,
  }
}

function row(slug, rowUsage) {
  return {
    host: 'forge.test', project: 'acme/widget', slug, label: slug, state: 'running',
    live: true, attention: null, usage: rowUsage,
  }
}

const priced = (total) => ({ currency: 'USD', total, partial: false, table_hash: 'sha256:aa', unpriced_models: [] })

describe('the burn reading', () => {
  it('shows the last 15 minutes of output beside the run total', () => {
    const wrapper = mountPane(RunCard, { run: row('a', usage()) }, stubs)
    const burn = wrapper.get('.run-burn')
    expect(burn.text()).toBe('burn 12k out/15m · 340k out total')
    expect(burn.attributes('title')).toContain('last 15 minutes')
    expect(burn.attributes('title')).toContain('cache read')
  })

  it('is absent when the run has no usage', () => {
    const wrapper = mountPane(RunCard, { run: row('a', null) }, stubs)
    expect(wrapper.find('.run-burn').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('burn')
  })
})

describe('most expensive now', () => {
  it('orders by cost when every row with usage is priced', () => {
    const rows = [
      // Burning hard but cheap; cost wins when every row is priced.
      row('cheap', usage({ burn: buckets(900000), burn_cost: priced(1) })),
      row('dear', usage({ burn: buckets(10), burn_cost: priced(9) })),
      row('middle', usage({ burn: buckets(500), burn_cost: priced(4) })),
      row('unread', null),
    ]
    expect(usageRanking(rows)).toBe('cost')
    expect(mostExpensiveFirst(rows).map((each) => each.slug)).toEqual(['dear', 'middle', 'cheap', 'unread'])
    expect([...rows].sort(usageComparator(rows)).map((each) => each.slug))
      .toEqual(['dear', 'middle', 'cheap', 'unread'])
  })

  it('orders by output + cache writes + input when any row is unpriced', () => {
    const rows = [
      row('dear', usage({ burn: buckets(10), burn_cost: priced(9) })),
      // Cache reads are left out of the weight, so this enormous read ranks low.
      row('reader', usage({ burn: buckets(100, { cache_read: 10000000 }) })),
      row('writer', usage({ burn: buckets(100, { cache_create: 5000, input: 1000 }) })),
      row('talker', usage({ burn: buckets(3000) })),
      row('unread', null),
    ]
    expect(usageRanking(rows)).toBe('unpriced_token_weight')
    expect(mostExpensiveFirst(rows).map((each) => each.slug))
      .toEqual(['writer', 'talker', 'reader', 'dear', 'unread'])
  })

  it('treats a partial cost as unpriced', () => {
    const rows = [
      row('a', usage({ burn: buckets(10), burn_cost: priced(9) })),
      row('b', usage({ burn: buckets(50), burn_cost: { ...priced(1), partial: true } })),
    ]
    expect(usageRanking(rows)).toBe('unpriced_token_weight')
    expect(mostExpensiveFirst(rows).map((each) => each.slug)).toEqual(['b', 'a'])
  })

  it('ranks by the burn cost, never the run total cost', () => {
    const rows = [
      // The dearest run overall, quiet now.
      row('old', usage({ cost: priced(500), burn_cost: priced(1) })),
      row('busy', usage({ cost: priced(20), burn_cost: priced(6) })),
    ]
    expect(usageRanking(rows)).toBe('cost')
    expect(mostExpensiveFirst(rows).map((each) => each.slug)).toEqual(['busy', 'old'])
  })

  it('treats a total cost without a burn cost as unpriced', () => {
    const rows = [row('a', usage({ cost: priced(9) }))]
    expect(usageRanking(rows)).toBe('unpriced_token_weight')
  })

  it('does not reorder the list it was given', () => {
    const rows = [row('a', usage({ burn: buckets(1) })), row('b', usage({ burn: buckets(2) }))]
    mostExpensiveFirst(rows)
    expect(rows.map((each) => each.slug)).toEqual(['a', 'b'])
  })
})

describe('the cost label', () => {
  it('renders an unknown total as nothing, never a zero', () => {
    expect(costLabel({ currency: 'USD', total: null, partial: true })).toBe(null)
    expect(costLabel({ currency: 'USD', total: 0, partial: false })).toBe('USD 0.00')
  })
})
