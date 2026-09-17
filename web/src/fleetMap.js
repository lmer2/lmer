// The constellation map's layout, minus the drawing (#141 fleet map).
//
// Everything here is pure and DOM-free, which is the point: the map's hard parts
// are not the SVG, they are the three questions below, and all three are
// answerable — and testable — without a browser. `FleetMap.vue` renders what this
// decides, and `tests/test_platform_web_fleetmap.js` executes it under node.
//
// 1. What is a cluster, and which runs are in it.
// 2. What survives a poll. The fleet re-polls every ten seconds; a layout that
//    started over each time would re-scatter the map while it is being read, so
//    the reconcile below keeps node identity — and with it position and
//    velocity — across polls, and reports whether anything actually changed.
// 3. What paints last. An attention run that a calm run draws over is a run the
//    operator does not see, which defeats the whole view.
//
// What is deliberately NOT here is colour. A node's tone comes from `format.js`'s
// `stateMeta`/`toneColor`, the same pair the run cards use, so there is exactly
// one table in this app mapping a run state to a colour.

import { runKey } from './runref.js'

// The force model, as data rather than as a call sequence, so the component
// configures d3-force from one place and these numbers can be read — and
// argued with — without reading the rendering code.
//
// `linkStrengthCross` being well under `linkStrengthSame` is not a tuning
// preference, it is what keeps the clusters legible: a relation between two
// projects pulls on both clusters, and at equal strength a handful of
// cross-project relations drag every project into one smear. A relation between
// projects should hint, not haul. (Found by building the map and looking at it,
// not by reasoning about it.)
export const FORCE = {
  // Raised with the group ring (round 3). At 0.022 a cluster was a suggestion:
  // fourteen runs across eight projects drifted past the viewBox edges and their
  // labels landed on each other, which is exactly the "shape of my fleet at a
  // glance" the view exists for, not delivered. Found the same way the numbers
  // below were — by rendering a realistic fleet headlessly and looking at it.
  clusterStrength: 0.08,
  chargeStrength: -140,
  linkDistance: 68,
  linkStrengthSame: 0.2,
  linkStrengthCross: 0.035,
  collideRadius: 26,
  // Below this, d3-force stops on its own. The map is meant to settle and hold
  // still: a node that keeps drifting is a node that moves out from under the
  // cursor aiming at it.
  alphaMin: 0.005,
  // What a poll that changed the graph re-heats to. Enough to re-place what
  // arrived, not enough to throw the whole map back into motion.
  reheatAlpha: 0.3,
}

// Ticks in a solve run by hand. Enough for the model above to reach `alphaMin`
// from a re-heat, which is what "settled" means here.
export const SETTLE_STEPS = 320

// Solve now, without animating: stop the ticker and run the model by hand, so
// the caller's next paint shows the finished layout rather than the first frame
// of one.
export function settle(simulation, steps = SETTLE_STEPS) {
  simulation.stop()
  for (let i = 0; i < steps; i += 1) simulation.tick()
}

// Re-heat, then run. Both ways of running are here because the re-heat is the
// part neither may skip, and skipping it is invisible: every d3 force scales by
// alpha, so ticking a simulation that has already decayed to `alphaMin` applies
// forces at ~6e-4 and moves nothing at all. The map looks fine — it simply
// stopped laying anything out.
//
// That is what made this worth taking out of the component. `instant` is the
// reduced-motion path, and it is not "do less", it is "do the same solve, now":
// an arrival on a later poll that is never re-heated keeps whatever position d3
// seeded it with, and for a run in a cluster the map has never seen there is no
// centroid to seed from — d3 falls back to its spiral around the origin, which
// is above the viewBox. An operator with reduced motion enabled would watch
// every newly-spawned run vanish from the map.
export function reheat(simulation, { instant = false, steps = SETTLE_STEPS } = {}) {
  simulation.alpha(FORCE.reheatAlpha)
  if (instant) {
    settle(simulation, steps)
    return
  }
  simulation.restart()
}

