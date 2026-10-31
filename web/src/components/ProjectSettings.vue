<script setup>
import { computed, nextTick, ref, watch } from 'vue'
import { archiveProject, fetchAssistantInstructions, fetchChannelRoutes, setAssistantInstructions, setProjectChannels,
  updateProject } from '../api.js'
import { ago } from '../format.js'
import { LAUNCH_SETTING_HINTS, LAUNCH_SETTING_KEYS, LAUNCH_SETTING_LABELS } from '../launchSettings.js'
import { projectColor, projectColors, projectTextColor, storedProjectColor } from '../projects.js'
import { assistantName } from '../ratchet.js'

// `focus` is `{ id, section, nonce }`; the nonce makes a repeat tap count.
// `isDefault` is whether this browser opens this project on load; `default` reports a change.
const props = defineProps({ project: { type: Object, required: true }, focus: { type: Object, default: null },
  now: { type: Number, default: () => Date.now() }, isDefault: Boolean })
const emit = defineEmits(['changed', 'close', 'default', 'focused'])
const openOnLoad = computed({
  get: () => props.isDefault,
  set: (on) => emit('default', on ? props.project.id : ''),
})
const form = ref({})
const orders = ref(null)
const instructions = ref('')
const savedInstructions = ref('')
const ordersOpen = ref(undefined)
const ordersPanel = ref(null)
const error = ref(null)
const ordersError = ref(null)
const busy = ref(false)
const channels = ref('')
const savedChannels = ref(null)
// The picker speaks hex; the form keeps the stored spelling, preset name or hex.
const pickedColor = computed({
  get: () => projectColor(form.value.color),
  set: (hex) => { form.value.color = storedProjectColor(hex) },
})
// v-color-picker draws each inner array as a column: ten columns of two.
const swatches = Object.values(projectColors).reduce((rows, hex, index) => {
  if (index % 2 === 0) rows.push([])
  rows.at(-1).push(hex)
  return rows
}, [])
// The canvas, sliders and HEX field: open for a color no swatch holds.
const customColor = ref(false)
const colorName = computed(() => (Object.hasOwn(projectColors, form.value.color) ? form.value.color : pickedColor.value))
const authorityLevels = [
  { value: 'none', title: 'None: its messages carry no approval' },
  { value: 'approvals', title: 'Approvals: answer workers\' asks and say go within the task' },
  { value: 'full', title: 'Full: direction and scope within a run\'s target' },
]
// Unset is `none`; choosing `none` clears the key rather than storing it, so
// a project has opted in only when it holds a level.
const supervisorAuthority = computed({
  get: () => form.value.settings.supervisor_authority || 'none',
  set: (value) => { form.value.settings.supervisor_authority = value === 'none' ? null : value },
})

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
  customColor.value = !Object.hasOwn(projectColors, props.project.color || 'Gray')
  readForNonce = props.focus?.nonce
  readOrders(id)
  readChannels(id)
}, { immediate: true })

const channelList = (text) => text.split('\n').map(value => value.trim()).filter(Boolean)

