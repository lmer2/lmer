// Executable checks for the constellation map's layout core (#141 fleet map).
// Run by tests/test_platform_web_map.py.
//
// These are the parts of the map that are decisions rather than drawing: what a
// cluster is, what survives a poll, and what paints last. All three are wrong in
// ways a screenshot would not show — a map that quietly re-scatters every ten
// seconds looks fine in a still.

import assert from 'node:assert/strict'
import {
  ATTENTION_PRIORITY, FORCE, GROUP_RULES, SETTLE_STEPS, UNIDENTIFIED_LABEL,
  attentionRank, buildGraph, centroids, clusterKey, drawnGroups, groupClusters,
  CLUSTER_LABEL_FLOOR, DETAIL_FLOOR, GROUP_LABEL_FLOOR, groupRuleFor, groupRuns,
  detailY, isOrchestrator, orchestratorOf, paintOrder, pairsFromReplies,
  reconcile, reheat, ringLabelX, ringLabelY, seedPositions, settle,
} from '../web/src/fleetMap.js'

const run = (slug, extra = {}) => ({
  host: 'gitlab.example.com', project: 'acme/widget', slug, state: 'running', ...extra,
})
const keyOf = (r) => `${r.host}/${r.project}/${r.slug}`

// --- clusters ---------------------------------------------------------------

assert.equal(clusterKey(run('a')), 'gitlab.example.com/acme/widget')
assert.equal(clusterKey(null), null)

// A project path with more than one segment stays whole.
assert.equal(
  clusterKey({ host: 'github.com', project: 'gh/acme/widget', slug: 'x' }),
  'github.com/gh/acme/widget',
)

// Two hosts can carry the same project name; they are not one cluster.
{
  const groups = groupRuns([
    run('a'),
    { host: 'github.com', project: 'acme/widget', slug: 'b', state: 'running' },
  ])
  assert.equal(groups.length, 2, 'same project on two hosts must not merge')
}

// Grouping is stable, so two renders of one fleet lay out the same way.
{
  const runs = [run('c'), run('a'), { host: 'a.example', project: 'p', slug: 'z', state: 'running' }]
  const first = groupRuns(runs).map((g) => g.key)
  const second = groupRuns([...runs].reverse()).map((g) => g.key)
  assert.deepEqual(first, second, 'cluster order depends on input order')
}

// --- seeding ----------------------------------------------------------------
{
  const seeded = seedPositions(groupRuns([run('a'), { host: 'h', project: 'p', slug: 'b', state: 'running' }]))
  assert.equal(seeded.length, 2)
  for (const cluster of seeded) {
    assert.ok(Number.isFinite(cluster.x) && Number.isFinite(cluster.y), 'cluster seeded off-plane')
  }
  // Deterministic: the same fleet seeds identically twice, or the map appears to
  // jump on every reload.
  const again = seedPositions(groupRuns([run('a'), { host: 'h', project: 'p', slug: 'b', state: 'running' }]))
  assert.deepEqual(seeded.map((c) => [c.x, c.y]), again.map((c) => [c.x, c.y]))
}

// --- the graph --------------------------------------------------------------
{
  const runs = [run('a'), run('b')]
  const relations = [{ a: keyOf(runs[0]), b: keyOf(runs[1]) }]
  const graph = buildGraph(runs, relations)
  assert.equal(graph.nodes.length, 2)
  assert.equal(graph.links.length, 1)
  assert.equal(graph.links[0].cross, false, 'same project is not a cross-cluster link')
}

// An edge to a run this fleet does not carry is dropped — it has no node.
{
  const runs = [run('a')]
  const graph = buildGraph(runs, [{ a: keyOf(runs[0]), b: 'gitlab.example.com/acme/widget/ghost' }])
  assert.equal(graph.links.length, 0, 'an edge survived to a run with no node')
  assert.equal(graph.nodes.length, 1, 'the dangling edge invented a node')
}

// One pair spelled both ways is one spring, not two pulling the same nodes.
{
  const runs = [run('a'), run('b')]
  const a = keyOf(runs[0])
  const b = keyOf(runs[1])
  const graph = buildGraph(runs, [{ a, b }, { a: b, b: a }])
  assert.equal(graph.links.length, 1, 'a mirrored pair became two springs')
}

