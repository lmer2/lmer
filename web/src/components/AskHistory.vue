<script setup>
// Retention hides pane entries; the server's record stays intact.

import {
  computed, onBeforeUnmount, onMounted, ref, watch,
} from 'vue'
import AskEntry from './AskEntry.vue'
import { fetchSessionAsk } from '../api.js'
import { retainedEntries } from '../operatorChat.js'

const props = defineProps({
  sessionId: { type: String, required: true },
  keepLast: { type: Number, default: 0 },
  // Whether the session is still running, which here decides one thing only:
  // whether the record can still grow. Nothing on this view is answerable.
  live: { type: Boolean, default: false },
  now: { type: Number, default: () => Date.now() },
})

// Same interval as the dock, for the same reason: a human is typing at the other
// end and each poll is a directory listing.
const POLL_MS = 5000

const history = ref([])
const entries = computed(() => retainedEntries(history.value, props.keepLast))
const problem = ref(null)
const loaded = ref(false)
let timer = null
let disposed = false
let generation = 0
let owner = null

const empty = computed(() => !entries.value.length)

async function load() {
  const mine = generation
  try {
    const page = await fetchSessionAsk(props.sessionId)
    if (disposed || mine !== generation) return
    history.value = page.entries || []
    problem.value = null
  } catch (exc) {
    if (disposed || mine !== generation) return
    // A failed poll must not discard entries already on screen.
    problem.value = exc.message
  } finally {
    if (!disposed && mine === generation) loaded.value = true
  }
}

function start() {
  if (owner !== props.sessionId) {
    owner = props.sessionId
    history.value = []
    loaded.value = false
    problem.value = null
  }
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

    <p v-if="loaded && history.length > entries.length" class="text-medium-emphasis">
      Showing the newest {{ entries.length }} entries. Older entries are hidden by your chat display setting.
    </p>
    <v-card v-if="loaded && !empty" class="mb-3">
      <v-card-text>
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
