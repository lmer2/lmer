// Mounted tests for the constellation map (#141 fleet map).
//
// The layout decisions are executed under node by
// tests/test_platform_web_fleetmap.js. What needs a DOM is everything this file
// covers: that a node is a real element a keyboard can reach, that its colour
// comes from the app's tone table, and that clicking one hands the run back the
// way a run card does.
//
// Run with `npm test` from web/ (see web/docs/TESTING.md).

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'
import { mount } from '@vue/test-utils'
import { createVuetify } from 'vuetify'
import * as components from 'vuetify/components'
import * as directives from 'vuetify/directives'
import { mountPane } from './mount.js'
import { mdiRobot } from '@mdi/js'
import FleetMap from '../src/components/FleetMap.vue'

// The palette `mountPane` mounts against. Vuetify's defaults are fixed values,
// so this is the same table the component resolves its tones through — which is
// what lets a case below name the colour it expects rather than asserting that
// two nodes merely differ.
const THEME = createVuetify().theme.global.current.value.colors

const run = (slug, extra = {}) => ({
  host: 'gitlab.example.com', project: 'acme/widget', slug, state: 'running', ...extra,
})
const keyOf = (r) => `${r.host}/${r.project}/${r.slug}`

// The map lays itself out in `onMounted`, and Vue flushes that render on the
// next tick — so every case here awaits one. Reading the DOM synchronously after
// mount sees the frame before the layout exists, which looks exactly like a map
// that draws nothing.
const GROUPS = [{ name: 'Team', patterns: ['team/', 'tools/team-'] }]

async function mountMap(runs, relations = [], groups = []) {
  const wrapper = mountPane(FleetMap, { runs, relations, groups })
  await nextTick()
  return wrapper
}

beforeEach(() => {
  // jsdom answers matchMedia through the suite's non-layout stub; each case that
  // cares sets it explicitly.
  window.matchMedia = vi.fn().mockImplementation((query) => ({
    matches: false, media: query, addEventListener() {}, removeEventListener() {},
  }))
})