// Transcribed from lmer_platform.inventory.ATTENTION_PRIORITY. Lower is more
// urgent. Kept in step with that table — the map must not rank attention
// differently from the list that ranks it today.
export const ATTENTION_PRIORITY = {
  live_question: 0,
  question: 1,
  feedback: 2,
  yield: 3,
  cap_reached: 4,
  stalled: 5,
  stuck_teardown: 5,
  critical_error: 6,
  crashed: 7,
  slot_contention: 8,
  unreadable: 9,
}

// An attention reason this bundle has not heard of still counts as attention —
// a daemon newer than this bundle is what produces one, and the safe direction
// is to draw it as wanting the operator rather than as calm.
const UNKNOWN_ATTENTION_RANK = 50

export function attentionRank(run) {
  const reason = run && run.attention && run.attention.reason
  if (!reason) return null
  const rank = ATTENTION_PRIORITY[reason]
  return rank === undefined ? UNKNOWN_ATTENTION_RANK : rank
}

// What `lmer_platform.inventory` writes into the identity fields of a row built
// from a session that has not committed a run identity yet. Transcribed rather
// than imported, like ATTENTION_PRIORITY above: it is a *rendering* choice over
// there, and the map has to recognise it so it does not draw a dash as though it
// were a project name.
const UNIDENTIFIED = '\u2014'

//: The label a cluster carries when its runs have no project to name it by.
export const UNIDENTIFIED_LABEL = 'unidentified'

// A field that is really there. Empty string, `null` and the daemon's dash all
// mean "nothing to show" and every one of them has reached this module.
function named(value) {
  const text = value === null || value === undefined ? '' : String(value).trim()
  return text && text !== UNIDENTIFIED ? text : null
}

// Which runs draw inside one ring, as a *list of rules* rather than as a rule.
//
// Deliberately not a namespace prefix. The operator's own example spans two
// namespaces — `fullctl/ixctl`, `fullctl/peerctl` and `syseng/fullctl-site` are
// one group — so "the first path segment" is the wrong shape: it would split the
// group it is meant to draw. A regex over the project path holds all three.
//
// First match wins, so order is precedence. A project matching no rule is its own
// group and draws exactly as it did before rules existed, which is what keeps a
// fleet nobody has written rules for looking untouched.
//
// **This constant is the seam.** Every function below takes its rules as an
// argument defaulting to this list, and a `pattern` may be a string as readily as
// a RegExp — so the later slice (rules stored per operator and edited in the UI)
// changes where the list comes from and nothing about how it is applied. That
// slice is not built here.
//
// **A decision that slice must not re-open: an operator-typed pattern has to be
// escaped into a literal substring, not compiled as a regex.** `groupRuns` runs
// on every poll and every render, and a regex is not a safe thing to accept from
// a text field — `(a+)+$` against a 31-character path backtracks for *minutes*,
// measured, and the `try`/`catch` below cannot help because catastrophic
// backtracking does not throw, it spins. That is the fleet view gone for the
// session. Rules the *code* declares stay regexes; they are reviewed like code.
// Unreachable today, which is exactly why it is settled here rather than left to
// be discovered by the slice that makes it live.
//
// One boundary for all three rules, rather than each rule inventing its own.
// `(^|[/-])name([/-]|$)` treats `/` and `-` alike, which is what the operator's
// own example needs: `syseng/fullctl-site` is a fullctl project and a
// segment-only anchor would drop it. What that boundary costs is stated plainly
// because it is not obvious from reading the list — see the table in
// `tests/test_platform_web_fleetmap.js`: it rejects `notfullctl/x`, and it still
// admits `fullctl-archive/notes`. It has to. `fullctl-site` and
// `fullctl-archive` are the same shape — a segment beginning `fullctl-` — so no
// pattern can keep the first and drop the second, and naming the group is a
// judgement only the operator can make.
export const GROUP_RULES = [
  { label: 'fullctl', pattern: /(^|[/-])fullctl([/-]|$)/i },
  { label: 'peeringdb', pattern: /(^|[/-])peeringdb([/-]|$)/i },
  { label: 'agents', pattern: /(^|[/-])agents([/-]|$)/i },
]

