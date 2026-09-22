<script setup>
// Lifecycle controls and retained history share this drawer; sends still target
// only the current generation. Transcript visibility is a host preference.

import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import {
  mdiRobot,
  mdiClose,
  mdiCogOutline,
  mdiDotsVertical,
  mdiPlayCircleOutline,
  mdiRestart,
  mdiStopCircleOutline,
} from '@mdi/js'
import AssistantSettings from './AssistantSettings.vue'
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
import { chatShowTranscript, refreshChatSettings } from '../operatorChat.js'
import StatusStrip from './StatusStrip.vue'

const props = defineProps({
  now: { type: Number, default: () => Date.now() },
})

const emit = defineEmits(['close'])

// The fleet view's cadence rather than the transcript's: this read answers "is
// there one at all", and the conversation inside does its own faster poll.
const POLL_MS = 10000

const status = ref(null)
const problem = ref(null)
const starting = ref(false)
const restarting = ref(false)
const stopping = ref(false)

// One confirmation per destructive verb rather than one flag holding which verb is
// pending: the copy differs in what it says is lost, so the two dialogs are two
// pieces of prose, and `v-model` on a boolean is the shape the rest of the app
// confirms things with (RunDetail's exit, Terminal's Ctrl-C).
const confirmingRestart = ref(false)
const confirmingStop = ref(false)

// The standing-orders panel: the reply, its own failure, and which panel is open.
// Kept apart from `problem` on purpose — a failed read of the orders must not make
// the drawer report the supervisor as unreachable, and vice versa.
const orders = ref(null)
const ordersProblem = ref(null)
const ordersLoading = ref(false)
const ordersOpen = ref(undefined)

// The settings dialog (issue 234) lives in its own component, and the split is
// what keeps this file's guard a guard: the drawer is pinned to carry no input
// affordance at all — the tempting one being a standing-orders textarea, a second
// writer of a chat-written document (T87) — while launch facts
// (model/harness/preset/agents) are a config file's to edit and a form's to
// write. This file owns only the open flag and the restart the dialog offers,
// which routes through the same confirmation as the header's: ending a context
// window is the same decision however it is reached.
const settingsOpen = ref(false)

let timer = null
let disposed = false
// Bumped by every request that replaces the state, so a slow reply cannot land
// after the drawer has moved on — the same guard Chat.vue and AskChannel.vue use.
let generation = 0

const running = computed(() => !!status.value?.running)
const sessionId = computed(() => status.value ? status.value.session_id || status.value.history_session : null)

// One lifecycle call at a time, and the whole cluster goes with it: the three verbs
// contradict each other, and a stop landing behind a restart would leave nothing
// running while the drawer showed the incarnation the restart had just brought up.
const busy = computed(() => starting.value || restarting.value || stopping.value)

// How many digests the daemon has spooled for uber lmer. A count, never the notes
// themselves: draining that queue is the supervisor's own destructive read and an
// operator peeking must not consume it — api.js has the whole reasoning.
const queued = computed(() => status.value?.pending || 0)

function stale(mine) {
  return disposed || mine !== generation
}

async function load() {
  const mine = generation
  // The pane's view is a platform-side setting (issue 488): re-read on the
  // same tick as the status, so a switch flipped in another browser reaches
  // this one within a poll rather than at its next page load.
  refreshChatSettings()
  try {
    const reply = await fetchAssistant()
    if (stale(mine)) return
    status.value = reply
    problem.value = null
  } catch (exc) {
    if (stale(mine)) return
    // What the daemon last said stays on screen. A drawer that emptied itself
    // because one poll was refused would read as "there is no uber lmer", which
    // is the one thing it must not say wrongly — and it would offer a start
    // button for a session that is running.
    problem.value = exc.message
  }
}

// Re-read on every expand rather than once: uber lmer rewrites this document from
// the chat, so the answer from the last time the panel was open may already be the
// old rules — which is the one thing a read-only view of a document must not show.
function onOrdersToggle(value) {
  if (value !== undefined) loadOrders()
}

async function loadOrders() {
  if (ordersLoading.value) return
  const mine = generation
  ordersLoading.value = true
  try {
    const reply = await fetchAssistantInstructions()
    if (stale(mine)) return
    orders.value = reply
    ordersProblem.value = null
  } catch (exc) {
    if (stale(mine)) return
    // Same degradation as the status read: whatever was last fetched stays, so a
    // refused poll does not read as "the operator has told it nothing".
    ordersProblem.value = exc.message
  } finally {
    if (!stale(mine)) ordersLoading.value = false
  }
}

