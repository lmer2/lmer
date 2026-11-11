<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { archiveProject, fetchPlatformSettings, setPlatformSettings, fetchConversationRequests, parkConversationRequest, releaseConversationRequest } from '../api.js'
import AssistantSettings from './AssistantSettings.vue'
import IngressStatus from './IngressStatus.vue'
import RotateAllAssistants from './RotateAllAssistants.vue'
import SettingsSection from './SettingsSection.vue'
import SlotEditor from './SlotEditor.vue'
import SlotRow from './SlotRow.vue'
import { projectColor, projectTextColor } from '../projects.js'
import { applyChatSettings } from '../operatorChat.js'
import { assistantName } from '../ratchet.js'
import { asNumber, numberReading } from '../numberField.js'

const props = defineProps({ state: { type: Object, default: null }, loading: Boolean, projects: { type: Array, default: () => [] }, slots: { type: Array, default: () => [] },
  runs: { type: Array, default: () => [] }, now: { type: Number, default: () => Date.now() }, focusSection: { type: String, default: null } })
const emit = defineEmits(['changed', 'open'])

// Issue 652: one section shown at a time, picked from the side nav (or the
// select on a phone), so every setting is one choice away.
const SECTIONS = [
  { id: 'fleet', title: 'Fleet' },
  { id: 'assistants', title: 'Assistants' },
  { id: 'chat', title: 'Chat and display' },
  { id: 'graph', title: 'Graph' },
  { id: 'slots', title: 'Service slots' },
  { id: 'projects', title: 'Projects' },
  { id: 'inbound', title: 'Inbound chat' },
]
const section = ref(SECTIONS.some(row => row.id === props.focusSection) ? props.focusSection : 'fleet')
const panel = ref(null)

const cap = ref(4)
const workerCap = ref(8)
const capacitySaved = ref(null)
const capacityReady = ref(false)
const capacityDone = ref(false)
const fleetGroups = ref([])
const groupsReady = ref(false)
const groupsBusy = ref(false)
const groupsError = ref(null)
const groupsSaved = ref(false)
// The stored groups and the daemon's complaint about them, for Discard.
let storedGroups = []
let storedGroupsProblem = null
const groupsBaseline = ref('[]')
// A stored list the daemon could not read is saved over even without an edit.
const groupsBroken = ref(false)
// Every run's operator channel dock (#465): '' is unset, which keeps every note.
const dockKeepLast = ref('')
const dockBaseline = ref('')
const dockReady = ref(false)
const dockBusy = ref(false)
const dockError = ref(null)
const dockSaved = ref(false)
let groupId = 0
const launch = ref(null)
const reminders = ref(null)
const display = ref(null)
const launchState = ref({ dirty: false, saving: false })
const remindersState = ref({ dirty: false, saving: false })
const displayState = ref({ dirty: false, saving: false })
const slotOpen = ref(false)
const slotName = ref(null)
const slot = computed(() => props.slots.find(row => row.name === slotName.value) || null)
const error = ref(null)
const busy = ref(false)
const requests = ref([])
const parking = ref(null)
const parkingReason = ref('')
const parkingBusy = ref(false)
const releasing = ref(null)
const releaseReason = ref('')
const releaseBusy = ref(false)
const requestsBusy = ref(false)
const requestsError = ref(null)
const archived = computed(() => props.projects.filter(row => row.archived))

const groupsText = groups => JSON.stringify(groups.map(group => [group.name, group.text]))
const capacityDirty = computed(() => capacitySaved.value !== null
  && (numberReading(cap.value) !== numberReading(capacitySaved.value.cap)
    || numberReading(workerCap.value) !== numberReading(capacitySaved.value.workers)))
