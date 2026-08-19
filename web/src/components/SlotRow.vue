<script setup>
// One declared service slot, as a row under the fleet's runs (#245).
//
// Deliberately not a RunCard: a slot is a fixture of the host rather than a
// thing that needs a human, so it carries no attention stripe and no forget.
// The one navigation it offers is the run holding it.
import { computed } from 'vue'
import {
  mdiAlertCircleOutline,
  mdiAlertOutline,
  mdiCheckboxBlankCircleOutline,
  mdiCircleSlice8,
} from '@mdi/js'

import { slotMeta, toneColor } from '../format.js'

const props = defineProps({
  row: { type: Object, required: true },
  // Passed in rather than fetched: the shell already has the list, and a second
  // copy is how the two start disagreeing.
  runs: { type: Array, default: () => [] },
})

const emit = defineEmits(['open'])

// Names, not the icon objects, so the mapping stays in format.js where it can
// be read under plain Node — @mdi/js is not importable there.
const ICONS = {
  'blank-circle': mdiCheckboxBlankCircleOutline,
  circle: mdiCircleSlice8,
  alert: mdiAlertOutline,
  'alert-circle': mdiAlertCircleOutline,
}

const meta = computed(() => slotMeta(props.row.state))
const icon = computed(() => ICONS[meta.value.icon] || mdiAlertOutline)

// Every live session holding the slot, not just the first: naming one holder
// would make the row that answers "who has my dev service" confidently wrong in
// exactly the case that matters.
const occupants = computed(() => props.row.occupants || [])
const contended = computed(() => occupants.value.length > 1)

// Sessions holding this slot's *service* under a different slot name. A row can
// be occupied by these alone, with nothing under its own name.
const serviceOccupants = computed(() => props.row.service_occupants || [])

// Every holder this row should name, paired with its tracked run where this
// orchestrator has one. A session spawned against a repo the daemon could not
// identify is never tracked, so `run` is legitimately null on a genuinely
// occupied row, and the row names the session instead of linking to nothing.
const holders = computed(() => [
  ...occupants.value.map((held) => ({ held, slot: null })),
  // Carries the slot it was claimed under: "held by X" is confusing on a row X
  // is not, so the answer is the session *and* which slot it took.
  ...serviceOccupants.value.map(({ slot, ...held }) => ({ held, slot })),
].map((entry) => ({
  ...entry,
  run: props.runs.find(
    (run) => run.host === entry.held.host
      && run.project === entry.held.project
      && run.slug === entry.held.slug,
  ) || null,
})))
</script>

<template>
  <v-card class="mb-2" variant="outlined">
    <v-card-text class="py-2">
      <div class="d-flex flex-wrap align-center ga-2">
        <!-- Tonal, like the run row's state chip: it is the verdict, and
             everything beside it is a fact read after it. The icon rides on the
             chip because colour alone is not a signal in this app. -->
        <v-chip
          :color="toneColor(meta.tone)"
          :prepend-icon="icon"
          size="small"
          variant="tonal"
        >{{ meta.label }}</v-chip>

        <span class="text-body-large font-weight-medium">{{ row.name }}</span>

        <!-- Colourless and outlined: identity, not urgency — the rule the run
             row's launch chips follow. A group slot names the group it holds
             (#312): its `service` is only where the session starts, and may be
             absent entirely. -->
        <v-chip v-if="row.service_group" size="small" variant="outlined">
          group: {{ row.service_group }}
        </v-chip>
        <v-chip v-else-if="row.service" size="small" variant="outlined">
          {{ row.service }}
        </v-chip>
      </div>

      <div
        v-if="row.description"
        class="text-body-small text-medium-emphasis mt-1"
      >{{ row.description }}</div>

      <!-- A link when this orchestrator tracks the run, the bare session id when
           it does not: an occupied slot whose holder cannot be opened is still
           an answer, and a dead link would be a worse one. -->
      <div v-if="holders.length" class="text-body-small mt-1">
        <!-- Named out loud: two live sessions in one slot means two agents
             running against one dev service, and the claim is not atomic, so
             this row is where that becomes visible. -->
        <span v-if="contended" class="text-error font-weight-medium">
          held by {{ occupants.length }} sessions at once —
        </span>
        <span v-else>held by</span>
        <template v-for="(entry, index) in holders" :key="entry.held.session_id">
          <span v-if="index">, </span>
          <a
            v-if="entry.run"
            href="#"
            @click.prevent="emit('open', entry.run)"
          >{{ entry.run.title || entry.run.label || entry.held.slug }}</a>
          <span v-else class="text-medium-emphasis">
            session {{ entry.held.session_id }} (not tracked here)
          </span>
          <span v-if="entry.slot" class="text-medium-emphasis">
            (via slot {{ entry.slot }})
          </span>
        </template>
      </div>

      <!-- Why it cannot be used, in the daemon's own words. Rendered for a slot
           that is broken *and* occupied too: "in use" is what is true now, and
           this is what will still be wrong when the session ends. -->
      <div
        v-if="row.reason"
        class="text-body-small text-medium-emphasis mt-1 scroll-x"
      >{{ row.reason }}</div>
    </v-card-text>
  </v-card>
</template>