// A run related to itself is not an edge.
{
  const runs = [run('a')]
  const graph = buildGraph(runs, [{ a: keyOf(runs[0]), b: keyOf(runs[0]) }])
  assert.equal(graph.links.length, 0)
}

// A cross-project relation is marked, because it is sprung differently.
{
  const here = run('a')
  const there = { host: 'github.com', project: 'acme/toolkit', slug: 'b', state: 'running' }
  const graph = buildGraph([here, there], [{ a: keyOf(here), b: keyOf(there) }])
  assert.equal(graph.links[0].cross, true)
}

// --- the poll diff ----------------------------------------------------------

// The property the whole map rests on: a run that is still here keeps the node
// object, and therefore its position, across a poll.
{
  const runs = [run('a'), run('b')]
  const first = buildGraph(runs, [])
  first.nodes[0].x = 123
  first.nodes[0].y = 456

  const next = reconcile(first, runs, [])
  const moved = next.byKey.get(keyOf(runs[0]))
  assert.equal(moved.x, 123, 'a surviving run lost its position across a poll')
  assert.equal(moved.y, 456)
  assert.equal(next.changed, false, 'an unchanged fleet reported a change and would re-heat')
}

// A state change is not a shape change: it must not re-heat the simulation.
{
  const before = [run('a')]
  const first = buildGraph(before, [])
  first.nodes[0].x = 10
  first.nodes[0].y = 20

  const after = [run('a', { state: 'failed' })]
  const next = reconcile(first, after, [])
  assert.equal(next.changed, false, 'a state change re-heated the layout')
  assert.equal(next.nodes[0].run.state, 'failed', 'the row did not update in place')
  assert.equal(next.nodes[0].x, 10, 'the node moved for a state change')
}

// An arrival is a shape change, and lands at its cluster's centre of mass.
{
  const before = [run('a'), run('b')]
  const first = buildGraph(before, [])
  first.nodes[0].x = 100; first.nodes[0].y = 100
  first.nodes[1].x = 200; first.nodes[1].y = 200

  const next = reconcile(first, [...before, run('c')], [])
  assert.equal(next.changed, true, 'an arrival did not re-heat')
  assert.equal(next.added, 1)
  const arrival = next.byKey.get(keyOf(run('c')))
  assert.equal(arrival.x, 150, 'an arrival did not start at its cluster centre')
  assert.equal(arrival.y, 150)
}

// A departure is a shape change too, and the node goes.
{
  const before = [run('a'), run('b')]
  const first = buildGraph(before, [])
  const next = reconcile(first, [run('a')], [])
  assert.equal(next.changed, true)
  assert.equal(next.removed, 1)
  assert.equal(next.nodes.length, 1)
}

// A new relation between two runs already on the map is a shape change: the
// spring is new even though no node is.
{
  const runs = [run('a'), run('b')]
  const first = buildGraph(runs, [])
  const next = reconcile(first, runs, [{ a: keyOf(runs[0]), b: keyOf(runs[1]) }])
  assert.equal(next.changed, true, 'a new edge did not re-heat')
  assert.equal(next.links.length, 1)
}

// Links after a reconcile point at the kept node objects, not at fresh copies —
// otherwise the springs pull on nodes the simulation is not drawing.
{
  const runs = [run('a'), run('b')]
  const first = buildGraph(runs, [{ a: keyOf(runs[0]), b: keyOf(runs[1]) }])
  first.nodes[0].x = 7
  const next = reconcile(first, runs, [{ a: keyOf(runs[0]), b: keyOf(runs[1]) }])
  const endpoint = next.links[0].source.key === keyOf(runs[0]) ? next.links[0].source : next.links[0].target
  assert.equal(endpoint.x, 7, 'a link points at a copy rather than the live node')
  assert.ok(next.nodes.includes(endpoint), 'link endpoint is not one of the drawn nodes')
}

// --- attention and paint order ----------------------------------------------