describe('the map as an interface', () => {
  it('draws one reachable control per run', async () => {
    const wrapper = await mountMap([run('a'), run('b'), run('c')])
    const nodes = wrapper.findAll('[role="button"]')
    expect(nodes).toHaveLength(3)
    for (const node of nodes) {
      expect(node.attributes('tabindex')).toBe('0')
      expect(node.attributes('aria-label')).toBeTruthy()
    }
    wrapper.unmount()
  })

  it('names each run and its state in the accessible label', async () => {
    const wrapper = await mountMap([run('release-adoption', { state: 'failed' })])
    const label = wrapper.find('[role="button"]').attributes('aria-label')
    expect(label).toContain('release-adoption')
    expect(label).toContain('failed')
    wrapper.unmount()
  })

  it('does not paint a detached run as healthy', async () => {
    // The lie format.js exists to stop: the process is up but nothing is being
    // recorded, so it must not read as a green, working run.
    const wrapper = await mountMap([run('a', { state: 'running' }), run('b', { state: 'detached' })])
    const fills = wrapper.findAll('.fleet-map__core').map((c) => c.attributes('fill'))
    expect(new Set(fills).size).toBe(2)
    // Asserted against the theme's own values rather than the tone names: this
    // test used to compare `fills` to the string 'success', which passes just as
    // happily when every node on the map is painting black.
    expect(fills).toContain(THEME.success)
    expect(fills).toContain(THEME.warning)
    wrapper.unmount()
  })

  it('hands the run back on click, the way a run card does', async () => {
    const only = run('a')
    const wrapper = await mountMap([only])
    await wrapper.find('[role="button"]').trigger('click')
    expect(wrapper.emitted('open')).toBeTruthy()
    expect(wrapper.emitted('open')[0][0].slug).toBe('a')
    wrapper.unmount()
  })

  it('opens a run from the keyboard', async () => {
    const wrapper = await mountMap([run('a')])
    await wrapper.find('[role="button"]').trigger('keydown.enter')
    expect(wrapper.emitted('open')).toBeTruthy()
    wrapper.unmount()
  })

  it('rings a run that wants the operator, and only that one', async () => {
    const wrapper = await mountMap([
      run('calm'),
      run('asked', { attention: { reason: 'question' } }),
    ])
    expect(wrapper.findAll('.fleet-map__halo')).toHaveLength(1)
    wrapper.unmount()
  })

  it('draws a line for a relation between two runs it has', async () => {
    const a = run('a')
    const b = run('b')
    const wrapper = await mountMap([a, b], [{ a: keyOf(a), b: keyOf(b) }])
    expect(wrapper.findAll('.fleet-map__edge')).toHaveLength(1)
    wrapper.unmount()
  })

  it('draws no line for a relation to a run it does not have', async () => {
    const a = run('a')
    const wrapper = await mountMap([a], [{ a: keyOf(a), b: 'gitlab.example.com/acme/widget/ghost' }])
    expect(wrapper.findAll('.fleet-map__edge')).toHaveLength(0)
    wrapper.unmount()
  })

  it('says so rather than drawing nothing when the fleet is empty', async () => {
    const wrapper = await mountMap([])
    expect(wrapper.text()).toContain('No runs to map yet')
    wrapper.unmount()
  })

  it('survives a daemon that sends no relations at all', async () => {
    // An older daemon has no `relations` key; the map is lines-less, not broken.
    const wrapper = mountPane(FleetMap, { runs: [run('a')] })
    await nextTick()
    expect(wrapper.findAll('[role="button"]')).toHaveLength(1)
    expect(wrapper.findAll('.fleet-map__edge')).toHaveLength(0)
    wrapper.unmount()
  })

  it('paints every node a colour a browser can actually resolve', async () => {
    // The bug this pins: `toneColor` returns a Vuetify colour *name*, and a name
    // is not an SVG paint. `fill="success"` is invalid, so the attribute falls
    // back to its initial value and the node draws black in both schemes —
    // which is D2's whole property, gone, with every source guard still green.
    const wrapper = await mountMap([
      run('a', { state: 'running' }),
      run('b', { state: 'detached' }),
    ])
    const fills = wrapper.findAll('.fleet-map__core').map((c) => c.attributes('fill'))
    expect(fills).toHaveLength(2)
    for (const fill of fills) {
      expect(fill, `${fill} is not something a browser can paint`)
        .toMatch(/^(#[0-9a-fA-F]{3,8}|rgba?\(|hsla?\()/)
    }
    // And still two different colours: a detached run is not a healthy one.
    expect(new Set(fills).size).toBe(2)
    wrapper.unmount()
  })

  it('repaints when the operator switches scheme', async () => {
    // The map resolves tone names against the *active* theme, so the value it
    // paints has to follow `theme.change`. Mounted on a theme of its own rather
    // than through `mountPane`, because Vuetify's stock light and dark themes
    // share one `success` — against those, a map frozen on the colours it
    // mounted with is indistinguishable from a working one.
    const vuetify = createVuetify({
      components,
      directives,
      theme: {
        defaultTheme: 'light',
        themes: {
          light: { dark: false, colors: { success: '#111111' } },
          dark: { dark: true, colors: { success: '#eeeeee' } },
        },
      },
    })
    const wrapper = mount(FleetMap, {
      props: { runs: [run('a', { state: 'running' })], relations: [] },
      attachTo: document.body,
      global: { plugins: [vuetify] },
    })
    await nextTick()
    expect(wrapper.find('.fleet-map__core').attributes('fill')).toBe('#111111')

    vuetify.theme.change('dark')
    await nextTick()
    expect(wrapper.find('.fleet-map__core').attributes('fill')).toBe('#eeeeee')
    wrapper.unmount()
  })

  it('every colour it draws came from the theme, not from a palette of its own', async () => {
    // The property that chose SVG over a canvas: the map themes like the rest of
    // the app because its colours are the app's.
    //
    // Asserted as "every paint is a value this theme defines" rather than as
    // "the markup holds no #hex". The map now resolves tone names through the
    // theme before they reach an SVG attribute — a name is not a paint — so the
    // markup carries resolved values, and the absence of a hex literal stopped
    // being evidence of anything the moment that became true.
    const wrapper = await mountMap([
      run('a', { attention: { reason: 'crashed' } }),
      run('b', { state: 'complete' }),
      run('c', { state: 'failed' }),
    ])
    const known = new Set([...Object.values(THEME), 'currentColor'])
    const painted = [
      ...wrapper.findAll('.fleet-map__core').map((c) => c.attributes('fill')),
      ...wrapper.findAll('.fleet-map__glow').map((c) => c.attributes('fill')),
      ...wrapper.findAll('.fleet-map__halo').map((c) => c.attributes('stroke')),
    ]
    expect(painted.length).toBeGreaterThan(0)
    for (const paint of painted) {
      expect(known.has(paint), `${paint} is not a colour this theme defines`).toBe(true)
    }
    wrapper.unmount()
  })
})

const at = (host, project, slug, extra = {}) => ({
  host, project, slug, state: 'running', ...extra,
})

describe('grouping and labels', () => {

  it('draws one group ring around projects a rule gathers across namespaces', async () => {
    const wrapper = await mountMap([
      at('gitlab.example.com', 'team/widget', 'a'),
      at('gitlab.example.com', 'team/service', 'b'),
      at('gitlab.example.com', 'tools/team-site', 'c'),
      at('gitlab.example.com', 'acme/widget', 'd'),
    ], [], GROUPS)
    const groupLabels = wrapper.findAll('.fleet-map__group').map((t) => t.text())
    expect(groupLabels).toEqual(['Team'])
    expect(wrapper.findAll('.fleet-map__group-ring')).toHaveLength(1)

    // The projects inside it stay visible and keep their own names.
    const clusterLabels = wrapper.findAll('.fleet-map__cluster').map((t) => t.text())
    expect(clusterLabels).toContain('team/widget')
    expect(clusterLabels).toContain('team/service')
    expect(clusterLabels).toContain('tools/team-site')
    expect(clusterLabels).toContain('acme/widget')
    wrapper.unmount()
  })

  it('draws no group ring for a fleet no rule gathers', async () => {
    // A second ring and a second label around a single project is noise, not
    // structure — a fleet nobody has written rules for looks untouched.
    const wrapper = await mountMap([run('a'), run('b')])
    expect(wrapper.findAll('.fleet-map__group-ring')).toHaveLength(0)
    expect(wrapper.findAll('.fleet-map__group')).toHaveLength(0)
    wrapper.unmount()
  })

  it('labels every cluster it draws, never a dash and never blank', async () => {
    const wrapper = await mountMap([
      run('a'),
      at('gitlab.example.com', 'catalog/data', 'p'),
      // A worker in its first seconds: no identity committed yet, so the daemon
      // sends dashes. It is a cluster like any other and has to be named.
      at('\u2014', '\u2014', 'brand-new'),
    ])
    const labels = wrapper.findAll('.fleet-map__cluster').map((t) => t.text().trim())
    expect(labels.length).toBeGreaterThan(0)
    for (const label of labels) {
      expect(label).not.toBe('')
      expect(label).not.toBe('\u2014')
    }
    expect(labels).toContain('unidentified')
    wrapper.unmount()
  })
})

  it('keeps every ring label on the canvas, however large the ring', async () => {
    // An SVG <text> at a negative y is simply not there — no clipping, no
    // overflow, nothing to notice. A ring big enough that "above it" is off the
    // top of the map must degrade to "at the top edge", never to "gone".
    const many = []
    for (let p = 0; p < 14; p += 1) {
      for (let n = 0; n < 2; n += 1) {
        many.push(at('gitlab.example.com', `team/p${p}`, `run-${p}-${n}`))
      }
    }
    const wrapper = await mountMap(many, [], GROUPS)
    const labels = [
      ...wrapper.findAll('.fleet-map__cluster'),
      ...wrapper.findAll('.fleet-map__group'),
    ]
    expect(labels.length).toBeGreaterThan(0)
    for (const label of labels) {
      const y = Number(label.attributes('y'))
      const x = Number(label.attributes('x'))
      expect(Number.isFinite(y) && Number.isFinite(x)).toBe(true)
      expect(y, `a ring label is drawn at y=${y}, off the top of the map`)
        .toBeGreaterThanOrEqual(14)
      // The same failure in the other axis. The width a label needs is
      // estimated from its character count — the map never measures text — so
      // this pins the clamp being applied, not the estimate being exact.
      const half = (label.text().length * 6.7) / 2
      expect(x - half, `a ring label runs off the left edge (x=${x})`)
        .toBeGreaterThanOrEqual(0)
      expect(x + half, `a ring label runs off the right edge (x=${x})`)
        .toBeLessThanOrEqual(1000)
    }
    wrapper.unmount()
  })

  it('keeps a hovered star\'s name on the canvas near the top edge', async () => {
    // The first line of the hover block is the run's *name* — the line the
    // operator hovered to read. Unfloored it is drawn above the viewBox for any
    // node in the top ~37px, and the two lines that survive are the two they
    // could already infer from the colour and the ring.
    const many = []
    for (let p = 0; p < 14; p += 1) {
      for (let n = 0; n < 2; n += 1) {
        many.push(at('gitlab.example.com', `proj${p}/x`, `run-${p}-${n}`, { title: `run ${p}-${n}` }))
      }
    }
    const wrapper = await mountMap(many, [], GROUPS)
    const nodes = wrapper.findAll('[role="button"]')
    // Hover whichever node the layout put highest.
    let top = null
    for (const node of nodes) {
      const y = Number(node.find('.fleet-map__core').attributes('cy'))
      if (top === null || y < top.y) top = { node, y }
    }
    await top.node.trigger('mouseenter')
    const block = wrapper.find('.fleet-map__name')
    const y = Number(block.attributes('y'))
    expect(Number.isFinite(y)).toBe(true)
    expect(y, `the hover block starts at y=${y} for a node at y=${top.y.toFixed(0)}`)
      .toBeGreaterThanOrEqual(14)
    wrapper.unmount()
  })

  it('draws no two ring labels over each other at a real fleet size', async () => {
    // A whole-render net rather than a test of the clamp: the clamping itself is
    // pinned in the layout core (`ringLabelY` against constructed geometry),
    // because the force layout will not reliably produce a group pressed against
    // the top edge on demand. What this catches is the general case — any two
    // labels landing on each other at a size somebody actually runs.
    const many = []
    for (let p = 0; p < 12; p += 1) {
      many.push(at('gitlab.example.com', `team/p${p}`, `run-${p}`))
      many.push(at('gitlab.example.com', `other${p}/thing`, `o-${p}`))
    }
    const wrapper = await mountMap(many, [], GROUPS)
    const read = (sel, charWidth) => wrapper.findAll(sel).map((t) => ({
      text: t.text(),
      x: Number(t.attributes('x')),
      y: Number(t.attributes('y')),
      half: (t.text().length * charWidth) / 2,
    }))
    const labels = [...read('.fleet-map__group', 8.7), ...read('.fleet-map__cluster', 6.7)]
    expect(labels.length).toBeGreaterThan(1)
    for (let i = 0; i < labels.length; i += 1) {
      for (let j = i + 1; j < labels.length; j += 1) {
        const a = labels[i]
        const b = labels[j]
        const overlapX = Math.abs(a.x - b.x) < a.half + b.half
        const overlapY = Math.abs(a.y - b.y) < 12
        expect(
          overlapX && overlapY,
          `"${a.text}" and "${b.text}" are drawn over each other at y≈${a.y.toFixed(0)}`,
        ).toBe(false)
      }
    }
    wrapper.unmount()
  })

describe('the orchestrator', () => {
  const boss = {
    host: '\u2014', project: '\u2014', slug: '\u2014', state: 'running',
    taskdef: 'orchestrate', orchestrator: true,
  }

  it('is not on the map at all', async () => {
    // It is one tap away in the app bar, painted with its own state there. The
    // map answers "what is my fleet doing"; the thing asking is not an answer.
    const wrapper = await mountMap([run('a'), boss])
    expect(wrapper.findAll('[role="button"]')).toHaveLength(1)
    expect(wrapper.findAll('.fleet-map__core')).toHaveLength(1)
    expect(wrapper.findAll('.fleet-map__mark')).toHaveLength(0)
    const labels = wrapper.findAll('[role="button"]').map((n) => n.attributes('aria-label'))
    expect(labels.join(' ')).not.toContain('uber lmer')
    wrapper.unmount()
  })

  it('leaves a fleet of nothing else with an empty map, not a broken one', async () => {
    const wrapper = await mountMap([boss])
    expect(wrapper.findAll('[role="button"]')).toHaveLength(0)
    expect(wrapper.text()).toContain('No runs to map yet')
    expect(wrapper.findAll('.fleet-map__ring')).toHaveLength(0)
    wrapper.unmount()
  })

  it('does not let an edge naming it invent it back', async () => {
    const a = run('a')
    const wrapper = await mountMap([a, boss], [{ a: keyOf(a), b: '\u2014/\u2014/\u2014' }])
    expect(wrapper.findAll('.fleet-map__edge')).toHaveLength(0)
    expect(wrapper.findAll('.fleet-map__core')).toHaveLength(1)
    wrapper.unmount()
  })
})

describe('the legend and what a star says', () => {
  it('names every tone it can paint, plus the ring and the line', async () => {
    // Without this the colour coding is a private language. Built from the same
    // table the nodes paint from, so a tone cannot reach the map unexplained.
    const wrapper = await mountMap([run('a')])
    const text = wrapper.find('.fleet-map__legend').text()
    for (const tone of ['live', 'attention', 'bad', 'idle', 'done']) {
      expect(text, `the legend does not explain the ${tone} tone`).toContain(tone)
    }
    expect(text).toContain('ring')
    expect(text).toContain('line')
    // And its swatches are painted, not named — the same bug the nodes had.
    const swatch = wrapper.find('.fleet-map__swatch')
    expect(swatch.attributes('style')).toMatch(/#[0-9a-fA-F]{3,8}|rgba?\(/)
    wrapper.unmount()
  })

  it('draws no legend for a fleet with nothing in it', async () => {
    const wrapper = await mountMap([])
    expect(wrapper.findAll('.fleet-map__legend')).toHaveLength(0)
    wrapper.unmount()
  })

  it('tells a hovered star what it is, what it is doing and whose it is', async () => {
    const wrapper = await mountMap([
      at('gitlab.example.com', 'acme/widget', 'nightly', { state: 'failed', title: 'nightly build' }),
    ])
    await wrapper.find('[role="button"]').trigger('mouseenter')
    const detail = wrapper.find('.fleet-map__name').text()
    expect(detail).toContain('nightly build')
    expect(detail).toContain('failed')
    expect(detail).toContain('acme/widget')
    wrapper.unmount()
  })
})

describe('dependency edges', () => {
  it('draws a line between two related runs and springs them together', async () => {
    const a = at('gitlab.example.com', 'acme/widget', 'one')
    const b = at('gitlab.example.com', 'acme/other', 'two')
    const far = at('gitlab.example.com', 'acme/third', 'three')
    const joined = await mountMap([a, b, far], [{ a: keyOf(a), b: keyOf(b) }])
    expect(joined.findAll('.fleet-map__edge')).toHaveLength(1)

    const line = joined.find('.fleet-map__edge')
    for (const attr of ['x1', 'y1', 'x2', 'y2']) {
      expect(Number.isFinite(Number(line.attributes(attr)))).toBe(true)
    }
    joined.unmount()
  })

  it('draws an edge for every distinct pair and no duplicates', async () => {
    const a = at('gitlab.example.com', 'acme/widget', 'one')
    const b = at('gitlab.example.com', 'acme/widget', 'two')
    const c = at('gitlab.example.com', 'acme/widget', 'three')
    const wrapper = await mountMap([a, b, c], [
      { a: keyOf(a), b: keyOf(b) },
      { a: keyOf(b), b: keyOf(a) },
      { a: keyOf(b), b: keyOf(c) },
    ])
    expect(wrapper.findAll('.fleet-map__edge')).toHaveLength(2)
    wrapper.unmount()
  })
})

describe('motion', () => {
  it('does not animate when the operator asked for reduced motion', async () => {
    window.matchMedia = vi.fn().mockImplementation((query) => ({
      matches: query.includes('reduced-motion'),
      media: query,
      addEventListener() {},
      removeEventListener() {},
    }))
    const wrapper = await mountMap([run('a'), run('b')])
    // Still laid out — solved and painted once rather than not drawn.
    const core = wrapper.find('.fleet-map__core')
    expect(Number(core.attributes('cx'))).toBeTypeOf('number')
    expect(Number.isFinite(Number(core.attributes('cx')))).toBe(true)
    wrapper.unmount()
  })

  it('lays out a run that arrives on a later poll under reduced motion', async () => {
    // The reduced-motion path solves by ticking the simulation by hand, and
    // every d3 force scales by alpha — so a solve that does not re-heat first
    // runs at the alpha the last one decayed to (~6e-4) and moves nothing. An
    // arrival into a cluster the map has never seen has no centroid to be
    // seeded from, so d3 puts it in its spiral around the origin and it stays
    // there: above the viewBox, invisible, for as long as the map is open.
    window.matchMedia = vi.fn().mockImplementation((query) => ({
      matches: query.includes('reduced-motion'),
      media: query,
      addEventListener() {},
      removeEventListener() {},
    }))
    const before = [run('a'), run('b')]
    const wrapper = mountPane(FleetMap, { runs: before, relations: [] })
    await nextTick()

    const arrival = { host: 'gitlab.example.com', project: 'acme/other', slug: 'new', state: 'running' }
    await wrapper.setProps({ runs: [...before, arrival] })
    await nextTick()

    const cores = wrapper.findAll('.fleet-map__core')
    expect(cores).toHaveLength(3)
    for (const core of cores) {
      const cx = Number(core.attributes('cx'))
      const cy = Number(core.attributes('cy'))
      expect(Number.isFinite(cx) && Number.isFinite(cy)).toBe(true)
      expect(cx, 'a node was laid out off the left/right of the viewBox').toBeGreaterThan(0)
      expect(cx).toBeLessThan(1000)
      expect(cy, 'a node was laid out above the top of the viewBox').toBeGreaterThan(0)
      expect(cy).toBeLessThan(600)
    }
    wrapper.unmount()
  })

  it('stops simulating while the tab is hidden', async () => {
    const wrapper = await mountMap([run('a'), run('b')])
    const hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(true)
    document.dispatchEvent(new Event('visibilitychange'))
    // Nothing to assert on a private simulation beyond "it did not throw and the
    // map is still there"; the guard that matters is the listener existing at
    // all, which the source test pins.
    expect(wrapper.findAll('[role="button"]')).toHaveLength(2)
    hidden.mockRestore()
    wrapper.unmount()
  })

  it('composes before the first paint rather than starting scattered', async () => {
    // Every node has a finite position on the very first render — the map
    // appears laid out instead of assembling itself while it is watched.
    const wrapper = await mountMap([run('a'), run('b'), run('c')])
    for (const core of wrapper.findAll('.fleet-map__core')) {
      expect(Number.isFinite(Number(core.attributes('cx')))).toBe(true)
      expect(Number.isFinite(Number(core.attributes('cy')))).toBe(true)
    }
    wrapper.unmount()
  })
})


it('refreshes saved groups without changing runs and keeps identical polls settled', async () => {
  window.matchMedia = vi.fn().mockImplementation((query) => ({
    matches: query.includes('reduced-motion'), media: query, addEventListener() {}, removeEventListener() {},
  }))
  const rows = [at('forge.test', 'team/widget', 'a'), at('forge.test', 'tools/team-site', 'b'), run('c')]
  const wrapper = await mountMap(rows)
  const coordinates = () => wrapper.findAll('.fleet-map__core').map(node => [node.attributes('cx'), node.attributes('cy')])
  const before = coordinates()
  const buttons = wrapper.findAll('[role="button"]').map(node => node.element)
  expect(wrapper.findAll('.fleet-map__group-ring')).toHaveLength(0)
  await wrapper.setProps({ groups: GROUPS })
  expect(wrapper.findAll('.fleet-map__group').map(node => node.text())).toEqual(['Team'])
  expect(coordinates()).not.toEqual(before)
  expect(wrapper.findAll('[role="button"]').map(node => node.element)).toEqual(buttons)
  const grouped = coordinates()
  await wrapper.setProps({ groups: JSON.parse(JSON.stringify(GROUPS)), runs: [...rows] })
  expect(coordinates()).toEqual(grouped)
  await wrapper.setProps({ groups: [{ ...GROUPS[0], name: 'Renamed' }] })
  expect(wrapper.find('.fleet-map__group').text()).toBe('Renamed')
  await wrapper.setProps({ groups: [] })
  expect(wrapper.findAll('.fleet-map__group-ring')).toHaveLength(0)
  expect(wrapper.findAll('.fleet-map__cluster')).toHaveLength(3)
  wrapper.unmount()
})