const groupsDirty = computed(() => groupsReady.value && groupsText(fleetGroups.value) !== groupsBaseline.value)
const dockDirty = computed(() => dockReady.value && numberReading(dockKeepLast.value) !== dockBaseline.value)
const dirtyBySection = computed(() => ({
  fleet: capacityDirty.value,
  assistants: launchState.value.dirty || remindersState.value.dirty,
  chat: displayState.value.dirty || dockDirty.value,
  graph: groupsDirty.value,
}))
const anyDirty = computed(() => Object.values(dirtyBySection.value).some(Boolean))
const savingBySection = computed(() => ({
  fleet: busy.value,
  assistants: launchState.value.saving || remindersState.value.saving,
  chat: displayState.value.saving || dockBusy.value,
  graph: groupsBusy.value,
}))
const pickerItems = computed(() => SECTIONS.map(row => ({
  value: row.id, title: dirtyBySection.value[row.id] ? `${row.title} (unsaved)` : row.title,
})))

// A leave that waits on Keep editing / Discard: `{ sections, resolve, opener }`.
const leaving = ref(null)
const leavingTitles = computed(() => (leaving.value?.sections || [])
  .map(id => SECTIONS.find(row => row.id === id).title).join(', '))

function discard(ids) {
  for (const id of ids) {
    if (id === 'fleet' && capacitySaved.value) { cap.value = capacitySaved.value.cap; workerCap.value = capacitySaved.value.workers; error.value = null }
    if (id === 'assistants') { launch.value?.reset(); reminders.value?.reset() }
    if (id === 'chat') { display.value?.reset(); dockKeepLast.value = dockBaseline.value; dockError.value = null }
    if (id === 'graph') readGroups({ fleet_groups: storedGroups, fleet_groups_problem: storedGroupsProblem })
  }
}

// A save in flight is neither unsaved nor discardable: wait for it to land.
function settled(ids) {
  const busy = () => ids.some(id => savingBySection.value[id])
  if (!busy()) return Promise.resolve()
  return new Promise(resolve => { const stop = watch(busy, (now) => { if (!now) { stop(); resolve() } }) })
}

// Resolves true once nothing unsaved stands in the way, after asking if needed.
async function confirmDiscard(ids) {
  await settled(ids)
  const dirty = ids.filter(id => dirtyBySection.value[id])
  if (!dirty.length) return true
  leaving.value?.resolve(false)
  const opener = document.activeElement
  return new Promise(resolve => { leaving.value = { sections: dirty, resolve, opener } })
}
async function answerLeave(discarding) {
  const { sections, resolve, opener } = leaving.value
  leaving.value = null
  if (discarding) discard(sections)
  resolve(discarding)
  if (discarding) return
  await nextTick()
  if (opener?.isConnected) opener.focus()
}

async function requestSection(id, focus = false) {
  if (!id || id === section.value) return
  if (!await confirmDiscard([section.value])) return
  section.value = id
  if (!focus) return
  await nextTick()
  panel.value?.querySelector(`[data-panel="${id}"] .settings-section-title`)?.focus()
}

// The shell asks before leaving Platform settings.
function confirmLeave() { return confirmDiscard(SECTIONS.map(row => row.id)) }
defineExpose({ hasUnsavedChanges: () => anyDirty.value, confirmLeave })

function guardUnload(event) {
  if (!anyDirty.value) return
  event.preventDefault()
  event.returnValue = ''
}
onMounted(() => window.addEventListener('beforeunload', guardUnload))
onBeforeUnmount(() => {
  window.removeEventListener('beforeunload', guardUnload)
  leaving.value?.resolve(false)
})

async function refreshRequests() {
  if (requestsBusy.value) return
  requestsBusy.value = true
  try {
    const pending = await fetchConversationRequests()
    requests.value = pending.requests.filter(row => row.state === 'choice' || row.error || row.recovery || row.prompt_state === 'sending'
      || ['reply_sending', 'reply_uncertain'].includes(row.state) || actionProblems(row))
    requestsError.value = null
  } catch (exc) { requestsError.value = exc.message } finally { requestsBusy.value = false }
}
watch(() => props.state, refreshRequests)

async function focusSlots() {
  await requestSection('slots')
  if (section.value !== 'slots') return
  await nextTick()
  const heading = document.getElementById('platform-service-slots')
  heading?.scrollIntoView?.({ block: 'start' })
  heading?.focus({ preventScroll: true })
}
watch(() => props.focusSection, (next) => { if (next === 'slots') focusSlots() })