assert.equal(attentionRank(run('a')), null, 'a calm run must not rank as attention')
assert.equal(attentionRank(run('a', { attention: { reason: 'question' } })), 1)

// A reason from a newer daemon still counts as attention rather than as calm.
assert.equal(
  attentionRank(run('a', { attention: { reason: 'something_new' } })) > ATTENTION_PRIORITY.unreadable,
  true,
  'an unknown attention reason was treated as calm',
)

{
  const calm = run('calm')
  const crashed = run('crashed', { attention: { reason: 'crashed' } })
  const asked = run('asked', { attention: { reason: 'live_question' } })
  const graph = buildGraph([crashed, calm, asked], [])
  const order = paintOrder(graph.nodes).map((n) => n.run.slug)
  assert.deepEqual(order, ['calm', 'crashed', 'asked'],
    'the most urgent run must paint last so nothing draws over it')
}

// Paint order is stable across polls for an unchanged fleet.
{
  const runs = [run('b'), run('a')]
  const graph = buildGraph(runs, [])
  assert.deepEqual(
    paintOrder(graph.nodes).map((n) => n.key),
    paintOrder([...graph.nodes].reverse()).map((n) => n.key),
    'paint order depends on node array order',
  )
}

// --- the force model --------------------------------------------------------
//
// The one number with a design argument behind it: a cross-project relation
// must pull more weakly than one inside a project, or a few of them drag every
// cluster into a single smear.
assert.ok(FORCE.linkStrengthCross < FORCE.linkStrengthSame,
  'a cross-cluster relation must hint, not haul')
assert.ok(FORCE.alphaMin > 0, 'the simulation must be able to stop')

// --- re-heating -------------------------------------------------------------
//
// The part that is invisible when it is wrong. A solve run by hand ticks the
// model directly, and every d3 force scales by alpha — so ticking a simulation
// that has already decayed to `alphaMin` applies forces at ~6e-4 and moves
// nothing. Nothing throws, nothing looks broken; the map just quietly stops
// laying out anything that arrives after the first frame. Recorded against a
// stub rather than d3 because this file runs under plain node, with no
// `node_modules` to resolve d3-force from.

const recorder = () => {
  const calls = []
  const fake = {
    calls,
    alpha(value) { calls.push(['alpha', value]); return fake },
    restart() { calls.push(['restart']); return fake },
    stop() { calls.push(['stop']); return fake },
    tick() { calls.push(['tick']); return fake },
  }
  return fake
}

// The reduced-motion path: re-heat, then solve it out in one go. The ordering is
// the assertion — a re-heat after the ticks powers nothing.
{
  const fake = recorder()
  reheat(fake, { instant: true })
  const verbs = fake.calls.map((c) => c[0])
  const heated = fake.calls.findIndex((c) => c[0] === 'alpha' && c[1] === FORCE.reheatAlpha)
  const firstTick = verbs.indexOf('tick')

  assert.ok(heated !== -1,
    'the instant path never re-heats, so every force it applies is scaled by a decayed alpha')
  assert.ok(firstTick !== -1, 'the instant path never ticks, so nothing is solved')
  assert.ok(heated < firstTick, 're-heat must land before the ticks it is there to power')
  assert.equal(verbs.filter((v) => v === 'restart').length, 0,
    'the reduced-motion path handed the solve to the animator')
  assert.equal(verbs.filter((v) => v === 'tick').length, SETTLE_STEPS)
}

// The animated path: re-heat and hand the running to d3, rather than ticking it
// out by hand while the operator watches.
{
  const fake = recorder()
  reheat(fake, {})
  assert.deepEqual(fake.calls, [['alpha', FORCE.reheatAlpha], ['restart']],
    'the animated path no longer re-heats and restarts, in that order')
}

// `settle` stops first: ticking a simulation whose ticker is still running
// double-steps the model and emits ticks the caller did not ask to paint.
{
  const fake = recorder()
  settle(fake, 3)
  assert.deepEqual(fake.calls.map((c) => c[0]), ['stop', 'tick', 'tick', 'tick'])
}

