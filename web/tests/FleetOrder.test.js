// The run list's "most expensive now" order (issue 315, spec §5.8).
//
// Run with `npm test` from web/ (see web/docs/TESTING.md).

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import App from '../src/App.vue'
import RunCard from '../src/components/RunCard.vue'
import { fetchProjects, fetchState } from '../src/api.js'
import { mountPane } from './mount.js'

vi.mock('../src/api.js', () => ({
  fetchState: vi.fn(),
  fetchProjects: vi.fn(),
  fetchRunRelations: vi.fn().mockResolvedValue({ relations: [] }),
  fetchUsage: vi.fn().mockResolvedValue({ enabled: false }),
  forgetRun: vi.fn(),
  prune: vi.fn(),
  rescan: vi.fn(),
}))

const buckets = (output) => ({ requests: 1, input: 0, cache_create: 0, cache_read: 0, output, thinking: 0 })
const usage = (burn) => ({ totals: buckets(burn * 10), burn: buckets(burn), models: [], status: 'collected', sessions: 1 })

const run = (slug, extra = {}) => ({
  host: 'gitlab.example.com', project: 'acme/widget', slug, label: slug, workspace_id: 'general',
  state: 'running', live: true, ...extra,
})

beforeEach(() => {
  vi.clearAllMocks()
  window.localStorage.clear()
  fetchProjects.mockResolvedValue({ projects: [{ id: 'general', name: 'General', color: 'Blue', assistant: { lifecycle: 'stopped' } }] })
  window.history.replaceState({}, '', '/?workspace=general')
  window.matchMedia = vi.fn().mockImplementation((query) => ({
    matches: false, media: query, addEventListener() {}, removeEventListener() {},
  }))
})

async function mountFleet(payload) {
  fetchState.mockResolvedValue({ attention: [], totals: {}, relations: [], ...payload })
  const wrapper = mountPane(App, {})
  await flushPromises()
  await flushPromises()
  return wrapper
}

const order = (wrapper) => wrapper.findAllComponents(RunCard).map((card) => card.props('run').slug)
const toggle = (wrapper) => wrapper.find('[aria-label="list order"]')
const button = (wrapper, text) => toggle(wrapper).findAll('button').find((b) => b.text() === text)

describe('most expensive now', () => {
  it('is not offered while no run has a burn reading', async () => {
    const wrapper = await mountFleet({ runs: [run('a'), run('b')] })
    expect(toggle(wrapper).exists()).toBe(false)
    wrapper.unmount()
  })

  it('keeps the list order until chosen, then ranks by burn and remembers it', async () => {
    const runs = [run('quiet', { usage: usage(10) }), run('busy', { usage: usage(5000) }), run('unread')]
    const wrapper = await mountFleet({ runs })
    expect(order(wrapper)).toEqual(['quiet', 'busy', 'unread'])

    await button(wrapper, 'most expensive now').trigger('click')
    await flushPromises()
    expect(order(wrapper)).toEqual(['busy', 'quiet', 'unread'])
    expect(window.localStorage.getItem('lmer.fleet.order')).toBe('cost')
    wrapper.unmount()
  })

  it('ranks inside each group, so a run that needs you stays first', async () => {
    window.localStorage.setItem('lmer.fleet.order', 'cost')
    const waiting = run('waiting', { usage: usage(1), attention: { kind: 'ask' } })
    const runs = [waiting, run('busy', { usage: usage(5000) }), run('quiet', { usage: usage(10) })]
    const wrapper = await mountFleet({ runs, attention: [waiting] })
    expect(order(wrapper)).toEqual(['waiting', 'busy', 'quiet'])
    wrapper.unmount()
  })
})
