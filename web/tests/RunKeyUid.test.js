// A schema-2 row is keyed by its uid, not by its slug (MR !387 review).
//
// The slug of a schema-2 row is the label of its `_runs/<slug>/` group, so two
// records of one work item share it. The shell decides which row is selected,
// hidden after a forget and opened by a run reference with `runKey`; spelled
// `host/project/slug`, those two rows were one key — selecting the second opened
// the first, forgetting one hid both, and a reference naming a uid matched no
// row at all. The rule is the backend's `runs.key_parts`: a 32-lowercase-hex
// `uid` wins, otherwise the slug. Schema-1 rows keep the key they always had.
//
// Run with `npm test` from web/ (see web/docs/TESTING.md).

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import App from '../src/App.vue'
import RunCard from '../src/components/RunCard.vue'
import RunDetail from '../src/components/RunDetail.vue'
import * as api from '../src/api.js'
import { dispatchRunRef, parseRunRef, runKey } from '../src/runref.js'
import { mountPane } from './mount.js'

vi.mock('../src/api.js', () => ({
  fetchState: vi.fn(),
  fetchProjects: vi.fn(),
  fetchRunRelations: vi.fn(),
  forgetRun: vi.fn(),
  prune: vi.fn(),
  rescan: vi.fn(),
}))

const uidA = '0123456789abcdef0123456789abcdef'
const uidB = 'fedcba9876543210fedcba9876543210'

describe('runKey', () => {
  it('keys a schema-1 row by its slug, byte for byte as before', () => {
    const row = { host: 'h.test', project: 'acme/widget', slug: 'develop-1' }
    expect(runKey(row)).toBe('h.test/acme/widget/develop-1')
    expect(runKey({ ...row, uid: null })).toBe('h.test/acme/widget/develop-1')
    expect(runKey({ ...row, uid: '' })).toBe('h.test/acme/widget/develop-1')
  })

  it('keys a schema-2 row by its uid', () => {
    const row = { host: 'h.test', project: 'acme/widget', slug: 'develop-1', uid: uidA }
    expect(runKey(row)).toBe(`h.test/acme/widget/${uidA}`)
    expect(runKey({ ...row, uid: uidB })).not.toBe(runKey(row))
  })

  it('degrades a malformed uid to the slug, as key_parts_lenient does', () => {
    // Read for display and selection, never acted on: a uid that is not 32
    // lowercase hex is not one, and the row keeps the key it can be found by.
    const row = { host: 'h', project: 'p', slug: 's' }
    for (const uid of [uidA.toUpperCase(), uidA.slice(1), `${uidA}0`, 42, 'not-a-uid']) {
      expect(runKey({ ...row, uid })).toBe('h/p/s')
    }
  })

  it('is null for no run', () => {
    expect(runKey(null)).toBe(null)
  })
})

describe('parseRunRef', () => {
  it('parses a slug reference into exactly the fields it always had', () => {
    expect(parseRunRef('lmer://run/h.test/acme/widget/develop-1')).toEqual({
      host: 'h.test', project: 'acme/widget', slug: 'develop-1', key: 'h.test/acme/widget/develop-1',
    })
    expect(parseRunRef('lmer://run/h.test/acme/widget/develop-1')).not.toHaveProperty('uid')
  })

  it('reads a last segment of 32 lowercase hex as a uid', () => {
    const ref = parseRunRef(`lmer://run/h.test/acme/widget/${uidA}`)
    expect(ref).toEqual({
      host: 'h.test', project: 'acme/widget', slug: uidA, uid: uidA, key: `h.test/acme/widget/${uidA}`,
    })
    expect(runKey(ref)).toBe(ref.key)
  })

  it('does not read an almost-uid as one', () => {
    for (const segment of [uidA.toUpperCase(), uidA.slice(1), `${uidA}0`]) {
      expect(parseRunRef(`lmer://run/h/p/${segment}`)).not.toHaveProperty('uid')
    }
  })

  it('keys a uid reference to the row it names', () => {
    const row = { host: 'h.test', project: 'acme/widget', slug: 'develop-1', uid: uidB }
    expect(parseRunRef(`lmer://run/h.test/acme/widget/${uidB}`).key).toBe(runKey(row))
  })
})

describe('two schema-2 rows of one group in the shell', () => {
  // One `_runs/develop-1/` group, two records: same host, project and slug.
  const row = (uid, title) => ({
    host: 'h.test', project: 'acme/widget', slug: 'develop-1', uid, title, label: title,
    workspace_id: 'general', state: 'complete', live: false, attention: null, questions: [],
  })
  const a = row(uidA, 'first attempt')
  const b = row(uidB, 'second attempt')

  beforeEach(() => {
    vi.clearAllMocks()
    window.localStorage.clear()
    window.history.replaceState({}, '', '/?workspace=general')
    window.matchMedia = vi.fn().mockImplementation((query) => ({
      matches: false, media: query, addEventListener() {}, removeEventListener() {},
    }))
    api.fetchState.mockResolvedValue({ runs: [a, b], attention: [], totals: {}, relations: [] })
    api.fetchProjects.mockResolvedValue({
      projects: [{ id: 'general', name: 'General', color: 'Blue', assistant: { lifecycle: 'stopped' } }],
    })
    api.forgetRun.mockResolvedValue({ forgotten: true })
  })

  async function mountShell() {
    const wrapper = mountPane(App, {}, { global: { stubs: { RunDetail: true, AssistantChat: true } } })
    await flushPromises()
    await flushPromises()
    return wrapper
  }

  const cardFor = (wrapper, run) => wrapper.findAllComponents(RunCard)
    .find((card) => card.props('run').uid === run.uid)

  it('opens the row that was selected', async () => {
    const wrapper = await mountShell()
    for (const picked of [b, a]) {
      cardFor(wrapper, picked).vm.$emit('open', picked)
      await flushPromises()
      expect(wrapper.getComponent(RunDetail).props('run').uid).toBe(picked.uid)
      wrapper.findComponent(RunDetail).vm.$emit('close')
      await flushPromises()
    }
    wrapper.unmount()
  })

  it('hides only the row that was forgotten', async () => {
    const wrapper = await mountShell()
    expect(wrapper.findAllComponents(RunCard)).toHaveLength(2)
    cardFor(wrapper, a).vm.$emit('forget', a)
    await flushPromises()
    expect(wrapper.findAllComponents(RunCard).map((card) => card.props('run').uid)).toEqual([uidB])
    wrapper.unmount()
  })

  it('opens the row a uid reference names', async () => {
    const wrapper = await mountShell()
    for (const picked of [b, a]) {
      expect(dispatchRunRef(parseRunRef(`lmer://run/h.test/acme/widget/${picked.uid}`))).toBe(true)
      await flushPromises()
      expect(wrapper.getComponent(RunDetail).props('run').uid).toBe(picked.uid)
      expect(wrapper.text()).not.toContain('does not track that run')
    }
    wrapper.unmount()
  })
})
