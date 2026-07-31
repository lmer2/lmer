<script setup>
// The chat with uber lmer — the one session that supervises the others (spec §8,
// §10.1), docked in the right-hand drawer.
//
// The names first, because they are the safety feature. The UI calls a worker
// session **lmer** and the supervising one **uber lmer** (operator request,
// 2026-07-27), and the word "assistant" is shown to an operator nowhere: it is the
// code's spelling only — the module, the taskdef and every API field keep it. The
// failure this drawer is shaped around is telling the supervisor to stop something
// while actually typing at the run itself, and two visibly different names plus two
// different edges of the screen are what stand between an operator and that
// mistake. Hence also the accent header rather than the plain surface the run
// navigator has on the left: the two panels must not be tellable apart by their
// contents alone.
//
// What is reused rather than rebuilt
// ----------------------------------
// uber lmer *is* an lmer session with a session id, which the status reports — so
// the conversation is `Chat.vue` pointed at that id. Same transcript read, same
// control-plane write, same held-back bubble for a message the transcript has not
// caught up with, same shared markdown renderer behind it (which Chat defers into
// its own chunk; nothing about it is duplicated here). None of that machinery is
// run-scoped — it keys on a session — so a second implementation would only be a
// second place to get the delay between sending and seeing it wrong.
//
// Chat's two run-shaped props are left at their defaults deliberately:
// `questionPending` is a run that exited on a question and `askPending` is a
// worker's ask channel. This session has neither a run nor a channel; the
// supervisor is asked things by being told them here.
//
// What this component owns is the *lifecycle*, which a worker chat never has to
// think about: a run's session exists because you opened the run, while uber lmer
// may not be running at all. Four states have to be readable — none running,
// starting, running, and a daemon that did not answer — and the last one is why a
// failed poll leaves the previous answer on screen instead of emptying the drawer.
//
// Standing orders are shown and never edited (T87)
// ------------------------------------------------
// The operator can tell uber lmer to always do something, and it keeps that as a
// document every later incarnation reads. This panel is how they can see what it
// currently believes, because the chat is a place you say things and not a place you
// re-read a document. There is deliberately **no editing affordance**: the operator
// asked for the chat to be the write path rather than "some ux config thing", so a
// textarea here would be a second writer of the same prose, editing a document
// stated in their own words, with nothing to confirm the wording back to them.
//
// Fetched when the panel is expanded rather than on the status poll, which keeps the
// drawer's cost where it already was: the fleet view pays nothing until the drawer is
// opened (App.vue mounts it lazily), and the drawer pays nothing for standing orders
// until they are asked for. They change a few times a month; the status changes every
// ten seconds.
//
// The lifecycle cluster (T126)
// ----------------------------
// Restart, stop and start, in the header, because the alternative was a host-side
// restart of the daemon: an incarnation that stopped answering could not be cleared
// from here at all, and clearing it that way takes the whole platform down with it.
// So the three verbs the daemon already serves are reachable from the panel the
// operator noticed the problem in (operator request, 2026-07-29).
//
// Restart is `POST /api/assistant/rotate` and it is the primary one for one reason:
// it always leaves something running. It replaces a live incarnation and it starts
// one when nothing is up, in a single call, so it cannot leave the fleet
// unsupervised the way a stop that nothing followed would. Stop is behind an
// overflow because it leaves nothing, and start only exists while nothing is up —
// against a live one the route answers 409 on purpose, to protect the context
// window of the incumbent.
//
// Both destructive verbs are confirmed, and the copy says what is actually lost:
// the conversation window, and only that. Standing orders survive a rotation and a
// stop-and-start alike, because `assistant.start` and `assistant.stop` edit state
// with `dataclasses.replace` and nothing consumes that document — and the handover
// note carries forward untouched when no new one is sent, which is what the
// starting incarnation is told to read before it says anything (the `orchestrate`
// taskdef). What the confirm does *not* claim is that a stop keeps uber lmer down:
// the daemon's supervisor respawns anything that is not running, whatever ended it,
// so a stop holds only for as long as nothing is supervising — which is the case
// the start button exists for.
//
// The conversation follows the replacement with no machinery of its own: each of
// these routes answers with the reconciled status, the reply becomes the status, the
// session id changes with it, and `Chat.vue` restarts its transcript when the id it
// was given changes. A rotation is exactly that — a different session — so nothing
// here has to reach into the chat, and nothing has to be reloaded.

import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import {
  mdiRobot,
  mdiClose,
  mdiDotsVertical,
  mdiPlayCircleOutline,
  mdiRestart,
  mdiStopCircleOutline,
} from '@mdi/js'
import Chat from './Chat.vue'
import {
  fetchAssistant,
  fetchAssistantInstructions,
  rotateAssistant,
  startAssistant,
  stopAssistant,
} from '../api.js'
import { ago } from '../format.js'

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

let timer = null
let disposed = false
// Bumped by every request that replaces the state, so a slow reply cannot land
// after the drawer has moved on — the same guard Chat.vue and AskChannel.vue use.
let generation = 0

const running = computed(() => !!status.value?.running)
const sessionId = computed(() => (running.value ? status.value.session_id : null))

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
        fresh one, so the conversation window is lost. Standing orders survive, and
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
        Stop uber lmer? This ends the incarnation and the conversation window goes
        with it, and nothing in this drawer brings one back. A daemon that is
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
        <Chat
          :session-id="sessionId"
          :now="now"
          composer-label="say something to uber lmer"
          agent-label="uber lmer"
        />
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