// Enough ticks to actually reach a standstill from a re-heat, or "settled" is a
// claim the map does not deliver. 0.9772 is d3-force's per-tick alpha decay
// (`1 - alphaMin**(1/300)` at its *default* alphaMin of 0.001 — the component
// lowers alphaMin, which does not recompute the decay), hardcoded because this
// file has no d3 to ask.
const D3_ALPHA_DECAY = 0.9772
assert.ok(FORCE.reheatAlpha * (D3_ALPHA_DECAY ** SETTLE_STEPS) < FORCE.alphaMin,
  'SETTLE_STEPS is too few to decay reheatAlpha to alphaMin: the solve stops mid-motion')

// --- configured literal text, ordered first-match grouping --------------------
const RULES = [{ name: 'Team', patterns: ['team/', 'tools/team-'] }]
assert.deepEqual(GROUP_RULES, [])
for (const project of ['team/widget', 'tools/team-site', 'catalog/data']) {
  assert.equal(groupRuleFor(project), null, 'an unconfigured graph has a default group')
}
{
  const rules = [...RULES, { name: 'Widget', patterns: ['widget'] }]
  for (const project of ['team/widget', 'TEAM/service', 'tools/team-site']) {
    assert.equal(groupRuleFor(project, rules)?.name, 'Team')
  }
  assert.equal(groupRuleFor('team/widget', [...rules].reverse())?.name, 'Widget')
  for (const project of ['teamwork/service', 'tools/other-site', 'catalog/data', null, '\u2014']) {
    assert.equal(groupRuleFor(project, rules), null)
  }
  assert.equal(groupRuleFor('notteam/widget', RULES)?.name, 'Team', 'text matching is not a path-boundary rule')
  const literal = [{ name: 'Literal', patterns: ['(a+)+$', '[', '*'] }]
  assert.equal(groupRuleFor('a'.repeat(100000) + '!', literal), null)
  assert.equal(groupRuleFor('team/widget', literal), null, '* is literal, not a wildcard')
  assert.equal(groupRuleFor('tools/(a+)+$', literal)?.name, 'Literal')
  assert.equal(groupRuleFor('tools/[draft]', literal)?.name, 'Literal')
  assert.equal(groupRuleFor('tools/*', literal)?.name, 'Literal')
  for (const bad of [null, {}, 'team', {name:'Bad'}, {name:'Bad', patterns:[{}]}, {name:'Bad',patterns:['']},
    {name:'Bad',patterns:[/team/]}, {name:'Bad',patterns:['x'.repeat(257)]}, {name:'Bad',patterns:Array(17).fill('team')}]) {
    assert.equal(groupRuleFor('team/widget', [bad, ...RULES])?.name, 'Team')
  }
  assert.equal(groupRuleFor('team/widget', {}), null)
  assert.equal(groupRuleFor('team/widget', Array(33).fill(RULES[0])), null)
  const unicode = [{ name: '🌍'.repeat(64), patterns: ['🌍'.repeat(256)] }]
  assert.equal(groupRuleFor('🌍'.repeat(256), unicode), unicode[0])
  assert.equal(groupRuleFor('🌍'.repeat(256), [{ ...unicode[0], name: '🌍'.repeat(65) }]), null)
  assert.equal(groupRuleFor('🌍'.repeat(257), [{ ...unicode[0], patterns: ['🌍'.repeat(257)] }]), null)
}

