<script setup>

import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  mdiBellRingOutline,
  mdiRobot,
  mdiClose,
  mdiCogOutline,
  mdiPlayCircleOutline,
  mdiRestart,
  mdiScriptTextOutline,
  mdiStopCircleOutline,
  mdiUnfoldMoreHorizontal,
} from '@mdi/js'
import ThreadLinks from './ThreadLinks.vue'
import Chat from './Chat.vue'
import {
  fetchAssistant,
  rotateAssistant,
  startAssistant,
  stopAssistant,
} from '../api.js'
import { ago } from '../format.js'
import { projectColor, projectTextColor, readDraft, storeDraft } from '../projects.js'
import { chatShowTranscript, refreshChatSettings } from '../operatorChat.js'
import StatusStrip from './StatusStrip.vue'

const props = defineProps({
  workspace: { type: Object, required: true },
  now: { type: Number, default: () => Date.now() },
})

// `settings` carries the section to open: 'orders' for the standing-orders button.
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
const chat = ref(null)

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
const questionWords = computed(() => `${openQuestions.value} open ${openQuestions.value === 1 ? 'question' : 'questions'}`)
const loaded = computed(() => chat.value?.total || 0)
const canLoadEarlier = computed(() => !!chat.value?.canLoadEarlier)
// The toolbar holds the bell whenever there is no composer to hold it (a recorded
// conversation, a failed or not-yet-read transcript), so it never vanishes.
const bellOnToolbar = computed(() => !!openQuestions.value && !chat.value?.composerOpen)

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
  <div class="uber-pane" role="region" :aria-label="`${workspace.name} · uber lmer`">
    <v-toolbar :color="projectColor(workspace.color)" :style="{ color: projectTextColor(workspace.color) }"
      density="compact" class="uber-toolbar">
      <v-icon :icon="mdiRobot" size="small" class="ms-3 me-1" :title="`${workspace.name} · uber lmer`" />
      <template v-if="status || problem">
        <ThreadLinks
          :session-id="sessionId"
          :workspace-id="workspace.id"
          :threads="status?.threads"
          :live="running"
          @changed="load"
        />
        <v-btn
          v-if="running"
          :icon="mdiRestart"
          :loading="restarting"
          :disabled="busy"
          variant="text"
          size="small"
          density="comfortable"
          aria-label="restart uber lmer"
          title="restart uber lmer"
          @click="confirmingRestart = true"
        />
        <v-btn
          v-if="running"
          :icon="mdiStopCircleOutline"
          :loading="stopping"
          :disabled="busy"
          variant="text"
          size="small"
          density="comfortable"
          aria-label="stop uber lmer"
          title="stop uber lmer"
          @click="confirmingStop = true"
        />
      </template>
      <v-btn
        :icon="mdiCogOutline"
        variant="text"
        size="small"
        density="comfortable"
        aria-label="project settings"
        title="project settings"
        @click="emit('settings')"
      />
      <v-btn
        :icon="mdiScriptTextOutline"
        variant="text"
        size="small"
        density="comfortable"
        aria-label="standing orders"
        title="standing orders — opens project settings"
        @click="emit('settings', 'orders')"
      />
      <v-spacer />
      <v-btn
        v-if="bellOnToolbar"
        variant="text"
        size="small"
        density="comfortable"
        icon
        class="open-questions"
        :aria-label="`${questionWords} from uber lmer — answer in its run view`"
        :title="`${questionWords} — answer in uber lmer's run view`"
        @click="emit('questions', sessionId)"
      >
        <v-badge :content="openQuestions" color="warning" floating>
          <v-icon :icon="mdiBellRingOutline" />
        </v-badge>
      </v-btn>
      <span v-if="loaded" class="text-body-small loaded-count">{{ loaded }} messages loaded</span>
      <v-btn
        v-if="canLoadEarlier"
        :icon="mdiUnfoldMoreHorizontal"
        variant="text"
        size="small"
        density="comfortable"
        aria-label="earlier messages"
        title="earlier messages"
        @click="chat?.loadEarlier()"
      />
      <v-btn
        :icon="mdiClose"
        variant="text"
        size="small"
        density="comfortable"
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

    <div class="uber-body pa-3">
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
          ref="chat"
          :key="composerActive ? 'active' : 'history'"
          fill
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
        >
          <!-- The slot keeps its width without the bell, so the text never reflows.
               `.stop`: a click in the field focuses it and raises a phone keyboard. -->
          <template #composer-inner>
            <span class="question-slot">
              <v-btn
                v-if="openQuestions && !bellOnToolbar"
                variant="text"
                size="small"
                density="comfortable"
                icon
                class="open-questions"
                :aria-label="`${questionWords} from uber lmer — answer in its run view`"
                :title="`${questionWords} — answer in uber lmer's run view`"
                @mousedown.stop
                @click.stop="emit('questions', sessionId)"
              >
                <v-badge :content="openQuestions" color="warning" floating>
                  <v-icon :icon="mdiBellRingOutline" />
                </v-badge>
              </v-btn>
            </span>
          </template>
        </Chat>
      </template>
    </div>
  </div>
</template>

<style scoped>
/* `min-height`, not `height`: a short window scrolls the drawer rather than
   squeezing the conversation to nothing. */
.uber-pane {
  display: flex;
  flex-direction: column;
  min-height: 100%;
}
.uber-toolbar { flex: none; }
.uber-toolbar :deep(.v-toolbar__content) { flex-wrap: wrap; height: auto !important; min-height: 48px; }
.uber-body {
  display: flex;
  flex-direction: column;
  flex: 1 1 0;
}
.loaded-count { white-space: nowrap; }
.question-slot {
  /* A disabled field (mid-send) sets `pointer-events: none` on its input. */
  pointer-events: auto;
  display: inline-flex;
  justify-content: center;
  width: 36px;
}
</style>