async function readChannels(id) {
  channels.value = ''
  savedChannels.value = null
  try {
    const { routes } = await fetchChannelRoutes()
    if (id !== props.project.id) return
    savedChannels.value = channels.value = routes.filter(route => route.project_id === id).map(route => route.label).join('\n')
  } catch (exc) { if (id === props.project.id) error.value = exc.message }
}

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
    if (savedChannels.value !== null && channels.value !== savedChannels.value) {
      const { routes } = await setProjectChannels(id, channelList(channels.value))
      savedChannels.value = channels.value = routes.map(route => route.label).join('\n')
    }
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
    </v-card-actions>
    <v-card-text class="settings-body">
      <v-alert v-if="error" type="error" class="mb-3">{{ error }}</v-alert>
      <v-alert v-if="ordersError" type="error" class="mb-3">{{ ordersError }}</v-alert>

      <section class="settings-group" aria-labelledby="project-identity">
        <h3 id="project-identity" class="text-title-small mb-1">Identity</h3>
        <p class="text-body-small text-medium-emphasis mb-3">
          How the project is named and coloured everywhere, and which repositories its workers use.
        </p>
        <div class="settings-grid">
          <div class="settings-field">
            <v-text-field v-model="form.name" label="Project name" :disabled="busy" maxlength="120" hide-details="auto" />
          </div>
        </div>
        <fieldset class="project-color settings-field" :disabled="busy">
          <legend class="text-body-medium mb-2">Project color
            <v-chip variant="flat" :color="pickedColor" :style="{ color: projectTextColor(pickedColor) }" size="small" class="ms-2">{{ colorName }}</v-chip>
          </legend>
          <v-color-picker v-model="pickedColor" mode="hex" :modes="['hex']" show-swatches :swatches="swatches"
            swatches-max-height="none" :hide-canvas="!customColor" :hide-sliders="!customColor" :hide-inputs="!customColor"
            canvas-height="120" :disabled="busy" elevation="0" width="100%" max-width="360" class="project-color-picker" />
          <v-btn variant="text" size="small" class="custom-color" :aria-expanded="customColor" :disabled="busy"
            @click="customColor = !customColor">{{ customColor ? 'Swatches only' : 'Custom…' }}</v-btn>
        </fieldset>
        <div class="settings-field">
          <v-textarea v-model="form.repositories" label="Repositories" :disabled="busy" rows="2" auto-grow
            persistent-hint hint="One URL per line." />
        </div>
      </section>

      <section class="settings-group" aria-labelledby="project-browser">
        <h3 id="project-browser" class="text-title-small mb-1">This browser</h3>
        <p class="text-body-small text-medium-emphasis mb-3">
          Kept in this browser only, and applied now. Other browsers and devices are not affected.
        </p>
        <div class="settings-field">
          <v-switch v-model="openOnLoad" class="project-default" color="primary" density="compact" hide-details
            label="Open this project when the page loads" />
          <p class="text-body-small text-medium-emphasis setting-source">
            Takes priority over the project you last had open; a link that names a project still wins.
            Turn it off to return to opening the last project.
          </p>
        </div>
      </section>

      <section class="settings-group" aria-labelledby="project-execution">
        <h3 id="project-execution" class="text-title-small mb-1">Execution</h3>
        <p class="text-body-small text-medium-emphasis mb-3">
          Applies at the next start or rotation. Empty fields inherit the platform defaults.
        </p>
        <div class="settings-grid">
          <div v-for="key in LAUNCH_SETTING_KEYS" :key="key" class="settings-field">
            <v-text-field v-model="form.settings[key]" :label="LAUNCH_SETTING_LABELS[key]" :hint="LAUNCH_SETTING_HINTS[key]"
              persistent-hint :disabled="busy" />
          </div>
        </div>
        <div class="settings-field">
          <v-combobox v-model="form.settings.threads" label="Project thread connections" multiple chips :disabled="busy"
            persistent-hint hint="Slack or Matrix thread permalinks. Press Enter after each one." />
        </div>
      </section>

      <section class="settings-group" aria-labelledby="project-assistant">
        <h3 id="project-assistant" class="text-title-small mb-1">Assistant</h3>
        <p class="text-body-small text-medium-emphasis mb-3">
          What {{ assistantName }} may decide on its own, and the instructions every new incarnation reads.
        </p>
        <div class="settings-field">
          <v-select v-model="supervisorAuthority" :items="authorityLevels" class="supervisor-authority"
            :label="`What ${assistantName} may approve for workers`" :disabled="busy" persistent-hint
            hint="Stamped on each of its messages to this project's workers. No level passes an operator-only gate such as spec approval or a merge." />
        </div>
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
                Usually set by telling {{ assistantName }} in chat — "from now on…", "always…" — and it
                confirms the wording before storing them.
              </p>
              <p v-if="orders?.instructions_at" :title="orders.instructions_at"
                class="orders-changed text-body-small text-medium-emphasis mb-0">
                last changed {{ ago(orders.instructions_at, now) }}
              </p>
            </v-expansion-panel-text>
          </v-expansion-panel>
        </v-expansion-panels>
      </section>

      <section class="settings-group" aria-labelledby="project-channels">
        <h3 id="project-channels" class="text-title-small mb-1">Chat channels</h3>
        <p class="text-body-small text-medium-emphasis mb-3">
          Bot mentions in these channels route to this project unless they name another one.
        </p>
        <div class="settings-field">
          <v-textarea v-model="channels" class="mention-channels" label="Mention channels" rows="2" auto-grow
            :disabled="busy || savedChannels === null" persistent-hint spellcheck="false"
            hint="One per line: a Slack channel ID, #name or link; a Matrix room ID, alias or link." />
        </div>
      </section>

      <section class="settings-group" aria-labelledby="project-archive">
        <h3 id="project-archive" class="text-title-small mb-1">Archive</h3>
        <p class="text-body-small text-medium-emphasis mb-3">
          Takes the project out of the switcher and the sidebar. It can be restored from Platform settings.
        </p>
        <v-btn color="warning" :disabled="busy" @click="archive">Archive project</v-btn>
      </section>
    </v-card-text>
  </v-card>
</template>

<style scoped>
.project-settings { overflow: visible; }
.settings-body { container-type: inline-size; }
/* Two columns for short values once the dialog has room for them, in DOM order so
   keyboard order is reading order; long fields and switches stay outside the grid. */
.settings-grid { display: grid; grid-template-columns: minmax(0, 1fr); column-gap: 16px; }
@container (min-width: 560px) {
  .settings-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
.project-color { border: 0; padding: 0; min-width: 0; text-align: center; }
.project-color-picker { margin-inline: auto; }
.project-color legend { display: flex; align-items: center; flex-wrap: wrap; padding: 0; }
.project-actions { position: sticky; top: var(--v-layout-top, 64px); z-index: 1; flex-wrap: wrap; background: rgb(var(--v-theme-surface)); }
</style>
