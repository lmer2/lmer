<script setup>
// One session's ask channel, docked: what it needs from you now, and what has just
// happened on it (spec D26/D27, T23, T40).
//
// Read from `GET api/sessions/{id}/ask`, which is a directory the platform
// mounted into the container — not the transcript and not the PTY log. That is
// the whole reason this view exists: a question the agent typed into its terminal
// is somewhere in the scrollback next to everything else, while a question posted
// here is a record with an id, an optional list of options, and a place for the
// answer to go.
//
// The section renders nothing at all when there is nothing on it, which is almost
// every run. An empty card headed "operator channel" would be noise on every
// detail view for a feature most sessions never use.
//
// Two views of one channel (T40)
// ------------------------------
// This one is docked above the tabs, where a remembered tab cannot hide it, and it
// answers "is anything waiting on me". AskHistory.vue is the whole record, in the
// lmer tab's operator-chat pane, and it answers "what was said". They are separate
// components rather than one with a mode because they are answering different
// questions: this one is allowed to *stop showing* an entry, and the record is not.
//
// Which is what makes clearing safe. Clearing is a view operation: it dismisses an
// entry from this dock and never touches the channel, so everything cleared is
// still in the history tab, with its text, its timestamps and its answer. The line
// under the card says so, because a "clear" that quietly destroyed the record would
// make the other view a lie.
//
// Where the cleared state lives, and why it lives there
// ----------------------------------------------------
// In dismissals.js — a module-level store keyed by session, for as long as the page
// is loaded. Not in this component, and not in the browser's storage, and the two
// halves of that are decided by different arguments.
//
// Not this component, because it is rebuilt every time a run is left and re-entered:
// a dismissal held here was gone by the next visit and the entries were back, which
// is a clear button that does not work. The operator reported exactly that.
//
// Not storage, because what "cleared" says is "not now". A dismissal that outlived
// the page would hide a stranded container days later behind a preference nobody can
// find, and the value it would have to keep — a per-run set of question ids — is the
// one shape preferences.js cannot validate (its contract is that a remembered value
// is checked against what exists now). So there is still no storage key and still no
// way for a stale value to blank a view; a reload brings the whole channel back.
//
// A question a live session is still blocked on is not clearable either way: that
// entry is the one thing this view exists to show.
//
// Polling only while the session is live: once it has exited nothing can arrive
// and nothing can be answered, so the feed is a record and one read is enough.
//
// A question has three ends, not two: answered, still open, or closed by the
// session itself (`lmer-ask close`) because it stopped waiting. The third one is
// shown as a record with a line saying so — never silently dropped, and never with
// a reply box, which the server would refuse anyway.
//
// A reply box is offered only while the session is live, and that is a refusal
// rather than a caution: the channel is that one session's directory, so once it
// has exited nothing will ever read a reply — not even the resumed run, which gets
// a channel of its own. The server refuses one (410, lmer_platform/ask.py), so an
// open question from a dead session gets the sentence in place of the box instead
// of a warning underneath a working one. The question itself stays on the page as
// a record, for the same reason a closed one does.
//
// What one entry looks like is not decided here: AskEntry.vue draws it, for both
// views, and its header says why that had to stop being a copy. Which entries are
// drawn, and what may be done with one, is this file's whole subject.

import {
  computed, onBeforeUnmount, onMounted, ref, watch,
} from 'vue'
import { mdiNotificationClearAll } from '@mdi/js'
import AskBox from './AskBox.vue'
import AskEntry from './AskEntry.vue'
import { fetchSessionAsk } from '../api.js'
import { clearedIds, rememberCleared } from '../dismissals.js'

const props = defineProps({
  sessionId: { type: String, required: true },
  // Whether the session is still running — the liveness the server refuses on,
  // not reachability, so a detached session still gets its box (spec D23). Decides
  // both the polling and whether a reply box is offered at all: an unanswered
  // question from a session that has exited is a record, because nothing is
  // reading the directory any more.
  live: { type: Boolean, default: false },
  // The open questions the fleet payload already carried, so the reply box is on
  // screen before the first fetch returns. Replaced by the full feed a moment
  // later — a phone opening a waiting run should not watch a spinner first.
  initial: { type: Array, default: () => [] },
  now: { type: Number, default: () => Date.now() },
})

const emit = defineEmits(['answered'])

// Slower than the terminal, same as the transcript: a human is typing the reply
// at the other end, and each poll is a directory listing on the daemon.
const POLL_MS = 5000

const entries = ref([...props.initial])
const problem = ref(null)
// The ids this operator has dismissed from the dock, read out of the page-lifetime
// store rather than started empty: this component is remounted by leaving and
// re-entering the run, and starting empty there is the bug (see the header). An
// array rather than a Set for one reason: it is read in the template, and a plain
// array's reactivity is not something anybody has to think about at the next edit.
const dismissed = ref(clearedIds(props.sessionId))
let timer = null
let disposed = false
let generation = 0

