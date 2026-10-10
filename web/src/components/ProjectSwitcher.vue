<script setup>
import { mergeProps } from 'vue'
import { mdiCogOutline, mdiMenuDown, mdiPlus } from '@mdi/js'
import { projectColor } from '../projects.js'

defineProps({
  projects: { type: Array, required: true },
  activeProject: { type: String, default: '' },
  label: { type: String, required: true },
  current: { type: Object, default: null },
  attention: { type: Function, required: true },
})
defineEmits(['choose', 'settings', 'create'])
</script>

<template>
  <v-menu location="bottom start">
    <template #activator="{ props: menu }">
      <v-tooltip :text="`project: ${label} — switch project`" location="bottom" open-on-focus>
        <template #activator="{ props: tooltip }">
          <v-btn v-bind="mergeProps(menu, tooltip)" class="project-switcher" variant="text" :append-icon="mdiMenuDown"
            :aria-label="`project: ${label} — switch project`">
            <span class="project-switcher-name">{{ label }}</span>
          </v-btn>
        </template>
      </v-tooltip>
    </template>
    <v-list :selected="[activeProject]" density="compact" class="project-switcher-list" aria-label="Projects">
      <v-list-item value="" title="All projects" @click="$emit('choose', '')" />
      <v-list-item v-for="project in projects" :key="project.id" :value="project.id" :title="project.name"
        @click="$emit('choose', project.id)">
        <template #prepend>
          <span class="project-switcher-dot" :style="{ background: projectColor(project.color) }" aria-hidden="true" />
        </template>
        <template v-if="attention(project.id).length" #append>
          <v-badge :content="attention(project.id).length" inline :aria-label="`${attention(project.id).length} need you`" />
        </template>
      </v-list-item>
      <v-divider class="my-1" />
      <v-list-item v-if="current" :prepend-icon="mdiCogOutline" :title="`Project settings for ${current.name}`"
        @click="$emit('settings')" />
      <v-list-item :prepend-icon="mdiPlus" title="New project" @click="$emit('create')" />
    </v-list>
  </v-menu>
</template>

<style scoped>
.project-switcher {
  min-width: 0;
  max-width: 100%;
  text-transform: none;
  letter-spacing: normal;
}

.project-switcher :deep(.v-btn__content) {
  min-width: 0;
}

.project-switcher-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.project-switcher-dot {
  display: inline-block;
  width: 12px;
  height: 12px;
  border-radius: 50%;
  margin-inline-end: 12px;
}
</style>
