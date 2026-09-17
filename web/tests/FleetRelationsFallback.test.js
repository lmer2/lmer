// The back-compat relations fallback (#141 fleet map).
//
// This whole path is temporary: the forward design is the `relations` key on the
// fleet payload, and this code is dead the moment the deployed daemon carries it.
// It is tested anyway, and tested for the properties that make it survivable
// rather than merely present — a fallback that fires once per poll per run is a
// request storm, and one that keeps firing after the daemon grows the key is a
// permanent second path wearing a temporary label.
//
// Run with `npm test` from web/ (see web/docs/TESTING.md).

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import App from '../src/App.vue'
import { fetchRunRelations, fetchState } from '../src/api.js'
import { mountPane } from './mount.js'

vi.mock('../src/api.js', () => ({
  fetchState: vi.fn(),
  fetchRunRelations: vi.fn(),
  forgetRun: vi.fn(),
  prune: vi.fn(),
  rescan: vi.fn(),
}))

const run = (slug, extra = {}) => ({
  host: 'gitlab.example.com', project: 'acme/widget', slug, label: slug,
  state: 'running', live: true, ...extra,
})

const key = (r) => `${r.host}/${r.project}/${r.slug}`

const reply = (subject, others) => ({
  run: { host: subject.host, project: subject.project, slug: subject.slug },
  relations: others.map((other) => ({ key: key(other) })),
})

beforeEach(() => {
  vi.clearAllMocks()
  window.matchMedia = vi.fn().mockImplementation((query) => ({
    matches: false, media: query, addEventListener() {}, removeEventListener() {},
  }))
})

async function mountFleet(payload) {
  fetchState.mockResolvedValue(payload)
  const wrapper = mountPane(App, {})
  await flushPromises()
  await flushPromises()
  return wrapper
}

describe('relations when the daemon does not send them', () => {
  it('asks the per-run route once per visible run', async () => {
    const a = run('one')
    const b = run('two')
    fetchRunRelations.mockImplementation((r) => Promise.resolve(
      r.slug === 'one' ? reply(a, [b]) : reply(b, [a]),
    ))
    // No `relations` key at all — this is the older daemon.
    const wrapper = await mountFleet({ runs: [a, b], attention: [], totals: {} })
    expect(fetchRunRelations).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })

  it('does not ask again while the same runs keep arriving', async () => {
    // The fleet re-polls every ten seconds. Asking per run per poll would be a
    // request storm for data that changes when somebody relates two runs.
    const a = run('one')
    fetchRunRelations.mockResolvedValue(reply(a, []))
    const wrapper = await mountFleet({ runs: [a], attention: [], totals: {} })
    expect(fetchRunRelations).toHaveBeenCalledTimes(1)

    // A second poll carrying the same fleet, with a state change on it.
    fetchState.mockResolvedValue({
      runs: [run('one', { state: 'failed' })], attention: [], totals: {},
    })
    await flushPromises()
    await flushPromises()
    expect(fetchRunRelations).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })

  it('stands down entirely once the daemon sends the key', async () => {
    const a = run('one')
    fetchRunRelations.mockResolvedValue(reply(a, []))
    const wrapper = await mountFleet({
      runs: [a], attention: [], totals: {}, relations: [],
    })
    expect(fetchRunRelations).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('survives a run the daemon will not answer for', async () => {
    // One failed read costs that run's edges, not the whole map.
    const a = run('one')
    const b = run('two')
    fetchRunRelations.mockImplementation((r) => (
      r.slug === 'one' ? Promise.reject(new Error('nope')) : Promise.resolve(reply(b, [a]))
    ))
    const wrapper = await mountFleet({ runs: [a, b], attention: [], totals: {} })
    expect(wrapper.html()).toContain('fleet')
    wrapper.unmount()
  })
})
