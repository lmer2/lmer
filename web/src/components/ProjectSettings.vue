<script setup>
import { ref, watch } from 'vue'
import { archiveProject, fetchAssistantInstructions, setAssistantInstructions, updateProject } from '../api.js'
import { projectColor, projectColors } from '../projects.js'

const props = defineProps({ project: { type: Object, required: true } })
const emit = defineEmits(['changed', 'close'])
const form = ref({})
const orders = ref(null)
const instructions = ref('')
const savedInstructions = ref('')
const ordersOpen = ref(undefined)
const error = ref(null)
const busy = ref(false)
const overrideKeys = ['model', 'harness', 'preset', 'agents']
const overrideLabels = { model: 'Model', harness: 'Harness', preset: 'Preset', agents: 'Fan-out presets' }

watch(() => props.project.id, async (id, _previous, onCleanup) => {
  let current = true
  onCleanup(() => { current = false })
  orders.value = null
  instructions.value = ''
  savedInstructions.value = ''
  ordersOpen.value = undefined
  error.value = null
  form.value = { name: props.project.name, color: props.project.color,
    repositories: (props.project.repositories || []).join('\n'), settings: { ...props.project.settings } }
  try {
    const loaded = await fetchAssistantInstructions(id)
    if (!current) return
    orders.value = loaded
    instructions.value = savedInstructions.value = loaded.instructions || ''
  } catch (exc) { if (current) error.value = exc.message }
}, { immediate: true })

async function save() {
  const id = props.project.id
  busy.value = true
  error.value = null
  try {
    if (orders.value && instructions.value !== savedInstructions.value) {
      orders.value = await setAssistantInstructions(id, instructions.value)
      instructions.value = savedInstructions.value = orders.value.instructions || ''
    }
    await updateProject(id, { name: form.value.name, color: form.value.color,
      repositories: form.value.repositories.split('\n').map(value => value.trim()).filter(Boolean),
      settings: Object.fromEntries(Object.entries(form.value.settings).map(([key, value]) => [key, value || null])) })
    emit('changed')
  } catch (exc) { error.value = exc.message } finally { busy.value = false }
}

async function archive() {
  busy.value = true
  error.value = null
  try { await archiveProject(props.project.id); emit('changed'); emit('close') }
  catch (exc) { error.value = exc.message } finally { busy.value = false }
}
</script>

<template>
  <v-card title="Project settings" :subtitle="project.name" class="project-settings">
    <v-card-actions class="project-actions">
      <v-btn :loading="busy" color="primary" variant="tonal" @click="save">Save project</v-btn>
      <v-spacer /><v-btn :disabled="busy" @click="emit('close')">Close</v-btn>
      <v-btn color="warning" :disabled="busy" @click="archive">Archive project</v-btn>
    </v-card-actions>
    <v-card-text>
      <v-alert v-if="error" type="error" class="mb-3">{{ error }}</v-alert>
      <v-text-field v-model="form.name" label="Project name" :disabled="busy" maxlength="120" />
      <v-select v-model="form.color" :items="Object.keys(projectColors)" label="Project color" :disabled="busy">
        <template #item="{ internalItem: item, props: itemProps }"><v-list-item v-bind="itemProps"><template #prepend><v-chip variant="flat" :color="projectColor(item.value)" size="small" class="me-2" aria-hidden="true">●</v-chip></template></v-list-item></template>
        <template #selection="{ internalItem: item }"><v-chip variant="flat" :color="projectColor(item.value)">{{ item.value }}</v-chip></template>
      </v-select>
      <v-textarea v-model="form.repositories" label="Repositories (one URL per line)" :disabled="busy" rows="2" />
      <p class="mb-3">Execution overrides apply at the next start or rotation. Empty fields inherit platform defaults.</p>
      <v-text-field v-for="key in overrideKeys" :key="key" v-model="form.settings[key]" :label="overrideLabels[key]"
        hint="Empty inherits the platform default." persistent-hint class="mb-2" :disabled="busy" />
      <v-combobox v-model="form.settings.threads" label="Project thread connections" multiple chips :disabled="busy" />
      <v-expansion-panels v-model="ordersOpen" class="project-orders">
        <v-expansion-panel title="Standing instructions">
          <v-expansion-panel-text>
            <v-textarea v-model="instructions" label="Standing instructions" :disabled="busy || !orders"
              :maxlength="orders?.limit" :counter="orders?.limit" rows="6" />
            <p>Saved with this project. Each new assistant incarnation reads these instructions.</p>
          </v-expansion-panel-text>
        </v-expansion-panel>
      </v-expansion-panels>
    </v-card-text>
  </v-card>
</template>

<style scoped>
.project-settings { overflow: visible; }
.project-actions { position: sticky; top: var(--v-layout-top, 64px); z-index: 1; flex-wrap: wrap; background: rgb(var(--v-theme-surface)); }
</style>