// A rule's pattern as something safe to `test` with.
//
// `g` and `y` make `test` stateful — the same rule would match every *other* call
// — and a rule is operator data one day, so the flags are dropped rather than the
// rule rejected. A string is accepted because that is what an operator types.
function toRegExp(pattern) {
  if (pattern instanceof RegExp) {
    const flags = pattern.flags.replace(/[gy]/g, '')
    return flags === pattern.flags ? pattern : new RegExp(pattern.source, flags)
  }
  if (typeof pattern === 'string' && pattern.trim()) {
    try {
      return new RegExp(pattern, 'i')
    } catch {
      return null
    }
  }
  return null
}

// The first rule a project path matches, or `null` for a project that is its own
// group. A malformed rule costs its own rule and never the map: one bad pattern
// in an operator's list must not stop the fleet from drawing.
export function groupRuleFor(project, rules = GROUP_RULES) {
  const path = named(project)
  if (!path) return null
  for (const rule of rules || []) {
    if (!rule || !rule.label) continue
    const pattern = toRegExp(rule.pattern)
    if (!pattern) continue
    try {
      if (pattern.test(path)) return rule
    } catch {
      continue
    }
  }
  return null
}

// The platform's own session, which is not a run in a project and is not in a
// cluster at all — it draws as one standalone node.
//
// Read off `orchestrator`, which the daemon puts on the row from the registry
// `kind` (`inventory.RunView.orchestrator`), and never inferred from the dashes
// the row's identity fields carry: those dashes mean "no identity yet", which is
// also true of a worker in its first seconds, and badging that worker as the
// platform itself is the mistake the daemon's own docstring warns about.
export function isOrchestrator(run) {
  return !!(run && run.orchestrator)
}

// A run's cluster is its project on its host — the same pair the fleet groups by
// everywhere else. Not the slug's prefix, which is a naming convention and not a
// fact about the run.
//
// `null` for the orchestrator, which is what keeps it out of every cluster, every
// group and every ring below.
export function clusterKey(run) {
  if (!run || isOrchestrator(run)) return null
  return `${run.host}/${run.project}`
}

// Clusters in a stable order, so the same fleet lays out the same way twice and
// a reviewer comparing two screenshots is comparing the change, not the
// iteration order.
//
// Each cluster carries the two things drawing it needs: the `label` it shows —
// never a dash and never empty, because an unlabelled ring is a ring the operator
// cannot act on — and the `group` it shares with any other project a rule puts
// beside it.
export function groupRuns(runs, rules = GROUP_RULES) {
  const clusters = new Map()
  for (const run of runs || []) {
    const key = clusterKey(run)
    if (!key) continue
    if (!clusters.has(key)) {
      const project = named(run.project)
      const rule = groupRuleFor(project, rules)
      clusters.set(key, {
        key,
        host: run.host,
        project: run.project,
        label: project || UNIDENTIFIED_LABEL,
        // Namespaced so a rule called `agents` and a lone project called
        // `agents` cannot collide into one ring.
        group: rule ? `rule:${rule.label}` : `cluster:${key}`,
        groupLabel: rule ? rule.label : project || UNIDENTIFIED_LABEL,
        runs: [],
      })
    }
    clusters.get(key).runs.push(run)
  }
  return [...clusters.values()].sort((a, b) => (a.key < b.key ? -1 : a.key > b.key ? 1 : 0))
}

// Clusters gathered into their groups, in the clusters' own order.
export function groupClusters(clusters) {
  const groups = new Map()
  for (const cluster of clusters || []) {
    const existing = groups.get(cluster.group)
    if (existing) {
      existing.clusters.push(cluster)
      continue
    }
    groups.set(cluster.group, {
      key: cluster.group,
      label: cluster.groupLabel,
      clusters: [cluster],
    })
  }
  return [...groups.values()]
}

