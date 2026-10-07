<script setup>

import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  mdiBellRingOutline,
  mdiRobot,
  mdiClose,
  mdiCogOutline,
  mdiDotsVertical,
  mdiPlayCircleOutline,
  mdiRestart,
  mdiStopCircleOutline,
} from '@mdi/js'
import ThreadLinks from './ThreadLinks.vue'
import Chat from './Chat.vue'
import {
  fetchAssistant,
  fetchAssistantInstructions,
  rotateAssistant,
  startAssistant,
  stopAssistant,
} from '../api.js'
import { ago } from '../format.js'
import { projectColor, readDraft, storeDraft } from '../projects.js'
import { chatShowTranscript, refreshChatSettings } from '../operatorChat.js'
import StatusStrip from './StatusStrip.vue'

const props = defineProps({
  workspace: { type: Object, required: true },
  now: { type: Number, default: () => Date.now() },
})

const emit = defineEmits(['close', 'settings', 'questions'])
const POLL_MS = 10000

const draftKey = `assistant:${props.workspace.id}`
const waitingDraft = ref(readDraft(draftKey).text)
watch(waitingDraft, text => storeDraft(draftKey, text, []))
const status = ref(null)
const problem = ref(null)
const starting = ref(false)
const restarting = ref(false)
const stopping = ref(false)
const confirmingRestart = ref(false)
const confirmingStop = ref(false)
const orders = ref(null)
const ordersProblem = ref(null)
const ordersLoading = ref(false)
const ordersOpen = ref(undefined)

let timer = null
let disposed = false
let generation = 0

const running = computed(() => !!status.value?.running)
const rotating = computed(() => status.value?.lifecycle === 'rotating')
const composerActive = computed(() => running.value || rotating.value)
watch(running, active => { if (!active) waitingDraft.value = readDraft(draftKey).text })
const sessionId = computed(() => status.value ? status.value.session_id || status.value.history_session : null)
const busy = computed(() => starting.value || restarting.value || stopping.value)
const queued = computed(() => status.value?.pending || 0)
// uber lmer's own unanswered asks to the operator (issue 507), counted by the daemon
// from the same ask channel the run view answers them in.
const openQuestions = computed(() => (running.value && status.value?.open_questions) || 0)

function stale(mine) {
  return disposed || mine !== generation
}

async function load() {
  const mine = generation
  refreshChatSettings()
  try {
    const reply = await fetchAssistant(props.workspace.id)
    if (stale(mine)) return
    status.value = reply
    problem.value = null
  } catch (exc) {
    if (stale(mine)) return
    problem.value = exc.message
  }
}
function onOrdersToggle(value) {
  if (value !== undefined) loadOrders()
}

async function loadOrders() {
  if (ordersLoading.value) return
  const mine = generation
  ordersLoading.value = true
  try {
    const reply = await fetchAssistantInstructions(props.workspace.id)
    if (stale(mine)) return
    orders.value = reply
    ordersProblem.value = null
  } catch (exc) {
    if (stale(mine)) return
    ordersProblem.value = exc.message
  } finally {
    if (!stale(mine)) ordersLoading.value = false
  }
}
async function lifecycle(flag, call) {
  if (busy.value) return
  generation += 1
  const mine = generation
  flag.value = true
  problem.value = null
  try {
    const reply = await call(props.workspace.id)
    if (stale(mine)) return
    status.value = reply
  } catch (exc) {
    if (stale(mine)) return
    problem.value = exc.message
  } finally {
    if (!stale(mine)) flag.value = false
  }
}

function startUberLmer() {
  return lifecycle(starting, startAssistant)
}
function restartUberLmer() {
  confirmingRestart.value = false
  return lifecycle(restarting, rotateAssistant)
}

function stopUberLmer() {
  confirmingStop.value = false
  return lifecycle(stopping, stopAssistant)
}

onMounted(() => {
  load()
  timer = setInterval(load, POLL_MS)
})

onBeforeUnmount(() => {
  disposed = true
  generation += 1
  clearInterval(timer)
})
</script>

