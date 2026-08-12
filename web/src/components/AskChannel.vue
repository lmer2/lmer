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
// What the agent wrote is rendered, what you replied is not — the same split the
// conversation view makes, for the same two reasons. A note posted here is written
// like a message to a person (options as a list, a command in backticks) and shown
// verbatim it is a wall of punctuation; a reply of yours went to the session as
// bytes, and a line that quietly ate a pair of asterisks would be misreporting
// what it got. Markdown.vue is the renderer, shared with the chat, and it is one
// component for a reason its header explains.

import {
  computed, defineAsyncComponent, onBeforeUnmount, onMounted, ref, watch,
} from 'vue'
import { mdiInformationOutline, mdiNotificationClearAll } from '@mdi/js'
import AskBox from './AskBox.vue'
import { fetchSessionAsk } from '../api.js'
import { clearedIds, rememberCleared } from '../dismissals.js'
import { ago, askEntryLabel, askPartColor } from '../format.js'

// Deferred into its own chunk, like every other consumer of it — Markdown.vue's
// header says why, and why nothing is drawn until it lands.
const Markdown = defineAsyncComponent(() => import('./Markdown.vue'))

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

// The reason the agent gave for closing a question, if it gave one — rendered
// inline inside the sentence below rather than shown alone, because on its own a
// clause like "took the safe branch" reads as something the operator did.
function closureReason(entry) {
  return entry.closure?.reason || ''
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
        <!-- One card per entry, inside the dock's own (#254). They were separated
             by a margin and nothing else, and the operator read the result as one
             thing: "messages look like the kinda flow into each other". Outlined
             rather than elevated or tonal, and the record's own reasoning — the
             outer card is the container and carries the shadow, so an entry needs
             an edge rather than a second one. The same shape as AskHistory.vue,
             because these are the same entries. -->
        <v-card
          v-for="entry in recent"
          :key="entry.id"
          class="entry"
          variant="outlined"
        >
          <v-card-text class="py-2">
            <div class="d-flex ga-2 align-center text-body-small text-medium-emphasis">
              <v-icon
                v-if="entry.kind === 'note'"
                :icon="mdiInformationOutline"
                size="small"
              />
              <span>{{ askEntryLabel(entry, live) }}</span>
              <span :title="entry.at || ''">{{ ago(entry.at, now) }}</span>
            </div>

            <!-- Which half of the exchange starts here, in a word, coloured from
                 format.js so the record cannot disagree with it. Not on a note: a
                 note is one voice saying one thing, and labelling it "QUESTION"
                 would promise an answer that is never coming. -->
            <div
              v-if="entry.kind !== 'note'"
              class="part"
              :class="`text-${askPartColor('question')}`"
            >QUESTION</div>

            <Markdown :text="entry.text" class="text-body-medium said" />

            <!-- The label leans with the words it labels, so the reply reads as one
                 block on its own side rather than as a heading over a stray line.
                 "you" stays on the line under it: the label says which half of the
                 exchange this is, and that is not the same fact as whose words they
                 are. -->
            <template v-if="entry.answer">
              <div
                class="part reply"
                :class="`text-${askPartColor('answer')}`"
              >ANSWER</div>
              <p class="text-body-medium said plain reply">
                you: {{ entry.answer.text }}
              </p>
            </template>

            <!-- Only when there is no answer: one that raced the close is the
                 answer to show, and saying "stopped waiting" under it would read
                 as if the reply had been thrown away. The agent's own reason is
                 rendered inline — it is prose, and this is a line. -->
            <p
              v-if="entry.closed && !entry.answered"
              class="text-body-small text-medium-emphasis mb-0"
            >
              The session stopped waiting for this<template
                v-if="closureReason(entry)"
              >: <Markdown
                :text="closureReason(entry)"
                inline
              /></template>. A reply can no longer reach it.
            </p>
          </v-card-text>
        </v-card>

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

<style scoped>
.entry {
  margin-bottom: 12px;
  min-width: 0;
}

/* The word that names a half of the exchange (#254). Small, spaced and heavier than
   the body, so it reads as a label rather than as the first line of the message —
   which is also why it is the label that is coloured and the message never is: the
   operator asked for exactly that ("i don't think color code the text entirely").
   The colour itself comes from format.js, because the record says the same words in
   the same ink. Same rule there, like every other rule these two views share. */
.part {
  font-size: 0.72rem;
  font-weight: 600;
  letter-spacing: 0.09em;
  margin-top: 6px;
}

/* Both halves of an entry sit in the same column with the same spacing; the class
   reaches the rendered one because a child component's root element carries its
   parent's scope attribute too. `anywhere` so a path or URL in a note does not
   scroll the page sideways on a phone (Markdown.vue restates it for the text it
   injects, which this rule cannot reach). */
.said {
  overflow-wrap: anywhere;
  margin: 2px 0 4px;
}

/* Only the verbatim half needs this: channel text carries its own newlines, and
   collapsing them would run a reply typed as a list into one paragraph. On
   rendered markup it would instead show the newlines *between* block tags as
   blank lines. Same class name as the conversation view, same meaning. */
.said.plain {
  white-space: pre-wrap;
}

/* The reply leans right — the label above it with it — so "you" and the agent read
   as two sides here the way they do in the conversation. It is the text that leans
   because there is still no bubble to lean: an entry's card holds both halves of one
   exchange rather than one turn each. That is also the whole of what makes it
   acceptable — a reply here is one short line, while the conversation's own turns
   lean as containers and keep their words left-aligned, because justified prose is
   what the operator has to read back. */
.reply {
  text-align: end;
}
</style>