// The groups worth drawing a ring around. A group holding one project *is* that
// project, and a second ring with a second label around it is noise rather than
// structure — so the map looks exactly as it did before rules existed until a
// rule actually gathers something.
export function drawnGroups(clusters) {
  return groupClusters(clusters).filter((group) => group.clusters.length > 1)
}

// Centres of mass and the reach around them, for whatever key the caller groups
// by. One implementation because the map draws two ring layers from the same
// arithmetic, and two copies of it would be two chances to disagree about where
// a ring sits.
export function centroids(nodes, keyOf) {
  const sums = new Map()
  for (const node of nodes || []) {
    const key = keyOf(node)
    if (key === null || key === undefined) continue
    if (!Number.isFinite(node.x) || !Number.isFinite(node.y)) continue
    const entry = sums.get(key) || { x: 0, y: 0, n: 0 }
    entry.x += node.x
    entry.y += node.y
    entry.n += 1
    sums.set(key, entry)
  }
  const centres = new Map()
  for (const [key, entry] of sums) {
    if (!entry.n) continue
    const x = entry.x / entry.n
    const y = entry.y / entry.n
    let reach = 0
    for (const node of nodes || []) {
      if (keyOf(node) !== key) continue
      if (!Number.isFinite(node.x) || !Number.isFinite(node.y)) continue
      reach = Math.max(reach, Math.hypot(node.x - x, node.y - y))
    }
    centres.set(key, { x, y, reach, n: entry.n })
  }
  return centres
}

// Seed positions. Each *group* gets a place, and the projects inside a group a
// smaller ring around it — which is what makes a group read as one region with
// its projects still legible inside, rather than as a relabelled cluster.
//
// Groups are placed on a **phyllotaxis spiral fitted to the frame**, not on a
// circle. A circle of radius `min(width, height) * k` is what the first cut did,
// and on a 1000x600 viewBox it used the middle third and left the left and right
// thirds empty while the clusters themselves crowded. The spiral spreads by
// `sqrt(i / n)`, which is the distribution that puts equal *area* around each
// group, and the separate x/y radii let it use a wide frame as wide.
//
// Deterministic on purpose — a random seed means the map composes differently on
// every reload, which reads as instability rather than as a fresh layout. The
// force simulation moves everything from here; this only decides where the
// settling starts.
const GOLDEN_ANGLE = Math.PI * (3 - Math.sqrt(5))

export function seedPositions(clusters, { width = 1000, height = 600 } = {}) {
  const cx = width / 2
  const cy = height / 2
  const spreadX = width * 0.38
  const spreadY = height * 0.36
  const innerRadius = Math.min(width, height) * 0.1
  const groups = groupClusters(clusters)
  const placed = new Map()
  groups.forEach((group, index) => {
    // A lone group sits in the middle; the spiral's first point does not.
    const reach = groups.length > 1 ? Math.sqrt((index + 0.5) / groups.length) : 0
    const angle = index * GOLDEN_ANGLE
    const gx = cx + Math.cos(angle) * reach * spreadX
    const gy = cy + Math.sin(angle) * reach * spreadY
    const spread = group.clusters.length > 1 ? innerRadius : 0
    group.clusters.forEach((cluster, position) => {
      const inner = (position / Math.max(group.clusters.length, 1)) * Math.PI * 2 - Math.PI / 2
      placed.set(cluster.key, {
        ...cluster,
        x: gx + Math.cos(inner) * spread,
        y: gy + Math.sin(inner) * spread,
      })
    })
  })
  // Returned in the caller's order, not the grouping's: the caller sorted these.
  return clusters.map((cluster) => placed.get(cluster.key)).filter(Boolean)
}

// The fleet's orchestrator row, for the app bar that stands in for it now that
// the map does not draw it. `null` when this payload has none — an older daemon,
// or a fleet whose supervisor is not running.
export function orchestratorOf(runs) {
  for (const run of runs || []) if (isOrchestrator(run)) return run
  return null
}