<template>
  <v-toolbar :color="projectColor(workspace.color)" density="comfortable" class="assistant-header">
    <v-icon :icon="mdiRobot" class="ms-4 me-1" />
    <v-toolbar-title class="text-title-medium project-chat-title">{{ workspace.name }} · uber lmer</v-toolbar-title>
    <template v-if="status || problem">
      <ThreadLinks
        :session-id="sessionId"
        :workspace-id="workspace.id"
        :threads="status?.threads"
        :live="running"
        @changed="load"
      />
      <v-btn
        v-if="openQuestions"
        variant="text"
        size="small"
        class="open-questions"
        :aria-label="`${openQuestions} open ${openQuestions === 1 ? 'question' : 'questions'} from uber lmer — answer in its run view`"
        :title="`${openQuestions} open ${openQuestions === 1 ? 'question' : 'questions'} — answer in uber lmer's run view`"
        @click="emit('questions', sessionId)"
      >
        <v-badge :content="openQuestions" color="warning" floating>
          <v-icon :icon="mdiBellRingOutline" />
        </v-badge>
      </v-btn>
      <v-btn
        v-if="running"
        :prepend-icon="mdiRestart"
        :loading="restarting"
        :disabled="busy"
        variant="text"
        size="small"
        @click="confirmingRestart = true"
      >restart</v-btn>

      <v-menu>
        <template #activator="{ props: overflow }">
          <v-btn
            v-bind="overflow"
            :icon="mdiDotsVertical"
            :loading="stopping"
            :disabled="busy"
            variant="text"
            aria-label="more uber lmer controls"
          />
        </template>
        <v-list density="compact">
          <v-list-item
            :prepend-icon="mdiCogOutline"
            title="Project settings"
            @click="emit('settings')"
          />
          <v-list-item
            v-if="running"
            :prepend-icon="mdiStopCircleOutline"
            title="stop uber lmer"
            @click="confirmingStop = true"
          />
          <v-list-item
            v-else-if="status?.lifecycle !== 'waiting'"
            :prepend-icon="mdiPlayCircleOutline"
            title="start uber lmer"
            @click="startUberLmer"
          />
        </v-list>
      </v-menu>
    </template>

    <v-btn
      :icon="mdiClose"
      variant="text"
      aria-label="close the uber lmer chat"
      @click="emit('close')"
    />
  </v-toolbar>
  <v-dialog v-model="confirmingRestart" max-width="480">
    <v-card>
      <v-card-text class="text-body-medium">
        Restart uber lmer? This ends the incarnation you are talking to and starts a
        fresh one, so its current context is lost. Your conversation history remains visible.
        Standing orders survive, and
        the new one starts from the handover note already on record — nothing here
        writes a new one, so it is as recent as the last note uber lmer wrote.
      </v-card-text>
      <v-card-actions>
        <v-spacer />
        <v-btn variant="tonal" @click="confirmingRestart = false">cancel</v-btn>
        <v-btn color="warning" variant="tonal" @click="restartUberLmer">restart</v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>

  <v-dialog v-model="confirmingStop" max-width="480">
    <v-card>
      <v-card-text class="text-body-medium">
        Stop uber lmer? This ends the incarnation and its current context.
        Your conversation history and pending work remain in {{ workspace.name }}.
        It stays stopped until you choose Start.
      </v-card-text>
      <v-card-actions>
        <v-spacer />
        <v-btn variant="tonal" @click="confirmingStop = false">cancel</v-btn>
        <v-btn color="error" variant="tonal" @click="stopUberLmer">stop</v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>

  <div class="pa-3">
    <p class="text-body-small text-medium-emphasis mb-3">
      The assistant for {{ workspace.name }}. Requests, instructions and history in
      this chat belong to this project.
    </p>
    <v-alert v-if="problem" type="warning" density="compact" class="mb-3">
      {{ problem }}
      <template v-if="status"> — showing what the daemon last reported.</template>
    </v-alert>
    <div v-if="!status && !problem" class="text-body-small text-medium-emphasis">
      <v-progress-circular indeterminate size="12" width="2" class="me-2" />
      asking the daemon…
    </div>

    <template v-else>
      <v-alert v-if="queued" type="info" density="compact" class="mb-3">
        {{ queued }} {{ queued === 1 ? 'digest' : 'digests' }} waiting for uber lmer
        <template v-if="running">— read on its next check.</template>
        <template v-else>— nothing is reading them while none is running.</template>
      </v-alert>
      <v-expansion-panels
        v-model="ordersOpen"
        class="mb-3"
        @update:model-value="onOrdersToggle"
      >
        <v-expansion-panel title="standing orders">
          <v-expansion-panel-text>
            <v-alert
              v-if="ordersProblem"
              type="warning"
              density="compact"
              class="mb-2"
            >
              {{ ordersProblem }}
            </v-alert>

            <div
              v-if="ordersLoading && !orders"
              class="text-body-small text-medium-emphasis"
            >
              <v-progress-circular indeterminate size="12" width="2" class="me-2" />
              reading…
            </div>

            <template v-else-if="orders">
              <p v-if="orders.instructions" class="text-body-small orders">
                {{ orders.instructions }}
              </p>
              <p v-else class="text-body-small text-medium-emphasis">
                You have not told uber lmer to always do anything yet.
              </p>

              <p class="text-body-small text-medium-emphasis mt-2 mb-0">
                Say it in the chat to change this — "from now on…", "always…",
                "stop…" — and uber lmer confirms the wording before it stores it.
                There is nothing to edit here on purpose.
              </p>
              <p
                v-if="orders.instructions_at"
                :title="orders.instructions_at"
                class="text-body-small text-medium-emphasis mb-0"
              >
                last changed {{ ago(orders.instructions_at, now) }}
              </p>
            </template>
          </v-expansion-panel-text>
        </v-expansion-panel>
      </v-expansion-panels>

      <template v-if="running">
        <div class="d-flex flex-wrap ga-2 text-body-small text-medium-emphasis mb-2">
          <span>generation {{ status.generation }}</span>
          <span v-if="status.started_at" :title="status.started_at">
            started {{ ago(status.started_at, now) }}
          </span>
        </div>
        <StatusStrip :status="status.statusline" :now="now" meter-color="primary" hideable class="mb-3" />
        <p v-if="restarting" class="text-body-small text-medium-emphasis mb-2">
          restarting — the conversation below is still the incarnation being
          replaced, until the daemon answers with the new one.
        </p>
        <p v-else-if="stopping" class="text-body-small text-medium-emphasis mb-2">
          stopping — the daemon answers once the session is gone.
        </p>
        <p class="text-body-small mb-2">You are talking to {{ workspace.name }} · uber lmer.</p>
      </template>

      <p v-else-if="rotating" class="text-body-small mb-3">Restarting {{ workspace.name }} · uber lmer. Your draft and attachments stay here; sending resumes when it is ready.</p>
      <v-card v-else class="mb-3">
        <v-card-text>
          <p class="text-body-medium mb-2">{{ workspace.name }} · {{ status?.lifecycle || 'Unavailable' }}</p>
          <p v-if="status?.queue_position" class="mb-2">Waiting to start — assistant limit reached · queue position {{ status.queue_position }}</p>
          <v-alert v-if="status?.error" type="warning" class="mb-2">{{ status.error }}</v-alert>
          <p v-if="status?.lifecycle === 'stopped'" class="mb-2">Stopped until you choose Start. Pending work is retained.</p>
          <v-btn v-if="status?.lifecycle === 'waiting'" :loading="stopping" :disabled="busy" @click="stopUberLmer">Cancel start</v-btn>
          <v-btn v-else :prepend-icon="mdiPlayCircleOutline" :loading="starting" :disabled="busy || !status"
            color="primary" @click="startUberLmer">{{ status?.lifecycle === 'failed' ? 'Retry' : 'Start' }}</v-btn>
        </v-card-text>
      </v-card>
      <div v-if="!composerActive" class="mt-3">
        <v-textarea v-model="waitingDraft" :label="`Draft for ${workspace.name} · uber lmer`" />
        <p class="text-body-small">Sending becomes available when this project's assistant is ready. Your draft stays here.</p>
      </div>
      <Chat
        v-if="sessionId"
        :key="composerActive ? 'active' : 'history'"
        assistant-history
        :read-only="!composerActive"
        :send-paused="rotating"
        :session-id="sessionId"
        :workspace-id="workspace.id"
        :now="now"
        :view="chatShowTranscript ? 'transcript' : 'conversation'"
        :composer-label="`Say something to ${workspace.name} · uber lmer`"
        :conversation-key="composerActive ? draftKey : `${draftKey}:history`"
        agent-label="uber lmer"
      />
    </template>
  </div>
</template>

<style scoped>
.project-chat-title { white-space: normal; overflow-wrap: anywhere; min-width: 180px; flex: 1 1 220px; }
.project-chat-title :deep(.v-toolbar-title__placeholder) { white-space: normal; overflow: visible; text-overflow: clip; }
.assistant-header { height: auto; min-height: 64px; }
.assistant-header :deep(.v-toolbar__content) { height: auto !important; min-height: 64px; flex-wrap: wrap; }

.orders {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
</style>