// --- the orchestrator is not in anything ------------------------------------
{
  const boss = { host: '\u2014', project: '\u2014', slug: '\u2014', state: 'running', orchestrator: true }
  assert.equal(isOrchestrator(boss), true)
  assert.equal(clusterKey(boss), null, 'the orchestrator was put in a cluster')

  const clusters = groupRuns([run('a'), boss])
  assert.equal(clusters.length, 1, 'the orchestrator became a cluster of its own')
  assert.ok(clusters.every((c) => c.runs.every((r) => !r.orchestrator)))

  // And it is not on the map at all: it is one tap away in the app bar, which is
  // where the thing asking "what is my fleet doing" belongs.
  const graph = buildGraph([run('a'), boss], [])
  assert.equal(graph.nodes.length, 1, 'the orchestrator was drawn as a node')
  assert.ok(graph.nodes.every((n) => !n.run.orchestrator))

  // An edge naming it is dropped with it, rather than inventing a node back.
  const withEdge = buildGraph([run('a'), boss], [{ a: keyOf(run('a')), b: '\u2014/\u2014/\u2014' }])
  assert.equal(withEdge.links.length, 0, 'an edge resurrected the orchestrator')
  assert.equal(withEdge.nodes.length, 1)

  // The bar needs it even though the map does not.
  assert.equal(orchestratorOf([run('a'), boss]), boss)
  assert.equal(orchestratorOf([run('a')]), null)
  assert.equal(orchestratorOf([]), null)

  // Detected by the flag, never by the dashes: a worker in its first seconds
  // carries the same dashes and is not the platform.
  const fresh = { host: '\u2014', project: '\u2014', slug: 'brand-new', state: 'running' }
  assert.equal(isOrchestrator(fresh), false)
  assert.ok(clusterKey(fresh), 'an identity-less worker lost its cluster to the orchestrator rule')
}

// --- every cluster is labelled ----------------------------------------------
{
  const fresh = { host: '\u2014', project: '\u2014', slug: 'brand-new', state: 'running' }
  const clusters = groupRuns([
    run('a'),
    { host: 'gitlab.example.com', project: 'team/widget', slug: 'i', state: 'running' },
    fresh,
  ])
  for (const cluster of clusters) {
    assert.ok(cluster.label, `cluster ${cluster.key} has no label`)
    assert.notEqual(cluster.label, '\u2014', `cluster ${cluster.key} is labelled with a dash`)
    assert.equal(cluster.label.trim(), cluster.label)
  }
  const unnamed = clusters.find((c) => c.runs.includes(fresh))
  assert.equal(unnamed.label, UNIDENTIFIED_LABEL)
}

// --- groups gather clusters, and only a real group draws a ring -------------
{
  const clusters = groupRuns([
    { host: 'h', project: 'team/widget', slug: 'a', state: 'running' },
    { host: 'h', project: 'team/service', slug: 'b', state: 'running' },
    { host: 'h', project: 'tools/team-site', slug: 'c', state: 'running' },
    { host: 'h', project: 'acme/widget', slug: 'd', state: 'running' },
  ], RULES)
  const groups = groupClusters(clusters)
  const team = groups.find((g) => g.label === 'Team')
  assert.ok(team, 'the Team rule gathered nothing')
  assert.equal(team.clusters.length, 3,
    'the three Team projects did not land in one group')

  const drawn = drawnGroups(clusters)
  assert.equal(drawn.length, 1, 'a one-project group drew a ring of its own')
  assert.equal(drawn[0].label, 'Team')

  // The ungrouped project is still its own group — it just does not draw a ring.
  const lone = groups.find((g) => g.clusters.length === 1)
  assert.ok(lone && lone.clusters[0].project === 'acme/widget')
}

// A rule named `Team` and a lone project literally called `Team` must not
// collide into one ring.
{
  const clusters = groupRuns(
    [
      { host: 'h', project: 'team/widget', slug: 'a', state: 'running' },
      { host: 'h', project: 'Team', slug: 'b', state: 'running' },
    ],
    [{ name: 'Team', patterns: ['team/'] }],
  )
  const keys = new Set(clusters.map((c) => c.group))
  assert.equal(keys.size, 2, 'a rule label collided with a cluster key')
}

