<script setup>
// Ask every project's assistant to rotate itself (issue 646). The platform asks
// and each one rotates at its own next breakpoint, with a handover, so progress
// is read rather than awaited: an instance is "rotated" once its project shows a
// later generation than the one that was asked.
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { mdiAutorenew } from '@mdi/js'
import { fetchRotateAll, requestRotateAll } from '../api.js'
import { assistantName } from '../ratchet.js'

// `block`: a labelled button for a page body, where a phone's header has no room for the icon.
defineProps({ block: { type: Boolean, default: false } })

const POLL_MS = 5000

const open = ref(false)
const targets = ref([])
const reasonLimit = ref(500)
const reason = ref('')
const loading = ref(false)
const sending = ref(false)
const problem = ref(null)
// The reply to a sent request: what each instance was asked, by project.
const sent = ref(null)
let timer = null
let disposed = false

const running = computed(() => targets.value.filter((target) => target.running))
const idle = computed(() => targets.value.filter((target) => !target.running))
const label = computed(() => `Ask every ${assistantName.value} to rotate`)

function key(target) {
  return target.workspace_id || 'assistant'
}

async function load() {
  loading.value = true
  try {
    const reply = await fetchRotateAll()
    targets.value = reply.targets || []
    reasonLimit.value = reply.reason_limit || reasonLimit.value
    problem.value = null
  } catch (exc) {
    problem.value = exc.message
  } finally {
    loading.value = false
  }
}

const progress = computed(() => {
  if (!sent.value) return []
  const now = new Map(targets.value.map((target) => [key(target), target]))
  return sent.value.targets.map((asked) => {
    const current = now.get(key(asked))
    let state = asked.outcome
    if (asked.outcome === 'requested') {
      state = current && current.generation > asked.generation ? 'rotated' : 'pending'
    }
    return { ...asked, state, generationNow: current?.generation ?? asked.generation }
  })
})
const settled = computed(() => progress.value.every((row) => row.state !== 'pending'))

const STATES = {
  rotated: 'rotated',
  pending: 'requested — still pending',
  not_running: 'not running, not asked',
  not_reached: 'not reached',
  failed: 'not sent',
}

function stopPolling() {
  clearInterval(timer)
  timer = null
}

async function send() {
  sending.value = true
  problem.value = null
  try {
    sent.value = await requestRotateAll(reason.value.trim())
    targets.value = sent.value.targets
    stopPolling()
    // Closed or unmounted while the request was in flight: either already stopped
    // a poll that had not started, and this one would outlive it.
    if (open.value && !disposed) timer = setInterval(load, POLL_MS)
  } catch (exc) {
    problem.value = exc.message
  } finally {
    sending.value = false
  }
}

watch(open, (isOpen) => {
  if (isOpen) {
    sent.value = null
    reason.value = ''
    problem.value = null
    load()
  } else {
    stopPolling()
  }
})
watch(settled, (done) => { if (done && sent.value) stopPolling() })
onBeforeUnmount(() => {
  disposed = true
  stopPolling()
})
</script>

<template>
  <v-tooltip :text="label" location="bottom" open-on-focus>
    <template #activator="{ props: tooltip }">
      <v-btn v-if="block" v-bind="tooltip" :prepend-icon="mdiAutorenew" variant="text" class="rotate-all"
        @click="open = true">{{ label }}</v-btn>
      <v-btn v-else v-bind="tooltip" :icon="mdiAutorenew" variant="text" :aria-label="label" class="rotate-all"
        @click="open = true" />
    </template>
  </v-tooltip>
  <v-dialog v-model="open" max-width="520">
    <v-card class="rotate-all-dialog">
      <v-card-title class="text-title-medium">{{ label }}</v-card-title>
      <v-card-text>
        <p class="text-body-small mb-3">
          Each running {{ assistantName }} is asked to hand over and rotate itself at its next
          breakpoint. Nothing is stopped: a clean handover matters more than speed.
        </p>
        <v-alert v-if="problem" type="error" density="compact" class="mb-3" role="alert">{{ problem }}</v-alert>
        <div v-if="loading && !targets.length" class="text-body-small text-medium-emphasis">reading…</div>

        <template v-else-if="!sent">
          <ul class="rotate-targets mb-3" aria-label="instances this reaches">
            <li v-for="target in running" :key="key(target)" class="text-body-medium">
              {{ target.project_name || assistantName }} · generation {{ target.generation }}
            </li>
            <li v-for="target in idle" :key="key(target)" class="text-body-small text-medium-emphasis">
              {{ target.project_name || assistantName }} · not running, not asked
            </li>
          </ul>
          <p v-if="!running.length" class="text-body-small mb-3">No {{ assistantName }} is running.</p>
          <v-textarea
            v-model="reason"
            label="reason (optional)"
            :maxlength="reasonLimit"
            rows="2"
            auto-grow
            persistent-hint
            hint="Goes into each request, so the handover and the next generation can say why."
            :disabled="sending"
          />
        </template>

        <ul v-else class="rotate-targets" aria-label="rotation progress" aria-live="polite">
          <li v-for="row in progress" :key="key(row)" class="text-body-medium" :data-state="row.state">
            {{ row.project_name || assistantName }} · generation {{ row.generation }}:
            <strong>{{ STATES[row.state] || row.state }}</strong>
            <template v-if="row.state === 'rotated'"> to generation {{ row.generationNow }}</template>
            <span v-if="row.detail" class="text-body-small text-medium-emphasis"> — {{ row.detail }}</span>
          </li>
        </ul>
      </v-card-text>
      <v-card-actions>
        <v-spacer />
        <v-btn variant="text" @click="open = false">{{ sent ? 'close' : 'cancel' }}</v-btn>
        <v-btn v-if="!sent" color="primary" variant="tonal" :loading="sending" :disabled="!running.length || loading"
          @click="send">ask {{ running.length }} to rotate</v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>

<style scoped>
.rotate-targets {
  padding-inline-start: 1.25rem;
}
</style>
