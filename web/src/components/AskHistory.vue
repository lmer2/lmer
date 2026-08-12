<script setup>
// The whole of one session's ask channel, as a record (T40).
//
// The other half of AskChannel.vue, and the reason that one is allowed to forget
// things. The dock above the tabs answers "is anything waiting on me", so it
// collapses what has been dealt with and lets the operator clear the rest. This
// view answers "what was said", so it shows every entry the channel has: notes,
// questions that were answered, questions the session closed because it stopped
// waiting, and the ones that were never answered at all — cleared or not, because
// clearing is a view operation over there and this is the view it did not touch.
//
// Read-only, and that is the whole design rather than a limitation. Replying
// happens in one place — the dock, where the question that needs an answer is —
// and a second box down here would be a second way to send the same file, one of
// them out of sight of the alert that says whether the session is even alive.
//
// Read from `GET api/sessions/{id}/ask`, the same directory listing the dock
// reads. Two readers of one channel is a cost worth naming: while this pane is
// open on a live run, the run detail is making two of these requests every five
// seconds instead of one. Both are directory listings on the daemon, this pane is
// only mounted while the operator is looking at it (a tab renders nothing until it
// is selected), and the alternative — lifting the fetch into RunDetail and passing
// entries down — would put this feed's polling in the view that owns the run.
//
// Polling only while the session is live, like the dock: once it has exited
// nothing can arrive, so one read is the whole record.
//
// What the agent wrote is rendered, what you replied is not. The split is the
// conversation view's and the reasons are AskChannel.vue's — above all that a
// reply went to the session as bytes, and a line that quietly ate a pair of
// asterisks would be misreporting what it got.

import {
  computed, defineAsyncComponent, onBeforeUnmount, onMounted, ref, watch,
} from 'vue'
import { mdiCommentQuestionOutline, mdiInformationOutline } from '@mdi/js'
import { fetchSessionAsk } from '../api.js'
import { ago, askEntryLabel, askPartColor } from '../format.js'

// Deferred into its own chunk, like every other consumer of it — Markdown.vue's
// header says why, and why nothing is drawn until it lands.
const Markdown = defineAsyncComponent(() => import('./Markdown.vue'))

const props = defineProps({
  sessionId: { type: String, required: true },
  // Whether the session is still running, which here decides one thing only:
  // whether the record can still grow. Nothing on this view is answerable.
  live: { type: Boolean, default: false },
  now: { type: Number, default: () => Date.now() },
})

// Same interval as the dock, for the same reason: a human is typing at the other
// end and each poll is a directory listing.
const POLL_MS = 5000

const entries = ref([])
const problem = ref(null)
const loaded = ref(false)
let timer = null
let disposed = false
let generation = 0

const empty = computed(() => !entries.value.length)

// The options a question offered, kept in the record because they are part of what
// was asked. Verbatim, and pointedly not as anything tappable: over in the dock a
// chip is literally the text it puts in the reply box, and one here would promise
// to send something on a view that sends nothing.
function options(entry) {
  return (entry.options || []).join(' · ')
}

// The reason the agent gave for closing a question, if it gave one — rendered
// inline inside the sentence rather than shown alone, because on its own a clause
// like "took the safe branch" reads as something the operator did.
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
    // The record already on screen stays: a failed poll must not empty the one
    // view that is supposed to be the whole of what was said.
    problem.value = exc.message
  } finally {
    if (!disposed && mine === generation) loaded.value = true
  }
}

function start() {
  generation += 1
  clearInterval(timer)
  load()
  if (props.live) timer = setInterval(load, POLL_MS)
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
  <div>
    <v-alert v-if="problem" type="error" density="compact" class="mb-3">
      {{ problem }}
    </v-alert>

    <p v-if="!loaded" class="text-medium-emphasis">reading…</p>

    <!-- Nothing was ever posted, which is almost every run. Said in a sentence
         rather than left as a blank pane, so the view does not read as broken. -->
    <p v-else-if="empty" class="text-medium-emphasis">
      This session posted nothing on its channel.
    </p>

    <v-card v-else class="mb-3">
      <v-card-text>
        <!-- One card per entry, inside the pane's own (#254). The operator, on the
             list this used to be: "messages look like the kinda flow into each
             other". They were separated by a margin and nothing else, so a note
             followed by a question read as one long thing with a dim line in the
             middle of it. Outlined rather than elevated or tonal: the outer card is
             the pane and carries the shadow, so an entry needs an edge rather than a
             second one — and a wash under half a dozen of them would repaint the
             pane. Same shape in the dock, which renders the same entries. -->
        <v-card
          v-for="entry in entries"
          :key="entry.id"
          class="entry"
          variant="outlined"
        >
          <v-card-text class="py-2">
            <div class="d-flex ga-2 align-center text-body-small text-medium-emphasis">
              <v-icon
                :icon="entry.kind === 'note' ? mdiInformationOutline : mdiCommentQuestionOutline"
                size="small"
              />
              <span>{{ askEntryLabel(entry, live) }}</span>
              <span :title="entry.at || ''">{{ ago(entry.at, now) }}</span>
            </div>

            <!-- Which half of the exchange starts here, in a word, coloured from
                 format.js so the dock cannot disagree with it. Not on a note: a note
                 is one voice saying one thing, and labelling it "QUESTION" would
                 promise an answer that is never coming. -->
            <div
              v-if="entry.kind !== 'note'"
              class="part"
              :class="`text-${askPartColor('question')}`"
            >QUESTION</div>

            <Markdown :text="entry.text" class="text-body-medium said" />

            <p
              v-if="options(entry)"
              class="text-body-small text-medium-emphasis said plain"
            >it offered: {{ options(entry) }}</p>

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
                 answer to show, and saying "stopped waiting" under it would read as
                 if the reply had been thrown away. The agent's own reason is
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
              /></template>.
            </p>

            <!-- Carried by the protocol rather than raised, so one unreadable pair
                 cannot empty the record (ask_channel/protocol.py). Shown as it was
                 written: it is the platform's sentence, not the agent's. -->
            <p
              v-if="entry.problem"
              class="text-body-small text-medium-emphasis mb-0"
            >{{ entry.problem }}</p>
          </v-card-text>
        </v-card>
      </v-card-text>
    </v-card>
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
   The colour itself comes from format.js, because the dock says the same words in
   the same ink. Same rule there, like every other rule these two views share. */
.part {
  font-size: 0.72rem;
  font-weight: 600;
  letter-spacing: 0.09em;
  margin-top: 6px;
}

/* Every part of an entry sits in the same column with the same spacing; the class
   reaches the rendered one because a child component's root element carries its
   parent's scope attribute too. `anywhere` so a path or URL in a note does not
   scroll the page sideways on a phone (Markdown.vue restates it for the text it
   injects, which this rule cannot reach). */
.said {
  overflow-wrap: anywhere;
  margin: 2px 0 4px;
}

/* Only the verbatim halves need this: what you replied carries the newlines you
   typed, and collapsing them would run a reply written as a list into one
   paragraph. On rendered markup it would instead show the newlines *between*
   block tags as blank lines. Same class name as the dock, same meaning. */
.said.plain {
  white-space: pre-wrap;
}

/* The reply leans right — the label above it with it — matching the conversation
   view's "what you said" side, so every view of a session reads the same way at a
   glance. The text leans rather than a bubble: an entry's card holds both halves of
   one exchange rather than one turn each. */
.reply {
  text-align: end;
}
</style>