onMounted(async () => {
  try {
    const [settings] = await Promise.all([fetchPlatformSettings(), refreshRequests()])
    readCapacity(settings)
    readGroups(settings)
    groupsReady.value = true
    readDock(settings)
    dockReady.value = true
  } catch (exc) { error.value = exc.message }
  if (props.focusSection === 'slots') focusSlots()
})
function readCapacity(settings) {
  cap.value = settings.max_live_assistants
  workerCap.value = settings.max_concurrent_sessions
  capacitySaved.value = { cap: cap.value, workers: workerCap.value }
  capacityReady.value = true
}
async function save() {
  busy.value = true
  capacityDone.value = false
  try {
    const changes = { max_live_assistants: asNumber(cap.value), max_concurrent_sessions: asNumber(workerCap.value) }
    await setPlatformSettings(changes)
    // The inputs are left alone: one changed while this was in flight is a newer
    // edit, and stays unsaved against what was sent.
    capacitySaved.value = { cap: changes.max_live_assistants, workers: changes.max_concurrent_sessions }
    capacityDone.value = true
    emit('changed')
    error.value = null
  } catch (exc) { error.value = exc.message } finally { busy.value = false }
}
function readGroups(settings) {
  storedGroups = settings.fleet_groups || []
  storedGroupsProblem = settings.fleet_groups_problem || null
  fleetGroups.value = storedGroups.map(group => ({
    id: ++groupId, name: group.name, text: group.patterns.join('\n'),
  }))
  groupsBaseline.value = groupsText(fleetGroups.value)
  groupsError.value = settings.fleet_groups_problem || null
  groupsBroken.value = !!settings.fleet_groups_problem
}
function editGroups() { groupsError.value = null; groupsSaved.value = false }
function addGroup() {
  fleetGroups.value.push({ id: ++groupId, name: '', text: '' })
  editGroups()
}
function removeGroup(index) { fleetGroups.value.splice(index, 1); editGroups() }
function moveGroup(index, direction) {
  const [group] = fleetGroups.value.splice(index, 1)
  fleetGroups.value.splice(index + direction, 0, group)
  editGroups()
}
async function saveGroups() {
  groupsBusy.value = true
  groupsSaved.value = false
  try {
    const settings = await setPlatformSettings({ fleet_groups: fleetGroups.value.map(group => ({
      name: group.name, patterns: group.text.split('\n').map(text => text.trim()).filter(Boolean),
    })) })
    readGroups(settings)
    groupsSaved.value = true
    emit('changed')
  } catch (exc) { groupsError.value = exc.message } finally { groupsBusy.value = false }
}
function readDock(settings) {
  const value = settings.dock?.keep_last_notes?.value
  dockKeepLast.value = Number.isSafeInteger(value) ? String(value) : ''
  dockBaseline.value = dockKeepLast.value
}
async function saveDock() {
  const edited = String(dockKeepLast.value ?? '').trim()
  const value = edited === '' ? null : asNumber(edited)
  if (value !== null && (!Number.isSafeInteger(value) || value < 0)) {
    dockError.value = 'Keep last must be a whole number of 0 or more, or empty to show every note.'
    dockSaved.value = false
    return
  }
  dockBusy.value = true
  dockSaved.value = false
  try {
    const settings = await setPlatformSettings({ dock_keep_last_notes: value })
    readDock(settings)
    // This browser's docks follow at once; other browsers on their next refresh.
    applyChatSettings(settings)
    dockError.value = null
    dockSaved.value = true
  } catch (exc) { dockError.value = exc.message } finally { dockBusy.value = false }
}
async function restore(project) {
  try { await archiveProject(project.id, true); emit('changed') } catch (exc) { error.value = exc.message }
}
function actionProblems(request) {
  return (request.actions || []).filter(action => ['sending', 'uncertain'].includes(action.state))
    .map(action => `${action.method} ${action.path}: ${action.state}${action.error ? ` — ${action.error}` : ''}`).join('; ')
}
function edit(row) { slotName.value = row?.name || null; slotOpen.value = true }
function projectName(request) { return props.projects.find(project => project.id === request.project_id)?.name || request.project_id }
function confirmParking(request) { parking.value = request; parkingReason.value = ''; error.value = null }
async function park() {
  const target = parking.value
  parkingBusy.value = true
  try {
    const result = await parkConversationRequest(target.id, target.project_id, parkingReason.value)
    target.parking = result.parking
    parking.value = null
    error.value = null
  } catch (exc) { error.value = exc.message } finally { parkingBusy.value = false }
}
function confirmRelease(request) { releasing.value = request; releaseReason.value = ''; error.value = null }
async function release() {
  const target = releasing.value
  releaseBusy.value = true
  try {
    await releaseConversationRequest(target.id, target.project_id, releaseReason.value)
    releasing.value = null
    error.value = null
    await refreshRequests()
    emit('changed')
  } catch (exc) { error.value = exc.message } finally { releaseBusy.value = false }
}
</script>