// Edge pairs out of the per-run relation replies the back-compat fallback reads
// (see `FleetMap`'s `relations` prop). One pair per relation however many ways it
// is spelled: the per-run route answers from both ends, so every edge arrives
// twice and a naive concat would draw two springs pulling the same two nodes.
export function pairsFromReplies(replies) {
  const pairs = []
  const seen = new Set()
  for (const reply of replies || []) {
    const subject = reply && reply.run ? runKey(reply.run) : null
    if (!subject) continue
    for (const relation of (reply && reply.relations) || []) {
      const other = relation && (relation.key || runKey(relation))
      if (!other || other === subject) continue
      const id = subject < other ? `${subject}|${other}` : `${other}|${subject}`
      if (seen.has(id)) continue
      seen.add(id)
      pairs.push({ a: subject, b: other })
    }
  }
  return pairs
}

// --- where a label goes ------------------------------------------------------
//
// Pure geometry, here rather than in the component for the reason everything else
// in this file is here: it is decided arithmetic, it has been wrong twice, and it
// is invisible when it is wrong. An SVG `<text>` outside the viewBox is not
// clipped or overflowed — it is simply absent, at every fleet size, with nothing
// to notice.

//: Top floors for the two ring-label layers, a full line apart.
//
// Both layers clamp to the top of the frame, and a group sitting near the top
// clamps its own projects there too — so with one floor the group's label and a
// project's label land on the same line and overlay each other (seen on the demo:
// `fullctl/fullctl` drawn through `fullctl`). A group's label belongs above its
// projects', which is where it sits everywhere else, so the floors say what the
// geometry already says.
export const GROUP_LABEL_FLOOR = 14
export const CLUSTER_LABEL_FLOOR = GROUP_LABEL_FLOOR + 17

//: Line height of the hover detail block, and its own top floor.
export const DETAIL_LINE = 11
export const DETAIL_FLOOR = 14

// Above the ring, unless the ring is large enough that "above it" is off the top
// of the map — a label clamped to the edge names nothing, which is the failure
// this whole layer is drawn from node positions to avoid. Then it sits just
// inside the ring instead, and never above `floor`.
export function ringLabelY(ring, gap, floor = CLUSTER_LABEL_FLOOR) {
  const outside = ring.y - ring.r - gap
  if (outside >= floor) return outside
  return Math.max(floor, Math.min(ring.y - ring.r + gap + 10, ring.y - 10))
}

// And horizontally. Same failure in the other axis: a ring pushed against the
// left or right edge centres its label past the viewBox and the text is cut,
// which on the widest labels (a full `host/project`) loses the half that says
// which project it is. Width is estimated from the character count because the
// map never measures text — these are monospace faces, so the estimate is close
// enough to keep a label inside its own frame.
export function ringLabelX(ring, charWidth, width = 1000) {
  const half = ((ring.label || '').length * charWidth) / 2
  return Math.min(Math.max(ring.x, half + 4), width - half - 4)
}

// Where a hovered star's detail block starts. The first of its lines is the run's
// *name* — the line the operator hovered to read — so an unfloored block drops
// exactly that one for a node near the top, leaving the state and the project:
// the two things they could already infer from the colour and the ring.
export function detailY(node, lines) {
  return Math.max(DETAIL_FLOOR, node.y - 15 - (lines - 1) * DETAIL_LINE)
}