// One shape for all three lifecycle verbs, because they differ in nothing this
// function does: the route answers with the reconciled status, so the reply *is* the
// new state — which is what lets the conversation open on a start, and follow a
// rotation to the incarnation that replaced the one it was reading, without waiting
// for the next poll. The generation bump is the same guard the reads use: a status
// poll already in flight must not land on top of a reply that superseded it.
async function lifecycle(flag, call) {
  if (busy.value) return
  generation += 1
  const mine = generation
  flag.value = true
  problem.value = null
  try {
    const reply = await call()
    if (stale(mine)) return
    status.value = reply
  } catch (exc) {
    if (stale(mine)) return
    // A 409 is "one is already running" and is not worth reporting as a failure:
    // two taps on a slow connection are exactly how it happens. Re-read instead,
    // which is what the route asks a client to do. Only `start` answers it today —
    // `rotate` starts one deliberately and `stop` refuses nothing — and it is
    // handled here rather than in one caller because what it means is "the daemon's
    // view of what is running differs from ours", which a re-read settles whichever
    // verb heard it.
    if (exc.status === 409) {
      await load()
      return
    }
    // The daemon's own sentence, and nothing is thrown away to show it: a refused
    // restart leaves the incarnation that is still up on screen, with the
    // conversation and any half-typed message in it untouched.
    problem.value = exc.message
  } finally {
    if (!stale(mine)) flag.value = false
  }
}

function startUberLmer() {
  return lifecycle(starting, startAssistant)
}