<template>
  <div class="platform-settings">
    <h1 class="text-title-large mb-4">Platform settings</h1>
    <v-alert v-if="error" type="error" class="mb-3">{{ error }}</v-alert>
    <div class="settings-layout">
      <nav class="settings-nav" aria-label="Settings sections">
        <v-btn v-for="row in SECTIONS" :key="row.id" block :variant="section === row.id ? 'tonal' : 'text'"
          :class="['settings-nav-item', { 'settings-nav-item--active': section === row.id }]" :data-section="row.id"
          :aria-current="section === row.id ? 'page' : undefined" @click="requestSection(row.id, true)">
          <span class="settings-nav-label">{{ row.title }}</span>
          <template v-if="dirtyBySection[row.id]">
            <span class="settings-dirty-dot" aria-hidden="true" />
            <span class="d-sr-only">, unsaved changes</span>
          </template>
        </v-btn>
      </nav>
      <v-select class="settings-picker" label="Section" :items="pickerItems" :model-value="section"
        hide-details @update:model-value="requestSection" />

      <div ref="panel" class="settings-content">
        <div v-show="section === 'fleet'" class="settings-panel" data-panel="fleet">
          <SettingsSection title="Capacity" description="How many sessions may run at once, across all projects."
            :dirty="capacityDirty" save-label="Save capacity" :saving="busy" :save-disabled="!capacityReady" @save="save">
            <div class="capacity-limits">
              <div>
                <label for="worker-capacity">Maximum live workers across all projects</label>
                <v-text-field id="worker-capacity" v-model="workerCap" type="number" inputmode="numeric" min="1" step="1" />
              </div>
              <div>
                <label for="assistant-capacity">Maximum live assistants</label>
                <v-text-field id="assistant-capacity" v-model="cap" type="number" inputmode="numeric" min="1" step="1" />
              </div>
            </div>
            <p>Projects wait in order when capacity is full. Lowering this limit keeps current assistants running. Worker capacity is separate.</p>
            <p v-if="capacityDone && !capacityDirty" role="status" class="mt-2">Capacity saved.</p>
          </SettingsSection>
        </div>

        <div v-show="section === 'assistants'" class="settings-panel" data-panel="assistants">
          <SettingsSection title="Launch defaults"
            :description="`How every project's ${assistantName} session starts, unless the project overrides it.`"
            :dirty="launchState.dirty" save-label="Save launch defaults" :saving="launchState.saving" @save="launch?.save()">
            <AssistantSettings ref="launch" :groups="['launch']" :allow-restart="false" @state="launchState = $event" />
          </SettingsSection>
          <SettingsSection title="Reminders" :description="`What the daemon types into a quiet ${assistantName} session, and when.`"
            :dirty="remindersState.dirty" save-label="Save reminders" :saving="remindersState.saving" @save="reminders?.save()">
            <AssistantSettings ref="reminders" :groups="['reminders']" :allow-restart="false" @state="remindersState = $event" />
          </SettingsSection>
          <SettingsSection title="Rotate all"
            :description="`Ask every running ${assistantName} to hand over and start fresh at its next breakpoint. Nothing is stopped.`">
            <template #actions><RotateAllAssistants block /></template>
          </SettingsSection>
        </div>

        <div v-show="section === 'chat'" class="settings-panel" data-panel="chat">
          <SettingsSection title="Chat display" description="How the chat panes show your messages and the working transcript, and what Enter does in the message boxes."
            :dirty="displayState.dirty" save-label="Save chat display" :saving="displayState.saving" @save="display?.save()">
            <AssistantSettings ref="display" :groups="['conversation']" :allow-restart="false" @state="displayState = $event" />
          </SettingsSection>
          <SettingsSection title="Operator channel dock" description="How many notes the dock above every run keeps showing."
            class="dock-display" :dirty="dockDirty" save-label="Save dock display" :saving="dockBusy" :save-disabled="!dockReady"
            @save="saveDock">
            <v-alert v-if="dockError" type="error" class="mb-3" role="alert">{{ dockError }}</v-alert>
            <v-text-field v-model="dockKeepLast" label="Operator channel dock: keep last N notes" type="number"
              inputmode="numeric" min="0" step="1" clearable persistent-hint :disabled="!dockReady || dockBusy"
              hint="Applies to the dock above every run. Open questions are always shown. 0 shows open questions only; leave empty to show every note." />
            <p class="text-medium-emphasis mt-2">Older notes leave the dock only: each run's operator chat tab keeps the full history.</p>
            <p v-if="dockSaved" role="status" class="mt-2">Dock display saved.</p>
          </SettingsSection>
        </div>

        <div v-show="section === 'graph'" class="settings-panel" data-panel="graph">
          <SettingsSection title="Graph groups" description="Cluster repositories on the fleet graph by text in their path."
            class="fleet-groups" :dirty="groupsDirty" save-label="Save groups" :saving="groupsBusy"
            :saveable="groupsBroken" :save-disabled="!groupsReady" @save="saveGroups">
            <p id="fleet-group-help" class="mb-3">Match case-insensitive text contained in the repository path, one entry per line.
              Any entry matches; the first matching group wins. For example, <code>team/</code> or <code>tools/team-</code>.
              All characters are literal. These groups only change the graph.</p>
            <v-alert v-if="groupsError" type="error" class="mb-3" role="alert">{{ groupsError }}</v-alert>
            <p v-if="groupsReady && !fleetGroups.length" class="mb-3">No graph groups. Repositories keep their own clusters.</p>
            <div v-for="(group, index) in fleetGroups" :key="group.id" class="fleet-group-row">
              <v-text-field v-model="group.name" :label="`Group ${index + 1} name`" maxlength="64"
                :disabled="groupsBusy" @update:model-value="editGroups" />
              <v-textarea v-model="group.text" :label="`Group ${index + 1} matching text`" rows="2" auto-grow
                aria-describedby="fleet-group-help" :disabled="groupsBusy" @update:model-value="editGroups" />
              <div class="fleet-group-actions">
                <v-btn size="small" variant="tonal" :aria-label="`Move group ${index + 1} up`" :disabled="groupsBusy || index === 0" @click="moveGroup(index, -1)">Up</v-btn>
                <v-btn size="small" variant="tonal" :aria-label="`Move group ${index + 1} down`" :disabled="groupsBusy || index === fleetGroups.length - 1" @click="moveGroup(index, 1)">Down</v-btn>
                <v-btn size="small" variant="tonal" color="error" :aria-label="`Remove group ${index + 1}`" :disabled="groupsBusy" @click="removeGroup(index)">Remove</v-btn>
              </div>
            </div>
            <p class="text-medium-emphasis">Up to 32 groups, with 1–16 entries of up to 256 characters each. Removing takes effect when you save; remove all groups and save to clear.</p>
            <p v-if="groupsSaved" role="status" class="mt-2">Graph groups saved.</p>
            <template #actions>
              <v-btn variant="tonal" :disabled="!groupsReady || groupsBusy || fleetGroups.length >= 32" @click="addGroup">Add group</v-btn>
            </template>
          </SettingsSection>
        </div>

        <div v-show="section === 'slots'" class="settings-panel" data-panel="slots">
          <SettingsSection title="Service slots" heading-id="platform-service-slots"
            description="Long-lived service sessions that projects share, and which projects may use each one.">
            <template v-if="!loading">
              <v-alert v-if="state?.slots_problem" type="error">{{ state.slots_problem }}</v-alert>
              <p v-else-if="!slots.length && state" class="text-medium-emphasis">No service slots configured.</p>
              <SlotRow v-for="row in slots" :key="row.name" :row="row" :runs="runs" @edit="edit" @open="emit('open', $event)" />
            </template>
            <template #actions><v-btn variant="tonal" @click="edit(null)">Add slot</v-btn></template>
          </SettingsSection>
          <SlotEditor v-model="slotOpen" :key="slot?.name || 'new'" :row="slot" :projects="projects" @changed="emit('changed')" />
        </div>

        <div v-show="section === 'projects'" class="settings-panel" data-panel="projects">
          <SettingsSection title="Archived projects"
            description="Projects taken out of the switcher and the sidebar. Restoring one brings it back stopped.">
            <p v-if="!archived.length" class="text-medium-emphasis">No archived projects.</p>
            <v-list v-else><v-list-item v-for="project in archived" :key="project.id"
              class="archived-project" :aria-label="`Archived project ${project.name}`"
              :style="{ borderInlineStartColor: projectColor(project.color) }">
              <v-chip :color="projectColor(project.color)" :style="{ color: projectTextColor(project.color) }" variant="flat">{{ project.name }}</v-chip>
              <template #append><v-btn variant="tonal" @click="restore(project)">Restore stopped</v-btn></template>
            </v-list-item></v-list>
          </SettingsSection>
        </div>

        <div v-show="section === 'inbound'" class="settings-panel" data-panel="inbound">
          <SettingsSection title="Message ingress" :description="`Whether Slack and Matrix messages are reaching ${assistantName}.`">
            <IngressStatus :now="now" />
          </SettingsSection>
          <SettingsSection title="Conversation requests needing attention"
            description="Chat requests that could not be routed or answered on their own.">
            <v-alert v-if="requestsError" type="error">{{ requestsError }}</v-alert>
            <p v-else-if="!requests.length" class="text-medium-emphasis">No conversation requests need attention.</p>
            <v-list><v-list-item v-for="request in requests" :key="request.id" :title="`${request.transport}: ${request.state}`"
              :subtitle="request.recovery?.reason || request.error || actionProblems(request) || `Project choice pending in ${request.conversation}, thread ${request.root}`">
              <p>{{ projectName(request) }} · {{ request.conversation }} · {{ request.root }}</p>
              <p v-if="request.recovery">{{ request.recovery.instruction }}</p>
              <p v-for="action in (request.actions || []).filter(row => row.state === 'complete')" :key="`${action.method}:${action.path}`">
                {{ action.method }} {{ action.path }}: complete — receipt retained
              </p>
              <v-btn v-if="request.recovery?.release_allowed" variant="tonal" color="error" @click="confirmRelease(request)">Release blocked request</v-btn>
              <p v-if="request.parking">Parked by {{ request.parking.by }} at {{ request.parking.at }}: {{ request.parking.reason }}. Delivery remains unknown; archive stays blocked.</p>
              <v-btn v-else-if="['reply_sending', 'reply_uncertain'].includes(request.state)" variant="tonal" @click="confirmParking(request)">Park uncertain reply</v-btn>
            </v-list-item></v-list>
            <template #actions><v-btn variant="tonal" :loading="requestsBusy" @click="refreshRequests">Refresh requests</v-btn></template>
          </SettingsSection>
        </div>
      </div>
    </div>

    <v-dialog :model-value="!!leaving" max-width="480" persistent>
      <v-card v-if="leaving" class="settings-leave" title="Discard unsaved changes?">
        <v-card-text>{{ leavingTitles }} has unsaved changes. Discard them, or keep editing and save first.</v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="tonal" @click="answerLeave(false)">Keep editing</v-btn>
          <v-btn color="error" variant="flat" @click="answerLeave(true)">Discard</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>
    <v-dialog :model-value="!!parking" :persistent="parkingBusy" max-width="560" @update:model-value="value => { if (!value) parking = null }">
      <v-card v-if="parking" :title="`Park uncertain reply for ${projectName(parking)}`">
        <v-card-text>
          <v-alert v-if="error" type="error" class="mb-3">{{ error }}</v-alert>
          <p>{{ parking.transport }} · {{ parking.conversation }} · {{ parking.root }}</p>
          <p class="mb-3">Allow independent work in this project. The reply stays uncertain and will not be resent or marked delivered. Archive remains blocked.</p>
          <v-textarea v-model="parkingReason" label="Reason for parking" maxlength="2000" :disabled="parkingBusy" />
        </v-card-text>
        <v-card-actions>
          <v-btn variant="tonal" :disabled="parkingBusy" @click="parking = null">Cancel</v-btn>
          <v-btn color="primary" variant="flat" :loading="parkingBusy" :disabled="!parkingReason.trim()" @click="park">Park and allow independent work</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>
    <v-dialog :model-value="!!releasing" :persistent="releaseBusy" max-width="560" @update:model-value="value => { if (!value) releasing = null }">
      <v-card v-if="releasing" :title="`Release blocked request for ${projectName(releasing)}`">
        <v-card-text>
          <v-alert v-if="error" type="error" class="mb-3">{{ error }}</v-alert>
          <p>{{ releasing.transport }} · {{ releasing.conversation }} · {{ releasing.root }}</p>
          <p>{{ releasing.recovery.reason }}</p>
          <p>No reply will be sent and no completed action will be repeated. Keep the existing worker and ask for a fresh request in the source thread.</p>
          <v-textarea v-model="releaseReason" label="Reason for release" maxlength="2000" :disabled="releaseBusy" />
        </v-card-text>
        <v-card-actions>
          <v-btn variant="tonal" :disabled="releaseBusy" @click="releasing = null">Cancel</v-btn>
          <v-btn color="error" variant="flat" :loading="releaseBusy" :disabled="!releaseReason.trim()" @click="release">Release and allow independent work</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>
  </div>
