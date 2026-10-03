<script setup>
// The fleet drawn as a constellation (#141 fleet map).
//
// Runs are stars, clustered by project; a line between two of them is a relation
// this orchestrator holds. It answers one question the list answers slowly —
// "what is the shape of my fleet right now, and what wants me" — and it hands
// every other question back to the list and the run view.
//
// Three things about how this is built, because each one is a decision rather
// than an implementation detail:
//
// - **The layout is not here.** `fleetMap.js` holds the clustering, the poll
//   diff and the paint order, DOM-free, so the parts that are genuinely hard to
//   get right are executed by a test under node rather than eyeballed in a
//   browser. This file draws what that decides.
// - **d3-force computes positions and nothing else.** It never touches the DOM;
//   Vue owns every element. That is what lets the map take the app's theme and
//   be keyboard-navigable, which a canvas would have cost.
// - **Colour comes from `format.js`.** The same `stateMeta`/`toneColor` pair the
//   run cards use, so there is one table in this app that says what a run state
//   looks like. A private palette here would quietly re-decide, for instance,
//   that `detached` is healthy.

import { computed, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import {
  forceCollide, forceLink, forceManyBody, forceSimulation, forceX, forceY,
} from 'd3-force'
import { useTheme } from 'vuetify'
import { stateMeta, toneColor } from '../format.js'
import { runKey } from '../runref.js'
import {
  CLUSTER_LABEL_FLOOR, DETAIL_LINE, FORCE, GROUP_LABEL_FLOOR, buildGraph,
  centroids, detailY, drawnGroups, groupRuns, paintOrder, reconcile, reheat,
  ringLabelX, ringLabelY, seedPositions, settle,
} from '../fleetMap.js'

const props = defineProps({
  runs: { type: Array, default: () => [] },
  relations: { type: Array, default: () => [] },
  groups: { type: Array, default: () => [] },
})

const emit = defineEmits(['open'])

// The drawing surface is a fixed viewBox scaled by CSS, so the layout maths is
// resolution-independent and the SVG stays responsive without re-running the
// simulation on every resize.
const VIEW_WIDTH = 1000
const VIEW_HEIGHT = 600

const NODE_RADIUS = 5.4
const GLOW_RADIUS = 8.5
const HALO_RADIUS = 13

// The tones the map paints, and what each one means to an operator reading it.
//
// This *is* a second list — `format.js`'s `TONE_COLORS` is the first — and the
// wording here used to claim the legend could not fall behind it, which nothing
// enforced. It is enforced now:
// `test_the_legend_names_every_tone_format_js_can_produce` compares these keys
// against `TONE_COLORS`, the same way the attention ranking is compared against
// the daemon's table rather than against a copy of itself. A sixth tone in
// `format.js` fails that test until it is named here.
//
// The swatches resolve through `toneColor` as the nodes do, so a tone that is
// listed also cannot be drawn in the legend a different colour from the node.
const LEGEND = [
  ['live', 'running'],
  ['attention', 'wants you'],
  ['bad', 'failed or crashed'],
  ['idle', 'held, parked, dormant'],
  ['done', 'complete'],
]

// A browser that will not answer is treated as "no preference": the map
// animating for somebody who asked it not to is the failure that matters, but
// so is a map that never settles because the query threw.
const prefersReducedMotion = () => {
  try {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches
  } catch {
    return false
  }
}

const graph = shallowRef({ nodes: [], links: [], byKey: new Map() })
const clusters = ref([])
const hovered = ref(null)
// Bumped on every tick so the template re-reads the node positions d3 mutates in
// place. The nodes are deliberately not reactive themselves — making a few
// hundred simulation objects reactive would re-run the dependency tracker sixty
// times a second for no gain.
const frame = ref(0)

let simulation = null
let hiddenWhileChanged = false

const orderedNodes = computed(() => {
  frame.value
  return paintOrder(graph.value.nodes)
})

const visibleLinks = computed(() => {
  frame.value
  return graph.value.links
})

// The palette, resolved once per theme rather than once per node per frame.
//
// `toneColor` hands back a colour *name* (`success`, `tone-idle`), which is
// correct for every other caller in this app because every other caller gives it
// to Vuetify — `:color` on a chip, a `text-…` class — and Vuetify resolves it.
// These values go into SVG `fill`/`stroke` attributes instead, where a name is
// not a paint: the attribute is invalid and the node falls back to black. So the
// resolving that Vuetify does for the run cards has to happen here.
//
// Reading `theme.current` in a computed is also what repaints the map when the
// operator switches scheme: the value is reactive, so the template re-renders
// with the other theme's colours instead of holding the ones it mounted with.
const theme = useTheme()
const themeColors = computed(() => theme.current.value.colors)

function toneOf(run) {
  const name = toneColor(stateMeta(run.state).tone)
  // A tone this theme does not define still has to paint something visible —
  // the same call `format.js` makes for an unmapped tone. `currentColor` borrows
  // the surface's ink rather than introducing a second palette here.
  return themeColors.value[name] || 'currentColor'
}

// What to call a run on the map — its title when this orchestrator has one, the
// way every other listing in this app names a run.
function nameOf(run) {
  return run.title || run.name || run.slug
}

function labelFor(node) {
  const meta = stateMeta(node.run.state)
  return `${nameOf(node.run)} — ${meta.label}`
}

// What a hovered star says. The mockup's three lines: what the run is called,
// what state it is in, and whose project it belongs to — a star that only gives
// back a slug makes the operator open the run to learn what they hovered for.
function detailFor(node) {
  const meta = stateMeta(node.run.state)
  const cluster = clusterFor(node.cluster)
  const lines = [nameOf(node.run), meta.label]
  if (cluster && cluster.label) lines.push(cluster.label)
  return lines
}

const legend = computed(() => LEGEND.map(([tone, meaning]) => ({
  tone, meaning, paint: themeColors.value[toneColor(tone)] || 'currentColor',
})))

// The ring layers, both drawn from where the nodes actually are rather than from
// their seed points: a label floating away from the region it names is worse
// than no label. One helper for both, so the two layers cannot disagree about
// where a centre is.
function ringsBy(keyOf, labelOf, pad) {
  const placed = []
  for (const [key, centre] of centroids(graph.value.nodes, keyOf)) {
    placed.push({ key, label: labelOf(key), x: centre.x, y: centre.y, r: centre.reach + pad })
  }
  return placed
}

function clusterFor(key) {
  return clusters.value.find((candidate) => candidate.key === key)
}

const clusterRings = computed(() => {
  frame.value
  return ringsBy(
    (node) => node.cluster,
    (key) => {
      const cluster = clusterFor(key)
      return cluster ? cluster.label : key
    },
    30,
  )
})

// Only a group that actually gathers more than one project draws a ring. Until a
// rule gathers something the map looks exactly as it did before there were any
// rules, which is what keeps this from being a second label on every cluster.
const groupRings = computed(() => {
  frame.value
  const groups = drawnGroups(clusters.value)
  if (!groups.length) return []
  const labels = new Map(groups.map((group) => [group.key, group.label]))
  return ringsBy(
    (node) => {
      const cluster = clusterFor(node.cluster)
      const key = cluster ? cluster.group : null
      return labels.has(key) ? key : null
    },
    // A wider pad than a cluster ring's, so the group reads as the region the
    // clusters sit *in* and its label has room to clear theirs.
    (key) => labels.get(key) || key,
    70,
  )
})

function build() {
  clusters.value = seedPositions(groupRuns(props.runs, props.groups), {
    width: VIEW_WIDTH, height: VIEW_HEIGHT,
  })
  const next = buildGraph(props.runs, props.relations)
  for (const node of next.nodes) {
    const cluster = clusters.value.find((candidate) => candidate.key === node.cluster)
    if (cluster) {
      node.x = cluster.x
      node.y = cluster.y
    }
  }
  graph.value = next
  start(true)
}

function applyForces() {
  // A node whose cluster has gone (a payload mid-change) falls back to the
  // middle rather than to the origin, which is off-canvas.
  const centreOf = (node) => clusters.value.find((c) => c.key === node.cluster)
  simulation
    .nodes(graph.value.nodes)
    .force('x', forceX((node) => (centreOf(node) || { x: VIEW_WIDTH / 2 }).x)
      .strength(FORCE.clusterStrength))
    .force('y', forceY((node) => (centreOf(node) || { y: VIEW_HEIGHT / 2 }).y)
      .strength(FORCE.clusterStrength))
    .force('charge', forceManyBody().strength(FORCE.chargeStrength))
    .force('collide', forceCollide(FORCE.collideRadius))
    .force('link', forceLink(graph.value.links)
      .id((node) => node.key)
      .distance(FORCE.linkDistance)
      // The number with an argument behind it: a relation across projects pulls
      // far more weakly than one inside a project, or a handful of them drag
      // every cluster into a single smear and the grouping is gone.
      .strength((link) => (link.cross ? FORCE.linkStrengthCross : FORCE.linkStrengthSame)))

}

function start(fresh) {
  if (!simulation) {
    simulation = forceSimulation()
      .alphaMin(FORCE.alphaMin)
      .on('tick', () => { frame.value += 1 })
  }
  applyForces()

  if (prefersReducedMotion()) {
    // Solve and paint once. No animation at all, which is an accessibility
    // obligation rather than a performance measure. No re-heat needed on this
    // call: a simulation this young is still at alpha 1.
    settle(simulation)
    frame.value += 1
    return
  }

  if (fresh) {
    // Compose before the first paint, so the map appears laid out rather than
    // assembling itself while it is watched.
    settle(simulation)
  }
  simulation.alpha(FORCE.reheatAlpha).restart()
}

// A change in the shape of the fleet, applied whichever way the operator asked
// the map to move. Both paths re-heat — see `reheat` in fleetMap.js for why the
// instant one must too.
function relayout() {
  applyForces()
  reheat(simulation, { instant: prefersReducedMotion() })
  frame.value += 1
}

function refresh() {
  if (!simulation) {
    build()
    return
  }
  const next = reconcile(graph.value, props.runs, props.relations)
  graph.value = next
  const nextClusters = seedPositions(groupRuns(props.runs, props.groups), {
    width: VIEW_WIDTH, height: VIEW_HEIGHT,
  })
  const regrouped = nextClusters.some((cluster, index) => cluster.group !== clusters.value[index]?.group)
  clusters.value = nextClusters
  frame.value += 1
  if (!next.changed && !regrouped) return
  // Only a change in the *shape* of the fleet is worth moving the map for. A
  // poll that changed nothing but a run's state must not re-heat: a node that
  // shifts under the cursor is a node the operator misses when they click.
  if (document.hidden) {
    hiddenWhileChanged = true
    return
  }
  relayout()
}

function onVisibility() {
  if (!simulation) return
  if (document.hidden) {
    // A tab animating in the background is work nobody asked for — and under
    // reduced motion, a solve nobody is looking at is the same waste, so both
    // paths defer to the return below rather than running here.
    simulation.stop()
    return
  }
  if (hiddenWhileChanged) {
    hiddenWhileChanged = false
    relayout()
  }
}

watch(() => [props.runs, props.relations, props.groups], refresh, { deep: false })

onMounted(() => {
  build()
  document.addEventListener('visibilitychange', onVisibility)
})

onBeforeUnmount(() => {
  document.removeEventListener('visibilitychange', onVisibility)
  if (simulation) simulation.stop()
})

function activate(node) {
  emit('open', node.run)
}
</script>

<template>
  <div class="fleet-map">
    <svg
      :viewBox="`0 0 ${VIEW_WIDTH} ${VIEW_HEIGHT}`"
      role="group"
      :aria-label="`Fleet map — ${graph.nodes.length} runs in ${clusterRings.length} projects`"
    >
      <defs>
        <filter id="fleet-star-glow" x="-160%" y="-160%" width="420%" height="420%">
          <feGaussianBlur stdDeviation="5" result="blurred" />
          <feMerge>
            <feMergeNode in="blurred" />
            <feMergeNode in="SourceGraphic" />
          </feMerge>
        </filter>
      </defs>

      <!-- Groups, outside the clusters they gather. Drawn only where a rule
           actually gathered more than one project, so a fleet nobody has written
           rules for renders exactly the rings it always did. -->
      <g aria-hidden="true">
        <circle
          v-for="ring in groupRings"
          :key="`group-ring-${ring.key}`"
          class="fleet-map__group-ring"
          :cx="ring.x"
          :cy="ring.y"
          :r="ring.r"
        />
        <text
          v-for="ring in groupRings"
          :key="`group-label-${ring.key}`"
          class="fleet-map__group"
          :x="ringLabelX(ring, 8.7)"
          :y="ringLabelY(ring, 15, GROUP_LABEL_FLOOR)"
          text-anchor="middle"
        >{{ ring.label }}</text>
      </g>

      <!-- Clusters. Decoration for a sighted reader: every run they contain is
           named on its own node, so this is hidden rather than read twice. -->
      <g aria-hidden="true">
        <circle
          v-for="ring in clusterRings"
          :key="`ring-${ring.key}`"
          class="fleet-map__ring"
          :cx="ring.x"
          :cy="ring.y"
          :r="ring.r"
        />
        <text
          v-for="ring in clusterRings"
          :key="`label-${ring.key}`"
          class="fleet-map__cluster"
          :x="ringLabelX(ring, 6.7)"
          :y="ringLabelY(ring, 9, CLUSTER_LABEL_FLOOR)"
          text-anchor="middle"
        >{{ ring.label }}</text>
      </g>

      <g aria-hidden="true">
        <line
          v-for="link in visibleLinks"
          :key="link.id"
          class="fleet-map__edge"
          :x1="link.source.x"
          :y1="link.source.y"
          :x2="link.target.x"
          :y2="link.target.y"
        />
      </g>

      <!-- One group per run, in paint order: calm first, the run that most wants
           the operator last, so nothing draws over it. -->
      <g
        v-for="node in orderedNodes"
        :key="node.key"
        class="fleet-map__node"
        role="button"
        tabindex="0"
        :aria-label="labelFor(node)"
        @click="activate(node)"
        @keydown.enter.prevent="activate(node)"
        @keydown.space.prevent="activate(node)"
        @mouseenter="hovered = node.key"
        @mouseleave="hovered = null"
        @focus="hovered = node.key"
        @blur="hovered = null"
      >
        <circle
          v-if="node.attention !== null && node.attention !== undefined"
          class="fleet-map__halo"
          :cx="node.x"
          :cy="node.y"
          :r="HALO_RADIUS"
          :stroke="toneOf(node.run)"
        />
        <circle
          class="fleet-map__glow"
          :cx="node.x"
          :cy="node.y"
          :r="GLOW_RADIUS"
          :fill="toneOf(node.run)"
          filter="url(#fleet-star-glow)"
        />
        <circle
          class="fleet-map__core"
          :cx="node.x"
          :cy="node.y"
          :r="NODE_RADIUS"
          :fill="toneOf(node.run)"
        />
        <!-- Three lines on hover, the mockup's: what the run is called, what
             state it is in, whose project it is. A star that gives back only a
             slug makes the operator open the run to find out what they just
             hovered, which is the trip this view exists to save. -->
        <text
          v-if="hovered === node.key"
          class="fleet-map__name"
          :x="node.x"
          :y="detailY(node, detailFor(node).length)"
          text-anchor="middle"
        ><tspan
          v-for="(line, index) in detailFor(node)"
          :key="line"
          :x="node.x"
          :dy="index ? DETAIL_LINE : 0"
          :class="index ? 'fleet-map__detail' : null"
        >{{ line }}</tspan></text>
      </g>
    </svg>

    <!-- The legend rail. Without it the colour coding is a private language:
         every tone on the map is named here, and so is the one thing on the map
         that is not a colour. Built from the same table the nodes paint from, so
         a tone cannot reach the map without a line explaining it. -->
    <div v-if="graph.nodes.length" class="fleet-map__legend" aria-label="Map legend">
      <span v-for="item in legend" :key="item.tone" class="fleet-map__key">
        <span class="fleet-map__swatch" :style="{ background: item.paint }" />
        <b>{{ item.tone }}</b> {{ item.meaning }}
      </span>
      <span class="fleet-map__key">
        <span class="fleet-map__swatch fleet-map__swatch--ring" />
        <b>ring</b> the run is waiting on you
      </span>
      <span class="fleet-map__key">
        <span class="fleet-map__swatch fleet-map__swatch--edge" />
        <b>line</b> the two runs are related
      </span>
    </div>

    <p v-if="!graph.nodes.length" class="text-center text-medium-emphasis py-6">
      No runs to map yet.
    </p>
  </div>
</template>

<style scoped>
.fleet-map svg {
  display: block;
  width: 100%;
  height: auto;
}

/* Every colour here is a theme token, never a literal: the map has to be right
   in both schemes, and that is the property that chose SVG over a canvas. */
.fleet-map__ring {
  fill: none;
  stroke: rgb(var(--v-theme-on-surface));
  stroke-dasharray: 2 7;
  opacity: 0.18;
}

.fleet-map__group-ring {
  fill: none;
  stroke: rgb(var(--v-theme-on-surface));
  stroke-dasharray: 1 6;
  opacity: 0.1;
}

.fleet-map__group {
  font-family: ui-monospace, "SFMono-Regular", Menlo, monospace;
  font-size: 12.5px;
  letter-spacing: 0.1em;
  text-transform: lowercase;
  fill: rgb(var(--v-theme-on-surface));
  opacity: 0.42;
}

.fleet-map__cluster {
  font-family: ui-monospace, "SFMono-Regular", Menlo, monospace;
  font-size: 11px;
  letter-spacing: 0.06em;
  fill: rgb(var(--v-theme-on-surface));
  opacity: 0.62;
}

.fleet-map__edge {
  stroke: rgb(var(--v-theme-on-surface));
  stroke-width: 1;
  opacity: 0.22;
}

.fleet-map__node {
  cursor: pointer;
}

.fleet-map__node:focus {
  outline: none;
}

.fleet-map__node:focus-visible .fleet-map__core {
  stroke: rgb(var(--v-theme-primary));
  stroke-width: 2.5;
}

.fleet-map__core {
  stroke: rgb(var(--v-theme-surface));
  stroke-width: 1.2;
}

.fleet-map__glow {
  opacity: 0.5;
}

.fleet-map__halo {
  fill: none;
  stroke-width: 1.4;
  opacity: 0.55;
  animation: fleet-map-halo 2.4s ease-in-out infinite;
}

.fleet-map__legend {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 20px;
  align-items: center;
  padding: 10px 2px 2px;
  font-size: 12.5px;
  color: rgba(var(--v-theme-on-surface), 0.7);
}

.fleet-map__key {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  white-space: nowrap;
}

.fleet-map__key b {
  color: rgb(var(--v-theme-on-surface));
  font-weight: 500;
}

.fleet-map__swatch {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  display: inline-block;
}

.fleet-map__swatch--ring {
  background: none;
  border: 1.4px solid rgba(var(--v-theme-on-surface), 0.55);
  width: 11px;
  height: 11px;
}

.fleet-map__swatch--edge {
  border-radius: 0;
  height: 1px;
  width: 16px;
  background: rgba(var(--v-theme-on-surface), 0.45);
}

.fleet-map__detail {
  font-size: 9.5px;
  opacity: 0.72;
}

.fleet-map__name {
  font-family: ui-monospace, "SFMono-Regular", Menlo, monospace;
  font-size: 10.5px;
  fill: rgb(var(--v-theme-on-surface));
  pointer-events: none;
}

@keyframes fleet-map-halo {
  0%, 100% { opacity: 0.9; }
  50% { opacity: 0.25; }
}

/* The operator asked for no motion; that includes the one decorative loop. */
@media (prefers-reduced-motion: reduce) {
  .fleet-map__halo {
    animation: none;
  }
}
</style>