// The primary control, and the one that fixes the state it was asked for: a wedged
// incarnation is replaced rather than merely ended, in one call, so there is no gap
// for a start to land in and nothing to remember to do afterwards.
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
  <!-- The accent bar is the identity, and it is the theme's `primary` rather than
       a colour of this component's: one place owns the palette (main.js). -->
  <v-toolbar color="primary" density="comfortable">
    <v-icon :icon="mdiRobot" class="ms-4 me-1" />
    <v-toolbar-title class="text-title-medium">uber lmer</v-toolbar-title>

    <!-- The lifecycle cluster, beside the close button because both act on the
         panel's subject rather than on anything in the conversation — and drawn only
         once the daemon has answered, since a control for a state nobody has read
         yet is a guess. Restart carries a word as well as an icon: it is the verb
         that gets tapped in a hurry, and the overflow beside it is two icon-only
         buttons away from being ambiguous already. -->
    <template v-if="status || problem">
      <!-- Uber lmer's own threads. The same control as a run row's, and the same
           three calls: connecting here also moves the standing `threads` launch
           setting, so a thread attached from this header survives a rotation. -->
      <ThreadLinks
        :session-id="sessionId"
        :threads="status?.threads"
        :live="running"
        @changed="load"
      />
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
          <!-- The spinner for a stop belongs here, on what was tapped: the entry
               that started it lives in a menu that is closed by the time the daemon
               answers, and a v-list-item has nowhere to put one. -->
          <v-btn
            v-bind="overflow"
            :icon="mdiDotsVertical"
            :loading="stopping"
            :disabled="busy"
            variant="text"
            aria-label="more uber lmer controls"
          />
        </template>
        <!-- Stop and start are never both here: one of them is always the verb the
             daemon would refuse, and offering it would be offering a 409. -->
        <v-list density="compact">
          <!-- Settings sit in the overflow rather than beside restart: the header
               already carries a worded verb plus two icon buttons, and issue 194's
               lesson was that this toolbar is read on a phone. Offered with no
               uber lmer running too — what is being configured is the NEXT one,
               which is exactly the state a start button is next to. -->
          <v-list-item
            :prepend-icon="mdiCogOutline"
            title="uber lmer settings"
            @click="settingsOpen = true"
          />
          <v-list-item
            v-if="running"
            :prepend-icon="mdiStopCircleOutline"
            title="stop uber lmer"
            @click="confirmingStop = true"
          />
          <v-list-item
            v-else
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

  <!-- Confirmed because each of these ends a context window, and the copy is
       specific about which one and about what outlives it: "are you sure" would put
       the operator's finger on the same tap with nothing new to decide with. Same
       shape as the app's other two confirmations (RunDetail's exit, Terminal's
       Ctrl-C) — a dialog with the sentence in it, cancel first, the verb second. -->
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

  <!-- The settings dialog (issue 234) — its own component, so this file stays
       free of input affordances; see AssistantSettings.vue for everything it
       promises. Its restart lands in the same confirmation as the header's. -->
  <AssistantSettings
    v-model="settingsOpen"
    :running="running"
    :running-settings="status?.settings || null"
    @restart="confirmingRestart = true"
  />

  <v-dialog v-model="confirmingStop" max-width="480">
    <v-card>
      <v-card-text class="text-body-medium">
        Stop uber lmer? This ends the incarnation and its current context.
        Your conversation history remains visible. A daemon that is
        supervising puts a fresh one up on its own within moments; if it has given up
        on starting one, or nothing is supervising, none comes back until you start
        one here. Restart is the one to reach for if what you want is a working
        uber lmer.
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
      The session that supervises this fleet: ask it what is happening, or tell it
      to start, follow up or wind down a run. It is not one of the runs — nothing
      typed here reaches an lmer working in a repository.
    </p>

    <!-- The daemon not answering is its own state, and it degrades rather than
         resets: the alert says so and whatever it last reported stays below. -->
    <v-alert v-if="problem" type="warning" density="compact" class="mb-3">
      {{ problem }}
      <template v-if="status"> — showing what the daemon last reported.</template>
    </v-alert>

    <!-- The first read, and only the first: a later one that fails leaves the
         answer it had rather than replacing it with a spinner. -->
    <div v-if="!status && !problem" class="text-body-small text-medium-emphasis">
      <v-progress-circular indeterminate size="12" width="2" class="me-2" />
      asking the daemon…
    </div>

    <template v-else>
      <!-- Something is waiting for uber lmer, said as a number and nothing more.
           Which matters most in the second case: with none running, nobody is
           reading the queue at all, and that is what the start button is for. -->
      <v-alert v-if="queued" type="info" density="compact" class="mb-3">
        {{ queued }} {{ queued === 1 ? 'digest' : 'digests' }} waiting for uber lmer
        <template v-if="running">— read on its next check.</template>
        <template v-else>— nothing is reading them while none is running.</template>
      </v-alert>

      <!-- What the operator has told uber lmer to always do. Read-only, and the
           panel says so: the chat is the write path, by their own choice. Collapsed
           by default and fetched on expand — the conversation is what the drawer is
           for, and this is a reference. -->
      <v-expansion-panels
        v-model="ordersOpen"
        class="mb-3"
        @update:model-value="onOrdersToggle"
      >
        <v-expansion-panel title="standing orders">
          <v-expansion-panel-text>
            <!-- Its own failure line, not the drawer's: a refused read here says
                 nothing about whether uber lmer is running. -->
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
              <!-- Verbatim, with its own line breaks: this is a short list of
                   rules the operator dictated, and running it into one paragraph
                   would make a rule they wrote unrecognisable. Not rendered as
                   markdown — the renderer belongs to the conversation. -->
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

        <!-- uber lmer's own status line (issue 248), at the run drawer's
             density: what is left of this incarnation's context is what decides
             whether to keep talking or restart. -->
        <StatusStrip :status="status.statusline" :now="now" class="mb-3" />

        <!-- A lifecycle call in flight, said where the incarnation it is about is
             described: the cluster's spinner says something is happening and this
             says what, which matters most for the restart — the conversation below
             is still the old one until the daemon answers. -->
        <p v-if="restarting" class="text-body-small text-medium-emphasis mb-2">
          restarting — the conversation below is still the incarnation being
          replaced, until the daemon answers with the new one.
        </p>
        <p v-else-if="stopping" class="text-body-small text-medium-emphasis mb-2">
          stopping — the daemon answers once the session is gone.
        </p>

        <!-- Above the composer, on purpose: the whole conversation below is with
             uber lmer and not with any run, and this is the last line read before
             typing. -->
        <p class="text-body-small mb-2">You are talking to uber lmer.</p>

        <!-- Two names passed down, and the same reason for both: the shared
             chat's defaults are a worker's, and "this session" or "lmer" in a
             drawer that is deliberately not one of the runs is the one wording
             the whole panel exists to avoid. The label is the last line read
             before a message goes; the title is on every turn above it. -->
      </template>

      <v-card v-else>
        <v-card-text>
          <p class="text-body-medium mb-2">No uber lmer is running.</p>
          <p class="text-body-small text-medium-emphasis mb-3">
            The daemon starts one and keeps it up, so this usually resolves itself
            within a poll or two; starting one here is the way past a start that
            has not happened.
            <!-- Optional chaining throughout, because `status` is legitimately
                 null here: `load()`'s catch leaves it null and sets `problem`, and
                 the spinner above is gated on *both* being absent — so a first
                 poll that fails renders this card with nothing behind it. That is
                 the path an operator is on when the daemon is restarting or
                 answering 401, i.e. exactly when the drawer was opened to find
                 out what is wrong; a bare deref throws on every re-render and
                 leaves the subtree blank until a reload. -->
            <template v-if="status?.stale">
              The one on record (generation {{ status.generation }}) is gone — its
              session is still named in the daemon's state, which is how the crash
              stays visible.
            </template>
            <!-- The other way to arrive here, and worth telling apart: a stop clears
                 the pointer, a crash leaves it, so `stale` is what distinguishes an
                 uber lmer somebody ended from one that died. -->
            <template v-else-if="status?.generation">
              Generation {{ status.generation }} left no session behind, which is what
              a stop looks like from here rather than a crash.
            </template>
          </p>
          <p v-if="starting" class="text-body-small text-medium-emphasis mb-3">
            starting — the daemon answers once the session is up, which takes a
            moment.
          </p>
          <v-btn
            :prepend-icon="mdiPlayCircleOutline"
            :loading="starting"
            :disabled="busy"
            color="primary"
            @click="startUberLmer"
          >start uber lmer</v-btn>
        </v-card-text>
      </v-card>
      <Chat
        v-if="sessionId"
        assistant-history
        :session-id="sessionId"
        :now="now"
        :view="chatShowTranscript ? 'transcript' : 'conversation'"
        composer-label="say something to uber lmer"
        agent-label="uber lmer"
      />
    </template>
  </div>
</template>

<style scoped>
/* Standing orders are dictated prose with newlines in them — a rule per line is how
   the operator said it, and how uber lmer stores it. Collapsing that would run three
   rules into one sentence. `anywhere` for the same reason the channel view has it: a
   preset name or a path must not scroll a phone sideways. */
.orders {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
</style>