</template>

<style scoped>
.platform-settings { container-type: inline-size; }
.settings-layout { display: grid; grid-template-columns: minmax(0, 1fr); gap: 16px; }
.settings-nav { display: none; }
.settings-content { min-width: 0; }
@container (min-width: 720px) {
  .settings-layout { grid-template-columns: 196px minmax(0, 1fr); align-items: start; }
  .settings-nav { display: flex; flex-direction: column; gap: 4px; position: sticky; top: calc(var(--v-layout-top, 64px) + 12px); }
  .settings-picker { display: none; }
}
.settings-nav-item :deep(.v-btn__content) { width: 100%; justify-content: flex-start; gap: 8px; }
.settings-nav-label { white-space: normal; text-align: start; }
/* Primary ink on a primary wash falls short of AA in the light scheme, so the
   current section keeps full-strength ink and takes its accent as an edge. */
.settings-nav-item--active { box-shadow: inset 3px 0 0 rgb(var(--v-theme-primary)); font-weight: 600; }
.settings-dirty-dot { width: 8px; height: 8px; border-radius: 50%; background: rgb(var(--v-theme-warning)); flex: 0 0 auto; }
.fleet-group-row { display: grid; grid-template-columns: minmax(120px, 1fr) minmax(160px, 2fr); gap: 0 16px; margin-bottom: 16px; }
.fleet-group-actions { grid-column: 1 / -1; display: flex; flex-wrap: wrap; gap: 8px; }
@media (max-width: 600px) { .fleet-group-row { grid-template-columns: minmax(0, 1fr); } }
.capacity-limits { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
.capacity-limits > div { display: flex; flex-direction: column; }
.capacity-limits .v-input { flex: 0 0 auto; }
.capacity-limits label { display: block; flex: 1; margin-bottom: 8px; font-size: .875rem; }
.archived-project { border-inline-start: 4px solid; }
.archived-project :deep(.v-list-item__content) { padding-inline-end: 8px; }
.archived-project .v-chip { max-width: 100%; height: auto; min-height: 32px; white-space: normal; overflow-wrap: anywhere; }
</style>
