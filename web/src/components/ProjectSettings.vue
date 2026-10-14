<script setup>
import { computed, nextTick, ref, watch } from 'vue'
import { archiveProject, fetchAssistantInstructions, setAssistantInstructions, updateProject } from '../api.js'
import { ago } from '../format.js'
import { projectColor, projectColors, projectTextColor, storedProjectColor } from '../projects.js'

// `focus` is `{ id, section, nonce }`; the nonce makes a repeat tap count.
const props = defineProps({ project: { type: Object, required: true }, focus: { type: Object, default: null },
  now: { type: Number, default: () => Date.now() } })
const emit = defineEmits(['changed', 'close', 'focused'])
const form = ref({})
const orders = ref(null)
const instructions = ref('')
const savedInstructions = ref('')
const ordersOpen = ref(undefined)
const ordersPanel = ref(null)
const error = ref(null)
const ordersError = ref(null)
const busy = ref(false)
// The picker speaks hex; the form keeps the stored spelling, preset name or hex.
const pickedColor = computed({
  get: () => projectColor(form.value.color),
  set: (hex) => { form.value.color = storedProjectColor(hex) },
})
// v-color-picker draws each inner array as a column: four columns of five, in palette order.
const swatches = Object.values(projectColors).reduce((rows, hex, index) => {
  if (index % 5 === 0) rows.push([])
  rows.at(-1).push(hex)
  return rows
}, [])
const colorName = computed(() => (Object.hasOwn(projectColors, form.value.color) ? form.value.color : pickedColor.value))
const overrideKeys = ['model', 'harness', 'preset', 'agents']
const overrideLabels = { model: 'Model', harness: 'Harness', preset: 'Preset', agents: 'Fan-out presets' }

// Each read or save of the orders takes a ticket, and only the latest one's reply lands:
// two quick taps, or a save during a read, would otherwise let the older text win.
let ordersTicket = 0
// A read waits out an orders save: a GET answered before the PUT would mark the saved
// text unsaved, and the next Save project would write it over newer orders.
let ordersSave = null
// The tap whose orders the project watcher's own read already covers.
let readForNonce

watch(() => props.project.id, (id) => {
  orders.value = null
  instructions.value = ''
  savedInstructions.value = ''
  ordersOpen.value = undefined
  error.value = null
  ordersError.value = null
  form.value = { name: props.project.name, color: props.project.color,
    repositories: (props.project.repositories || []).join('\n'), settings: { ...props.project.settings } }
  readForNonce = props.focus?.nonce
  readOrders(id)
}, { immediate: true })

// uber lmer rewrites the orders from the chat, so each tap re-reads them: saving a
// stale copy would overwrite the newer wording. An unsaved edit is kept.
async function readOrders(id) {
  await ordersSave?.catch(() => {})
  const ticket = ++ordersTicket
  try {
    const loaded = await fetchAssistantInstructions(id)
    if (ticket !== ordersTicket) return
    const edited = instructions.value !== savedInstructions.value
    orders.value = loaded
    savedInstructions.value = loaded.instructions || ''
    if (!edited) instructions.value = savedInstructions.value
    ordersError.value = null
  } catch (exc) { if (ticket === ordersTicket) ordersError.value = exc.message }
}

// The request is spent once read, so coming back to this project later opens it as it was.
watch(() => props.focus?.nonce, async () => {
  if (props.focus?.section !== 'orders' || props.focus.id !== props.project.id) return
  ordersOpen.value = 0
  if (props.focus.nonce !== readForNonce) readOrders(props.project.id)
  emit('focused')
  await nextTick()
  ordersPanel.value?.$el?.scrollIntoView?.({ block: 'start' })
}, { immediate: true })

async function save() {
  const id = props.project.id
  busy.value = true
  error.value = null
  try {
    if (orders.value && instructions.value !== savedInstructions.value) {
      const ticket = ++ordersTicket
      ordersSave = setAssistantInstructions(id, instructions.value)
      const saved = await ordersSave
      if (ticket === ordersTicket) {
        orders.value = saved
        instructions.value = savedInstructions.value = saved.instructions || ''
      }
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
      <v-alert v-if="ordersError" type="error" class="mb-3">{{ ordersError }}</v-alert>
      <v-text-field v-model="form.name" label="Project name" :disabled="busy" maxlength="120" />
      <fieldset class="project-color mb-4" :disabled="busy">
        <legend class="text-body-medium mb-2">Project color
          <v-chip variant="flat" :color="pickedColor" :style="{ color: projectTextColor(pickedColor) }" size="small" class="ms-2">{{ colorName }}</v-chip>
        </legend>
        <v-color-picker v-model="pickedColor" mode="hex" :modes="['hex']" show-swatches :swatches="swatches"
          swatches-max-height="none" :disabled="busy" elevation="0" width="100%" max-width="400" />
      </fieldset>
      <v-textarea v-model="form.repositories" label="Repositories (one URL per line)" :disabled="busy" rows="2" />
      <p class="mb-3">Execution overrides apply at the next start or rotation. Empty fields inherit platform defaults.</p>
      <v-text-field v-for="key in overrideKeys" :key="key" v-model="form.settings[key]" :label="overrideLabels[key]"
        hint="Empty inherits the platform default." persistent-hint class="mb-2" :disabled="busy" />
      <v-combobox v-model="form.settings.threads" label="Project thread connections" multiple chips :disabled="busy" />
      <v-expansion-panels ref="ordersPanel" v-model="ordersOpen" class="project-orders">
        <v-expansion-panel title="Standing instructions">
          <v-expansion-panel-text>
            <p v-if="orders && !savedInstructions" class="orders-empty text-medium-emphasis mb-3">
              No standing instructions yet.
            </p>
            <v-textarea v-model="instructions" label="Standing instructions" :disabled="busy || !orders"
              :maxlength="orders?.limit" :counter="orders?.limit" rows="6" />
            <p class="mb-2">Saved with this project. Each new assistant incarnation reads these instructions.</p>
            <p class="orders-hint text-body-small text-medium-emphasis mb-2">
              Usually set by telling uber lmer in chat — "from now on…", "always…" — and it
              confirms the wording before storing them.
            </p>
            <p v-if="orders?.instructions_at" :title="orders.instructions_at"
              class="orders-changed text-body-small text-medium-emphasis mb-0">
              last changed {{ ago(orders.instructions_at, now) }}
            </p>
          </v-expansion-panel-text>
        </v-expansion-panel>
      </v-expansion-panels>
    </v-card-text>
  </v-card>
</template>

<style scoped>
.project-settings { overflow: visible; }
.project-color { border: 0; padding: 0; margin: 0; min-width: 0; }
.project-actions { position: sticky; top: var(--v-layout-top, 64px); z-index: 1; flex-wrap: wrap; background: rgb(var(--v-theme-surface)); }
</style>
