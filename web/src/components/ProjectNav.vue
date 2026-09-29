<script setup>
import { computed, ref } from 'vue'
import { mdiChevronDown, mdiChevronRight, mdiRobot } from '@mdi/js'
import { projectColor } from '../projects.js'
import RunNav from './RunNav.vue'

const props = defineProps({
  project: { type: Object, required: true },
  runs: { type: Array, default: () => [] },
  attention: { type: Array, default: () => [] },
  selected: Boolean,
  currentKey: { type: String, default: null },
  now: { type: Number, default: () => Date.now() },
})
defineEmits(['chat', 'open'])
const expanded = ref(true)
const status = computed(() => {
  const assistant = props.project.assistant
  const lifecycle = (assistant?.lifecycle || 'unavailable').replaceAll('_', ' ')
  return assistant?.queue_position ? `${lifecycle} · queue ${assistant.queue_position}` : lifecycle
})
const statusColor = computed(() => ({
  running: 'success', waiting: 'warning', starting: 'warning', rotating: 'warning',
  stopping: 'warning', failed: 'error',
})[props.project.assistant?.lifecycle])
const chatLabel = computed(() => `Chat with ${props.project.name} — assistant ${status.value}`)
</script>

<template>
  <section class="project-nav" :class="{ 'project-nav-selected': selected }"
    :aria-label="project.name" :style="{ borderInlineStartColor: projectColor(project.color) }">
    <div class="project-nav-header">
      <button type="button" class="project-toggle" :aria-expanded="expanded"
        :aria-controls="`project-runs-${project.id}`" :aria-label="`${expanded ? 'Collapse' : 'Expand'} ${project.name}`"
        :title="project.name"
        @click="expanded = !expanded">
        <v-icon :icon="expanded ? mdiChevronDown : mdiChevronRight" size="18" />
        <span class="project-name">{{ project.name }}</span>
        <span v-if="attention.length" class="project-attention" :aria-label="`${attention.length} need you`">{{ attention.length }}</span>
      </button>
      <v-btn class="project-assistant" variant="tonal" size="small" :color="statusColor"
        :aria-label="chatLabel" :title="chatLabel" @click="$emit('chat', project)">
        <v-icon :icon="mdiRobot" size="18" />
        <span>{{ status }}</span>
      </v-btn>
    </div>
    <div v-show="expanded" :id="`project-runs-${project.id}`">
      <RunNav :runs="runs" :attention="attention" :current-key="currentKey" :now="now" @open="$emit('open', $event)" />
    </div>
  </section>
</template>

<style scoped>
.project-nav {
  border-inline-start: 3px solid;
  border-block-end: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
}
.project-nav-selected > .project-nav-header {
  background: rgba(var(--v-theme-on-surface), .06);
}
.project-nav-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px;
}
.project-toggle {
  appearance: none;
  border: 0;
  background: transparent;
  color: inherit;
  font: inherit;
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 4px;
  flex: 1;
  min-width: 0;
  min-height: 40px;
  text-align: start;
  border-radius: 4px;
}
.project-toggle:hover {
  background: rgba(var(--v-theme-on-surface), .06);
}
.project-toggle:focus-visible {
  outline: 2px solid currentColor;
  outline-offset: 2px;
}
.project-toggle .v-icon {
  flex-shrink: 0;
}
.project-name {
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  overflow: hidden;
  overflow-wrap: anywhere;
}
.project-attention {
  font-size: .75rem;
  font-weight: 600;
}
.project-assistant {
  flex-shrink: 0;
  max-width: 104px;
  min-height: 40px;
  height: auto;
  padding: 6px 8px;
}
.project-assistant :deep(.v-btn__content) {
  gap: 4px;
  white-space: normal;
  overflow-wrap: anywhere;
}
</style>
