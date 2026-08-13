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
// What one entry looks like is not decided here: AskEntry.vue draws it, for this
// view and for the dock, and its header says why that had to stop being a copy —
// including the split this view used to argue for itself, that what the agent
// wrote is rendered and what you replied is not. Which entries are drawn is the
// whole of what this file decides, and the answer is all of them.

import {
  computed, onBeforeUnmount, onMounted, ref, watch,
} from 'vue'
import AskEntry from './AskEntry.vue'
import { fetchSessionAsk } from '../api.js'

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
        <!-- One card per entry, inside the pane's own, and drawn by the component
             the dock draws them with (#254, #274): they are the same entries, and a
             record that ran them together where the dock separated them would be
             one channel read two ways. What this view asks it for is everything an
             entry carried — the kind of each one, the alternatives a question
             offered, and the platform's sentence about a pair it could not read —
             because this is the view that is supposed to be the whole of what was
             said. -->
        <AskEntry
          v-for="entry in entries"
          :key="entry.id"
          :entry="entry"
          :live="live"
          :now="now"
          question-icon
          show-options
          show-problem
        />
      </v-card-text>
    </v-card>
  </div>
</template>
