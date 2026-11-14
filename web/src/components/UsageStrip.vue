<script setup>
// The fleet's AI usage over a window, and each account window's headroom
// (issue 315). Its own route on its own timer; absent renders nothing.
// Why: docs/USAGE-TRACKING.md, "How each surface reads".
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { fetchUsage } from '../api.js'
import { bucketParts, costLabel, costTitle, headroomView } from '../usage.js'

const props = defineProps({
  // The shell's clock, so "resets in" ages with every other time on the page.
  now: { type: Number, default: () => Date.now() },
})

//: The windows the daemon answers for, shortest first and the first the default.
const WINDOWS = ['1h', '24h', '7d']
//: The collector's own default tick: reading faster re-reads the same numbers.
const USAGE_POLL_MS = 30000

const usageWindow = ref(WINDOWS[0])
const reply = ref(null)
let poll = null
// Which request is current, so a reply for a window the operator has since left
// cannot land on top of the one they chose.
let sequence = 0

async function load() {
  const mine = ++sequence
  try {
    const answer = await fetchUsage(usageWindow.value)
    if (mine === sequence) reply.value = answer
  } catch {
    // An unreachable daemon is the fleet view's error to report, and an older one
    // has no such route: either way the strip keeps what it had, or stays away.
  }
}

watch(usageWindow, load)
onMounted(() => {
  load()
  poll = setInterval(load, USAGE_POLL_MS)
})
onUnmounted(() => clearInterval(poll))

const shown = computed(() => !!reply.value?.enabled)
const parts = computed(() => bucketParts(reply.value?.totals))
const cost = computed(() => costLabel(reply.value?.cost))
const notCollected = computed(() => reply.value?.sessions?.not_collected || 0)
const headroom = computed(() => (reply.value?.headroom || []).map((entry) => ({
  key: `${entry.harness}/${entry.operator}/${entry.window}`,
  ...headroomView(entry, props.now),
})))
const collectorError = computed(() => reply.value?.collector?.last_error || null)
</script>

<template>
  <section v-if="shown" aria-label="AI usage" class="usage-strip mb-3">
    <div class="d-flex flex-wrap align-center ga-3 text-body-small">
      <v-btn-toggle v-model="usageWindow" density="compact" mandatory variant="text" aria-label="usage window">
        <v-btn v-for="value in WINDOWS" :key="value" :value="value" size="small">{{ value }}</v-btn>
      </v-btn-toggle>
      <span
        v-for="part in parts"
        :key="part.key"
        class="usage-bucket"
        :title="part.title"
      ><span class="text-medium-emphasis">{{ part.label }}</span> {{ part.text }}</span>
      <span v-if="cost" class="usage-cost" :title="costTitle(reply.cost)">cost {{ cost }}</span>
      <span
        v-if="notCollected"
        class="usage-not-collected text-medium-emphasis"
        title="sessions whose transcripts the collector cannot read (standalone runs, or a harness it does not parse); their records still reach the rollups"
      >{{ notCollected }} not collected</span>
    </div>

    <!-- Outlined and colourless unless a window is close to full, which is the
         one thing here that is a warning; a stale reading is faded rather than
         hidden, because "we last knew 43%" is still worth knowing. -->
    <div v-if="headroom.length" class="d-flex flex-wrap ga-2 mt-2">
      <v-chip
        v-for="chip in headroom"
        :key="chip.key"
        class="usage-headroom"
        :class="{ 'usage-stale': chip.stale, 'usage-warn': chip.warn }"
        :color="chip.warn ? 'warning' : undefined"
        :title="chip.title"
        size="small"
        variant="outlined"
      >{{ chip.text }}<template v-if="chip.fills"> · {{ chip.fills }}</template></v-chip>
    </div>

    <p v-if="collectorError" class="usage-collector-error text-body-small text-medium-emphasis mt-2 mb-0">
      usage collector: {{ collectorError }}
    </p>
  </section>
</template>

<style scoped>
.usage-stale {
  opacity: 0.55;
}
</style>