// A question the session closed is not open any more — it stopped waiting, and the
// server refuses a reply to it with a 409. Offering the box would be inviting the
// operator to type something nothing will read, which is the whole point of the
// close verb. It moves into the record below instead of vanishing: a question that
// had been on screen for ten minutes must not disappear without a word, or the
// operator is left wondering what they missed.
//
// `open` is a fact about the question; whether a box is drawn for it is a fact
// about the session, and `waiting` makes that decision once. Once the session is
// gone nothing is waiting — including the question that never got its answer,
// which becomes a record like the rest.
const open = computed(() => entries.value.filter(
  (entry) => entry.kind === 'question' && !entry.answered && !entry.closed,
))
const waiting = computed(() => (props.live ? open.value : []))

// Everything else the channel has, minus what has been cleared: notes, answered
// questions (so a reply the operator just sent is visibly landed rather than
// silently gone), the ones the session closed, and — on a session that has exited
// — the questions nothing will ever read.
const recent = computed(() => entries.value.filter(
  (entry) => !waiting.value.includes(entry) && !dismissed.value.includes(entry.id),
))

// The sentence in place of the reply box, and it goes with the questions it is
// about: once those have been cleared there is nothing on screen for it to
// explain, and a standing warning about a session that ended hours ago is the
// noise this dock exists to keep out.
const stranded = computed(() => !props.live && recent.value.some(
  (entry) => entry.kind === 'question' && !entry.answered && !entry.closed,
))

const clearedCount = computed(() => dismissed.value.length)
const empty = computed(
  () => !waiting.value.length && !recent.value.length
    && !clearedCount.value && !problem.value,
)

// Only what is not waiting on an answer: a live session's open question is the one
// thing here that another human being is blocked on, and hiding it would be this
// view lying about the only thing it is for. It is also why the button says what
// it does rather than "dismiss all".
function clear() {
  dismissed.value = rememberCleared(
    props.sessionId, recent.value.map((entry) => entry.id),
  )
}

async function load() {
  const mine = generation
  try {
    const page = await fetchSessionAsk(props.sessionId)
    if (disposed || mine !== generation) return
    entries.value = page.entries || []
    problem.value = null
  } catch (exc) {
    if (disposed || mine !== generation) return
    // The feed already rendered stays: losing a question because one poll failed
    // is worse than showing it with a line saying the last read did not land.
    problem.value = exc.message
  }
}

function start() {
  generation += 1
  clearInterval(timer)
  // A dismissal belongs to the session it was made against: the ids are that
  // channel's, and carrying them into another one could hide an entry nobody has
  // seen. Which is what keying the store by session buys — this reads back what was
  // cleared from *this* channel, and a switch of session therefore starts from that
  // session's own set rather than from the previous one's.
  dismissed.value = clearedIds(props.sessionId)
  load()
  if (props.live) timer = setInterval(load, POLL_MS)
}

// A reply changes the feed immediately; do not make the operator wait a poll to
// see their own answer land, and tell the parent so the fleet row catches up.
function answered() {
  emit('answered')
  load()
}

onMounted(start)
onBeforeUnmount(() => {
  disposed = true
  generation += 1
  clearInterval(timer)
})

watch(() => props.sessionId, start)
watch(() => props.live, start)
</script>

<template>
  <div v-if="!empty">
    <div class="section-title">operator channel</div>

    <v-alert v-if="problem" type="error" density="compact" class="mb-3">
      {{ problem }}
    </v-alert>

    <!-- Open questions first, and ahead of everything else here: this is the one
         thing on the page that another human being is blocked on. Only while there
         is someone to unblock, though — see below. -->
    <AskBox
      v-for="question in waiting"
      :key="question.id"
      :session-id="sessionId"
      :question="question"
      :now="now"
      @answered="answered"
    />

    <!-- In place of the box, not under it: a reply to a session that has exited
         cannot be delivered at all, and a box that took one would report it
         delivered. The question is in the record below, where it keeps its text
         and its timestamp. -->
    <v-alert
      v-if="stranded"
      type="warning"
      density="compact"
      class="mb-3"
    >
      This session has ended, so nothing is reading the channel any more — a reply
      cannot reach it. Resume the run to continue it.
    </v-alert>

    <v-card v-if="recent.length" class="mb-3">
      <v-card-text>
        <!-- One card per entry, inside the dock's own, and drawn by the component
             the record draws them with (#254, #274): these are the same entries,
             and a dock that separated them where the record did not would be one
             channel read two ways. What this view asks it for is the one line the
             record has no use for — that a reply can no longer be delivered, which
             here stands where the box would have been. -->
        <AskEntry
          v-for="entry in recent"
          :key="entry.id"
          :entry="entry"
          :live="live"
          :now="now"
          unreachable
        />

        <!-- Clearing is a view operation and the button says which view: what
             leaves is this dock, and the channel is untouched. Offered only when
             there is something to clear, and never for a question a live session
             is still blocked on. -->
        <v-btn
          :prepend-icon="mdiNotificationClearAll"
          variant="tonal"
          size="small"
          class="mt-2"
          @click="clear"
        >clear from here</v-btn>
      </v-card-text>
    </v-card>

    <!-- What clearing did, in the one place it could be misread. The record is
         still whole and this says where it is — without it, an operator who
         cleared a channel has no way of knowing they can read it back. -->
    <p v-if="clearedCount" class="text-body-small text-medium-emphasis">
      {{ clearedCount }} cleared from here this visit. Nothing was deleted — the
      whole channel, answers included, is in the lmer tab's operator chat.
    </p>
  </div>
</template>