// --- seeding puts a group's projects together, and leaves the middle free ----
{
  const clusters = seedPositions(groupRuns([
    { host: 'h', project: 'team/widget', slug: 'a', state: 'running' },
    { host: 'h', project: 'tools/team-site', slug: 'b', state: 'running' },
    { host: 'h', project: 'acme/widget', slug: 'c', state: 'running' },
  ], RULES), { width: 1000, height: 600 })
  assert.equal(clusters.length, 3, 'seeding dropped or invented a cluster')

  const at = (project) => clusters.find((c) => c.project === project)
  const near = (p, q) => Math.hypot(p.x - q.x, p.y - q.y)
  const ix = at('team/widget')
  const site = at('tools/team-site')
  const widget = at('acme/widget')
  assert.ok(near(ix, site) < near(ix, widget),
    'two projects in one group seeded further apart than an unrelated project')

  // Seeding fills the frame rather than ringing its middle. The first cut used a
  // circle of `min(width, height) * k`, which on a 1000x600 viewBox left the left
  // and right thirds empty while the clusters crowded — the map claims to show
  // the shape of a fleet at a glance, and dead space is the glance wasted.
  for (const cluster of clusters) {
    assert.ok(cluster.x > 0 && cluster.x < 1000, 'a cluster seeded off the frame')
    assert.ok(cluster.y > 0 && cluster.y < 600, 'a cluster seeded off the frame')
  }
}

// Eight groups have to use the frame's width, not a band down its middle.
{
  const rows = []
  for (let i = 0; i < 8; i += 1) {
    rows.push({ host: 'h', project: `p${i}/x`, slug: `s${i}`, state: 'running' })
  }
  const seeded = seedPositions(groupRuns(rows), { width: 1000, height: 600 })
  const xs = seeded.map((c) => c.x)
  const ys = seeded.map((c) => c.y)
  const spanX = Math.max(...xs) - Math.min(...xs)
  const spanY = Math.max(...ys) - Math.min(...ys)
  assert.ok(spanX > 520, `groups use only ${spanX.toFixed(0)}px of 1000px width`)
  assert.ok(spanY > 280, `groups use only ${spanY.toFixed(0)}px of 600px height`)
  for (const c of seeded) {
    assert.ok(c.x > 0 && c.x < 1000 && c.y > 0 && c.y < 600, 'a seed landed off-canvas')
  }
}

// A single group sits in the middle rather than on the spiral's first point.
{
  const seeded = seedPositions(groupRuns([run('a')]), { width: 1000, height: 600 })
  assert.equal(seeded.length, 1)
  assert.ok(Math.hypot(seeded[0].x - 500, seeded[0].y - 300) < 1,
    'a lone cluster was seeded off-centre')
}

// --- the back-compat relations fallback -------------------------------------
//
// One pair per relation, however many ways it arrives. The per-run route answers
// from both ends, so a naive concat would draw two springs on the same two nodes.
{
  const replies = [
    {
      run: { host: 'h', project: 'p', slug: 'a' },
      relations: [{ key: 'h/p/b' }, { key: 'h/p/c' }],
    },
    {
      run: { host: 'h', project: 'p', slug: 'b' },
      relations: [{ key: 'h/p/a' }],
    },
  ]
  const pairs = pairsFromReplies(replies)
  assert.equal(pairs.length, 2, 'the mirrored relation became two edges')
  const ids = pairs.map((p) => [p.a, p.b].sort().join('|')).sort()
  assert.deepEqual(ids, ['h/p/a|h/p/b', 'h/p/a|h/p/c'])
}

// A run related to itself, a reply with no run, a null entry: none of them is an
// edge, and none of them throws. These come off the wire.
{
  assert.deepEqual(pairsFromReplies([]), [])
  assert.deepEqual(pairsFromReplies(null), [])
  assert.deepEqual(pairsFromReplies([null, {}, { relations: [{ key: 'h/p/a' }] }]), [])
  assert.deepEqual(
    pairsFromReplies([{ run: { host: 'h', project: 'p', slug: 'a' }, relations: [{ key: 'h/p/a' }] }]),
    [],
    'a run related to itself became an edge',
  )
}

// An entry with parts but no composite key still becomes an edge — the route
// carries both, and a client should not depend on which.
{
  const pairs = pairsFromReplies([{
    run: { host: 'h', project: 'p', slug: 'a' },
    relations: [{ host: 'h', project: 'p', slug: 'b' }],
  }])
  assert.equal(pairs.length, 1)
  assert.deepEqual([pairs[0].a, pairs[0].b].sort(), ['h/p/a', 'h/p/b'])
}

