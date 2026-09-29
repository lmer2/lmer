<script setup>
import { computed, nextTick, onMounted, ref } from 'vue'
import { archiveProject, fetchPlatformSettings, setPlatformSettings, fetchConversationRequests, parkConversationRequest } from '../api.js'
import AssistantSettings from './AssistantSettings.vue'
import IngressStatus from './IngressStatus.vue'
import SlotEditor from './SlotEditor.vue'
import SlotRow from './SlotRow.vue'
import { projectColor } from '../projects.js'

const props = defineProps({ state: { type: Object, default: null }, loading: Boolean, projects: { type: Array, default: () => [] }, slots: { type: Array, default: () => [] },
  runs: { type: Array, default: () => [] }, now: { type: Number, default: () => Date.now() }, focusSection: { type: String, default: null } })
const emit = defineEmits(['changed', 'open'])
const cap = ref(4)
const workerCap = ref(8)
const defaultsOpen = ref(false)
const slotOpen = ref(false)
const slotName = ref(null)
const slotSection = ref(null)
const slot = computed(() => props.slots.find(row => row.name === slotName.value) || null)
const error = ref(null)
const busy = ref(false)
const requests = ref([])
const parking = ref(null)
const parkingReason = ref('')
const parkingBusy = ref(false)

onMounted(async () => {
  try {
    const [settings, pending] = await Promise.all([fetchPlatformSettings(), fetchConversationRequests()])
    cap.value = settings.max_live_assistants
    workerCap.value = settings.max_concurrent_sessions
    requests.value = pending.requests.filter(row => row.state === 'choice' || row.error || row.prompt_state === 'sending'
      || ['reply_sending', 'reply_uncertain'].includes(row.state) || actionProblems(row))
  } catch (exc) { error.value = exc.message }
  if (props.focusSection === 'slots') {
    await nextTick()
    slotSection.value?.scrollIntoView?.({ block: 'start' })
    slotSection.value?.focus({ preventScroll: true })
  }
})
async function save() {
  busy.value = true
  try { await setPlatformSettings({ max_live_assistants: Number(cap.value), max_concurrent_sessions: Number(workerCap.value) }); emit('changed'); error.value = null }
  catch (exc) { error.value = exc.message } finally { busy.value = false }
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
</script>

<template>
  <h1 class="text-title-large mb-4">Platform settings</h1>
  <v-alert v-if="error" type="error" class="mb-3">{{ error }}</v-alert>
  <v-card class="mb-4" title="Fleet capacity">
    <v-card-text>
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
    </v-card-text>
    <v-card-actions class="flex-wrap"><v-btn :loading="busy" @click="save">Save limit</v-btn><v-btn @click="defaultsOpen = true">Execution and chat defaults</v-btn></v-card-actions>
  </v-card>
  <AssistantSettings v-model="defaultsOpen" :allow-restart="false" />
  <IngressStatus :now="now" />
  <div class="d-flex align-center mt-4 mb-2"><h2 id="platform-service-slots" ref="slotSection" tabindex="-1" class="text-title-medium">Service slots</h2><v-spacer /><v-btn @click="edit(null)">Add slot</v-btn></div>
  <template v-if="!loading">
    <v-alert v-if="state?.slots_problem" type="error">{{ state.slots_problem }}</v-alert>
    <p v-else-if="!slots.length && state" class="text-medium-emphasis">No service slots configured.</p>
    <SlotRow v-for="row in slots" :key="row.name" :row="row" :runs="runs" @edit="edit" @open="emit('open', $event)" />
  </template>
  <SlotEditor v-model="slotOpen" :key="slot?.name || 'new'" :row="slot" :projects="projects" @changed="emit('changed')" />
  <h2 class="text-title-medium mt-4 mb-2">Archived projects</h2>
  <v-list><v-list-item v-for="project in projects.filter(row => row.archived)" :key="project.id"
    class="archived-project" :aria-label="`Archived project ${project.name}`"
    :style="{ borderInlineStartColor: projectColor(project.color) }">
    <v-chip :color="projectColor(project.color)" variant="flat">{{ project.name }}</v-chip>
    <template #append><v-btn :color="projectColor(project.color)" variant="flat" @click="restore(project)">Restore stopped</v-btn></template>
  </v-list-item></v-list>
  <h2 v-if="requests.length" class="text-title-medium mt-4">Conversation requests needing attention</h2>
  <v-list><v-list-item v-for="request in requests" :key="request.id" :title="`${request.transport}: ${request.state}`"
    :subtitle="request.error || actionProblems(request) || `Project choice pending in ${request.conversation}, thread ${request.root}`">
    <p>{{ projectName(request) }} · {{ request.conversation }} · {{ request.root }}</p>
    <p v-if="request.parking">Parked by {{ request.parking.by }} at {{ request.parking.at }}: {{ request.parking.reason }}. Delivery remains unknown; archive stays blocked.</p>
    <v-btn v-else-if="['reply_sending', 'reply_uncertain'].includes(request.state)" @click="confirmParking(request)">Park uncertain reply</v-btn>
  </v-list-item></v-list>
  <v-dialog :model-value="!!parking" :persistent="parkingBusy" max-width="560" @update:model-value="value => { if (!value) parking = null }">
    <v-card v-if="parking" :title="`Park uncertain reply for ${projectName(parking)}`">
      <v-card-text>
        <v-alert v-if="error" type="error" class="mb-3">{{ error }}</v-alert>
        <p>{{ parking.transport }} · {{ parking.conversation }} · {{ parking.root }}</p>
        <p class="mb-3">Allow independent work in this project. The reply stays uncertain and will not be resent or marked delivered. Archive remains blocked.</p>
        <v-textarea v-model="parkingReason" label="Reason for parking" maxlength="2000" :disabled="parkingBusy" />
      </v-card-text>
      <v-card-actions>
        <v-btn :disabled="parkingBusy" @click="parking = null">Cancel</v-btn>
        <v-btn :loading="parkingBusy" :disabled="!parkingReason.trim()" @click="park">Park and allow independent work</v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>

<style scoped>
#platform-service-slots { scroll-margin-top: 80px; }
.capacity-limits { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
.capacity-limits > div { display: flex; flex-direction: column; }
.capacity-limits .v-input { flex: 0 0 auto; }
.capacity-limits label { display: block; flex: 1; margin-bottom: 8px; font-size: .875rem; }
.archived-project { border-inline-start: 4px solid; }
.archived-project :deep(.v-list-item__content) { padding-inline-end: 8px; }
.archived-project .v-chip { max-width: 100%; height: auto; min-height: 32px; white-space: normal; overflow-wrap: anywhere; }
</style>