// The graph the simulation runs on. Edges join to nodes by run key, and an edge
// naming a run that is not in this fleet is dropped: it has no node to attach
// to. The daemon already drops those (`_fold_relations`), so this is the second
// half of the same contract rather than a duplicate — the payload can be older
// than the run list it arrives with.
export function buildGraph(runs, relations) {
  const nodes = []
  const byKey = new Map()
  for (const run of runs || []) {
    // The platform's own session is not on the map at all. It is one tap away in
    // the app bar, which is where it belongs: the map answers "what is my fleet
    // doing", and the thing asking that question is not one of the answers.
    if (isOrchestrator(run)) continue
    const key = runKey(run)
    if (!key || byKey.has(key)) continue
    const node = {
      key,
      run,
      cluster: clusterKey(run),
      attention: attentionRank(run),
    }
    nodes.push(node)
    byKey.set(key, node)
  }
  const links = []
  const seen = new Set()
  for (const pair of relations || []) {
    const a = pair && pair.a
    const b = pair && pair.b
    if (!a || !b || a === b) continue
    const source = byKey.get(a)
    const target = byKey.get(b)
    if (!source || !target) continue
    // The store is symmetric and keyed once, but a payload is data from
    // elsewhere: two spellings of one pair would otherwise become two springs
    // pulling the same two nodes.
    const id = a < b ? `${a}|${b}` : `${b}|${a}`
    if (seen.has(id)) continue
    seen.add(id)
    links.push({ id, source, target, cross: source.cluster !== target.cluster })
  }
  return { nodes, links, byKey }
}

// The poll diff. Keeps the node objects the simulation already holds — which is
// what preserves x/y/vx/vy — updates their run data in place, seeds arrivals at
// their cluster's current centre of mass rather than at a random point, and
// says whether the graph's *shape* changed. That last value is what decides
// whether to re-heat: a poll where only a state changed must not move anything.
export function reconcile(previous, runs, relations) {
  const kept = new Map()
  const previousKeys = previous && previous.byKey ? previous.byKey : new Map()
  const fresh = buildGraph(runs, relations)

  let added = 0
  for (const node of fresh.nodes) {
    const existing = previousKeys.get(node.key)
    if (existing) {
      existing.run = node.run
      existing.cluster = node.cluster
      existing.attention = node.attention
      kept.set(node.key, existing)
    } else {
      added += 1
      kept.set(node.key, node)
    }
  }

  const removed = [...previousKeys.keys()].filter((key) => !kept.has(key)).length

  // An arrival starts at its own cluster's centre of mass, so it appears among
  // its project and settles outward instead of flying across the map. Through
  // the same `centroids` the rings are drawn from — one implementation, so an
  // arrival cannot be seeded somewhere the ring around it does not cover. It
  // also means a node with no cluster (the orchestrator) is not seeded from the
  // average of every other unclustered node: it is placed, not averaged.
  if (added) {
    const centres = centroids([...kept.values()], (node) => node.cluster)
    for (const node of kept.values()) {
      if (Number.isFinite(node.x)) continue
      const centre = centres.get(node.cluster)
      if (centre) {
        node.x = centre.x
        node.y = centre.y
      }
    }
  }

  const nodes = [...kept.values()]
  const links = fresh.links.map((link) => ({
    ...link,
    source: kept.get(link.source.key) || link.source,
    target: kept.get(link.target.key) || link.target,
  }))

  const previousLinkIds = new Set((previous && previous.links ? previous.links : []).map((l) => l.id))
  const linksChanged = links.length !== previousLinkIds.size
    || links.some((link) => !previousLinkIds.has(link.id))

  return {
    nodes,
    links,
    byKey: kept,
    // The one question the caller actually asks: is this worth re-heating for?
    changed: added > 0 || removed > 0 || linksChanged,
    added,
    removed,
  }
}

// Calm first, attention last, and inside attention the most urgent last of all —
// so the star the operator most needs to see is the one drawn over its
// neighbours rather than under them. A stable tiebreak on key keeps the order
// from shuffling between polls.
export function paintOrder(nodes) {
  return [...(nodes || [])].sort((a, b) => {
    const aAttention = a.attention !== null && a.attention !== undefined
    const bAttention = b.attention !== null && b.attention !== undefined
    if (aAttention !== bAttention) return aAttention ? 1 : -1
    if (aAttention && bAttention && a.attention !== b.attention) {
      // Lower rank number is more urgent, so it must come later.
      return b.attention - a.attention
    }
    return a.key < b.key ? -1 : a.key > b.key ? 1 : 0
  })
}
