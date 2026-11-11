<script setup>
import { computed } from 'vue'
import { mdiClockOutline, mdiTimerOutline, mdiTimerSyncOutline } from '@mdi/js'
import { runningAccent } from '../projects.js'

// The header's clock (#668). The time line, the chips and the controls are the bar's;
// the clock opens the bar while no timer runs, and while one runs (the bar cannot
// be hidden then) it opens and closes the bar's edit view. Running, it wears the mode
// icon in an accent derived from the header's tint so it reads on any project colour.
// With the bar hidden, a badge counts the warnings and prompts it would show.
// `hint` replaces it when the operator credential acts for nobody.
const props = defineProps({
  status: { type: Object, default: null },
  hint: { type: String, default: null },
  open: Boolean,
  editing: Boolean,
  tint: { type: String, default: null },
})
const emit = defineEmits(['toggle', 'edit'])

const running = computed(() => props.status?.running || null)
const waiting = computed(() => (props.status?.warnings?.length || 0) + (props.status?.prompts?.length || 0))
const accent = computed(() => (running.value && props.tint ? runningAccent(props.tint) : null))
const label = computed(() => {
  if (running.value) {
    const mode = running.value.mode === 'manual' ? 'manual' : 'automatic'
    return `${props.editing ? 'close the timer editor' : 'edit the running timer'} · tracking ${mode}`
  }
  const count = !props.open && waiting.value ? ` · ${waiting.value} waiting` : ''
  return `${props.open ? 'hide' : 'show'} time tracking${count}`
})
const expanded = computed(() => (running.value ? props.editing : props.open))

function click() {
  emit(running.value ? 'edit' : 'toggle')
}
</script>

<template>
  <span v-if="hint" class="time-hint text-body-small" :title="hint">time: no default person</span>
  <div v-else-if="status" class="time-indicator">
    <v-tooltip :text="label" location="bottom" open-on-focus>
      <template #activator="{ props: tooltip }">
        <v-badge :model-value="!running && !open && waiting > 0" :content="waiting" color="warning"
          floating offset-x="6" offset-y="6" class="time-badge">
          <v-btn v-bind="tooltip" class="time-toggle" :class="{ 'time-toggle-running': running }"
            :icon="running ? (running.mode === 'manual' ? mdiTimerOutline : mdiTimerSyncOutline) : mdiClockOutline"
            :variant="running ? 'flat' : open ? 'tonal' : 'text'"
            :color="running && !accent ? 'primary' : undefined"
            :style="accent ? { backgroundColor: accent.ground, color: accent.ink } : undefined"
            :aria-label="label" :aria-expanded="expanded" aria-controls="time-tracking-bar" @click="click" />
        </v-badge>
      </template>
    </v-tooltip>
  </div>
</template>

<style scoped>
.time-indicator {
  display: flex;
  align-items: center;
  margin-inline-end: 4px;
}
.time-hint {
  white-space: nowrap;
  opacity: .8;
  margin-inline-end: 4px;
}
</style>
