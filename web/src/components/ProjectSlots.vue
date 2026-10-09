<script setup>
import { computed } from 'vue'
import { mdiCogOutline, mdiServerOutline } from '@mdi/js'
import { projectColor } from '../projects.js'
import { slotIsFree, slotMeta, toneColor } from '../format.js'
// By the row key, not the slug: two uid rows of one group share a slug.
import { runKey } from '../runref.js'

const props = defineProps({
  slots: { type: Array, default: () => [] },
  runs: { type: Array, default: () => [] },
  projects: { type: Array, default: () => [] },
  project: { type: Object, default: null },
  loaded: Boolean,
  problem: { type: String, default: null },
})
defineEmits(['manage'])

function holder(held, via = null) {
  const run = (held.session_id && props.runs.find(row => row.session?.id === held.session_id))
    || props.runs.find(row => held.host && held.project && held.slug
      && runKey(row) === runKey(held))
  const project = props.projects.find(row => row.id === run?.workspace_id)
  return { session: held.session_id, via, project, name: project?.name || run?.workspace_name || 'Unassigned project' }
}

const rows = computed(() => props.slots.map(slot => {
  const holders = [
    ...(slot.occupants || (slot.occupant ? [slot.occupant] : [])).map(held => holder(held)),
    ...(slot.service_occupants || []).map(held => holder(held, held.slot)),
  ]
  const assigned = !!props.project && slot.applicable_projects?.includes(props.project.id)
  const heldHere = !!props.project && holders.some(held => held.project?.id === props.project.id)
  const heldDirectly = heldHere && holders.some(held => held.project?.id === props.project.id && !held.via)
  const available = assigned && !props.project.archived && !slot.needs_project_assignment && slotIsFree(slot) && !holders.length
  const meta = slotMeta(slot.state)
  return { ...slot, holders, assigned, heldHere, heldDirectly, available,
    label: available ? 'Available now' : heldDirectly ? 'Held by this project' : holders.length ? 'In use' : meta.label,
    color: available ? 'success' : toneColor(meta.tone) }
}).filter(slot => props.project ? slot.assigned || slot.heldHere : slot.holders.length || slot.state === 'occupied'))

const empty = computed(() => !props.slots.length ? 'No service slots configured.'
  : props.project ? `No service slots held by or assigned to ${props.project.name}.`
    : 'No service slots are currently held.')
</script>

<template>
  <div class="project-slots mb-4" role="region" aria-label="Service slots">
    <div class="d-flex flex-wrap align-center ga-2 mb-2">
      <h2 class="text-title-medium"><v-icon :icon="mdiServerOutline" size="20" class="me-1" />Service slots</h2>
      <v-spacer />
      <v-btn v-if="project" variant="text" size="small" :prepend-icon="mdiCogOutline"
        href="#platform-service-slots" @click.prevent="$emit('manage')">Manage in Platform settings</v-btn>
    </div>
    <v-alert v-if="problem" type="warning" density="compact">{{ problem }}</v-alert>
    <p v-else-if="!loaded" class="text-body-small text-medium-emphasis">Loading service slots…</p>
    <template v-else>
      <p v-if="!rows.length" class="text-body-small text-medium-emphasis">{{ empty }}</p>
      <v-card v-for="slot in rows" :key="slot.name" :aria-label="`Service slot ${slot.name}`"
        class="project-slot mb-2" variant="outlined">
        <v-card-text class="py-2">
          <div class="d-flex flex-wrap align-center ga-2">
            <span class="text-body-large font-weight-medium slot-name">{{ slot.name }}</span>
            <v-chip size="small" variant="tonal" :color="slot.color" class="slot-availability">{{ slot.label }}</v-chip>
            <span v-if="slot.service_group || slot.service" class="text-body-small text-medium-emphasis">
              {{ slot.service_group ? `Group: ${slot.service_group}` : slot.service }}
            </span>
          </div>
          <div v-for="held in slot.holders" :key="`${held.session}:${held.via || ''}`"
            class="slot-holder d-flex flex-wrap align-center ga-1 mt-1 text-body-small">
            <span>{{ held.via ? 'Service held by' : 'Held by' }}</span>
            <v-chip v-if="held.project" :color="projectColor(held.project.color)" variant="tonal" size="small">{{ held.name }}</v-chip>
            <span v-else>{{ held.name }}</span>
            <span v-if="held.via">via slot {{ held.via }}</span>
          </div>
          <p v-if="slot.state === 'occupied' && !slot.holders.length" class="text-body-small mt-1">Holder information unavailable.</p>
          <p v-if="project && slot.assigned && !slot.available" class="text-body-small text-medium-emphasis mt-1">Unavailable for a new run.</p>
          <p v-if="project && slot.heldHere && !slot.assigned" class="text-body-small text-medium-emphasis mt-1">{{ slot.heldDirectly ? 'Held allocation retained; not assigned for new runs.' : 'Service in use by this project; slot not assigned for new runs.' }}</p>
          <p v-if="slot.reason" class="text-body-small text-medium-emphasis mt-1">{{ slot.reason }}</p>
        </v-card-text>
      </v-card>
    </template>
  </div>
</template>

<style scoped>
.project-slot { overflow-wrap: anywhere; }
.project-slot .v-chip { height: auto; min-height: 24px; white-space: normal; }
.project-slots > div > .v-btn { height: auto; min-height: 32px; max-width: 100%; }
.project-slots :deep(.v-btn__content) { white-space: normal; }
</style>