// --- centroids --------------------------------------------------------------
{
  const nodes = [
    { cluster: 'a', x: 0, y: 0 },
    { cluster: 'a', x: 10, y: 0 },
    { cluster: 'b', x: 100, y: 100 },
    { cluster: null, x: 500, y: 300 },
    { cluster: 'b', x: Number.NaN, y: 4 },
  ]
  const centres = centroids(nodes, (node) => node.cluster)
  assert.equal(centres.size, 2, 'a null key or an unplaced node reached a ring')
  assert.deepEqual([centres.get('a').x, centres.get('a').y], [5, 0])
  assert.equal(centres.get('a').reach, 5)
  assert.equal(centres.get('b').n, 1, 'a node with no finite position was averaged in')
}

// --- where a label goes -----------------------------------------------------
//
// An SVG <text> outside the viewBox is not clipped, it is absent — so every one
// of these is a thing that looks fine until the fleet is big enough, and then
// silently loses the words the operator was reading.

// A ring small enough to have room above it keeps its label there.
{
  const ring = { label: 'acme/widget', x: 500, y: 300, r: 60 }
  assert.equal(ringLabelY(ring, 9, CLUSTER_LABEL_FLOOR), 231)
}

// A ring taller than the space above it puts its label inside rather than at a
// negative y — and never above the floor.
{
  const huge = { label: 'Team', x: 500, y: 300, r: 400 }
  const y = ringLabelY(huge, 15, GROUP_LABEL_FLOOR)
  assert.ok(y >= GROUP_LABEL_FLOOR, `a huge ring's label landed at ${y}`)
}

// The two layers cannot land on one line. This is the demo's `team/team`
// drawn through `Team`: a group near the top clamps, and clamps its own
// projects with it, so one floor puts both labels at the same y.
{
  const group = { label: 'Team', x: 500, y: 120, r: 200 }
  const child = { label: 'team/team', x: 500, y: 60, r: 40 }
  const groupY = ringLabelY(group, 15, GROUP_LABEL_FLOOR)
  const childY = ringLabelY(child, 9, CLUSTER_LABEL_FLOOR)
  assert.ok(groupY >= GROUP_LABEL_FLOOR && childY >= CLUSTER_LABEL_FLOOR)
  assert.ok(childY - groupY >= 12,
    `a group label at ${groupY} and its project's at ${childY} overlap`)
  // And the group stays above its projects, which is where it sits everywhere
  // else — a group label under its own project reads as belonging to it.
  assert.ok(groupY < childY, 'the group label dropped below its project label')
}

// Both floors on one geometry, so the separation is a property of the floors
// rather than of the two rings happening to differ.
{
  const same = { label: 'x', x: 500, y: 0, r: 500 }
  assert.ok(ringLabelY(same, 9, CLUSTER_LABEL_FLOOR) - ringLabelY(same, 9, GROUP_LABEL_FLOOR) >= 12,
    'the two label layers share a floor, so they can be drawn over each other')
}

// Horizontally: a ring against an edge keeps its whole label on the frame.
{
  const left = { label: 'tools/team-site', x: 8, y: 300, r: 60 }
  const x = ringLabelX(left, 6.7, 1000)
  assert.ok(x - (left.label.length * 6.7) / 2 >= 0, `label runs off the left at x=${x}`)

  const right = { label: 'tools/team-site', x: 995, y: 300, r: 60 }
  const rx = ringLabelX(right, 6.7, 1000)
  assert.ok(rx + (right.label.length * 6.7) / 2 <= 1000, `label runs off the right at x=${rx}`)

  // A ring with room keeps its centre.
  assert.equal(ringLabelX({ label: 'a', x: 500, y: 300, r: 10 }, 6.7, 1000), 500)
}

// The hover block's first line is the run's NAME, so an unfloored block drops
// exactly the line the operator hovered to read.
{
  assert.equal(detailY({ y: 300 }, 3), 300 - 15 - 22)
  const high = detailY({ y: 20 }, 3)
  assert.ok(high >= DETAIL_FLOOR, `a hovered star near the top starts at ${high}`)
  // One line needs less headroom than three, and must not be pushed down for it.
  assert.ok(detailY({ y: 60 }, 1) > detailY({ y: 60 }, 3))
}

console.log('all assertions passed')
