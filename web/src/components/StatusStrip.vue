<script>
import { ref } from 'vue'

const usageDetails = ref()
</script>

<script setup>
// The harness's own status line, mirrored into the platform (issue 248). One
// component for all three surfaces, in two densities; nothing here fetches, so
// the strip refreshes with whatever view it sits in. The density, monochrome
// and freshness decisions are in docs/SESSION-STATUS.md ("How the UI renders
// it").
import { computed } from 'vue'

import { resetsLabel, statusAge, statusSegments } from '../format.js'
import { rememberFlag, storedFlag } from '../preferences.js'

const props = defineProps({
  // The `statusline` block off a run row or the assistant reply. Null is the
  // ordinary case — a dormant run, a harness that publishes nothing — and
  // renders the whole strip away.
  status: { type: Object, default: null },
  // The shell's clock, as everywhere else in this app: a component reading
  // Date.now() itself drifts from the ages beside it.
  now: { type: Number, default: () => Date.now() },
  compact: { type: Boolean, default: false },
})

const USAGE_DETAILS_STORAGE_KEY = 'lmer:usage-details'
const details = usageDetails
if (details.value === undefined) {
  details.value = storedFlag(() => window.localStorage.getItem(USAGE_DETAILS_STORAGE_KEY), false)
}
const hasDetails = computed(() => props.status?.harness === 'codex'
  && statusSegments(props.status, { details: true }).some((each) => each.detailOnly))
const segments = computed(() => statusSegments(props.status, { details: details.value }))
const age = computed(() => statusAge(props.status, segments.value))

function toggleDetails() {
  details.value = !details.value
  rememberFlag((value) => window.localStorage.setItem(USAGE_DETAILS_STORAGE_KEY, value), details.value)
}

// The compact line: every segment, and its age only when there is one to give.
const printed = computed(() => {
  const line = segments.value.map((each) => each.text).join(' · ')
  return age.value ? `${line} (${age.value})` : line
})

// When the reading was taken, on every density: the row shows no timestamp of
// its own, and a percentage with no date behind it cannot be checked.
const reading = computed(() => props.status?.harness === 'codex'
  ? segments.value.map((segment) => title(segment)).join(' · ')
  : props.status?.updated || '')

function title(segment) {
  return [resetsLabel(segment.resets_at, props.now), segment.updated || props.status?.updated,
    segment.provenance, segment.detail, segment.sourceAge, segment.note]
    .filter(Boolean).join(' — ')
}
</script>

<template>
  <!-- Unstyled and inline in the row's facts line, so it inherits that line's
       size and emphasis rather than declaring its own. The age joins the same
       string — a separate element for three words would wrap on a phone. -->
  <span
    v-if="compact && segments.length"
    :title="reading"
  >{{ printed }}</span>

  <div v-else-if="segments.length || (!compact && hasDetails)" class="d-flex flex-wrap align-center ga-2">
    <div
      v-for="segment in segments"
      :key="segment.key"
      class="status-segment"
      :class="{ 'status-detail': segment.detailOnly }"
      :title="title(segment)"
    >
      <div class="d-flex justify-space-between text-body-small">
        <span class="text-medium-emphasis">{{ segment.label }}</span>
        <span class="font-weight-medium">{{ segment.value }}</span>
      </div>
      <div v-if="segment.age" class="text-body-small text-medium-emphasis">{{ segment.age }}</div>
      <!-- `tone-idle` is this app's own neutral, so a meter cannot be mistaken
           for the urgency ramp. A segment that is not a proportion (effort)
           draws no meter at all rather than a full one. -->
      <v-progress-linear
        v-if="segment.percent !== undefined"
        :model-value="segment.percent"
        color="tone-idle"
        height="4"
        rounded
      />
    </div>
    <button
      v-if="hasDetails"
      type="button"
      class="text-body-small text-medium-emphasis usage-details"
      :aria-pressed="details"
      :aria-label="details ? 'Hide usage details' : 'Show usage details'"
      @click.stop="toggleDetails"
    >{{ details ? 'less' : 'details' }}</button>
    <!-- Its own line under the meters, because it qualifies all of them. -->
    <div
      v-if="age"
      class="text-body-small text-medium-emphasis w-100"
      :title="reading"
    >{{ age }}</div>
  </div>
  <button
    v-if="compact && hasDetails"
    type="button"
    class="text-body-small text-medium-emphasis usage-details"
    :aria-pressed="details"
    :aria-label="details ? 'Hide usage details' : 'Show usage details'"
    @click.stop="toggleDetails"
  >{{ details ? 'less' : 'details' }}</button>
</template>

<style scoped>
/* Wide enough for `100%` beside the label and narrow enough that four of them
   wrap onto a phone rather than compressing into unreadable slivers. */
.status-segment {
  min-width: 5.5rem;
  flex: 1 1 5.5rem;
  max-width: 12rem;
}
.usage-details {
  margin-inline-start: 0.5rem;
  background: transparent;
  border: 0;
  text-decoration: underline;
}
.status-detail {
  flex-basis: 14rem;
  min-width: min(100%, 14rem);
  max-width: 24rem;
  overflow-wrap: anywhere;
}
.status-detail > .d-flex {
  flex-wrap: wrap;
  gap: 0.25rem;
}
</style>
