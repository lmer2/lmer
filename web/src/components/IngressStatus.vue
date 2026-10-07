<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { fetchHealth } from '../api.js'
import { ago } from '../format.js'

const props = defineProps({ now: { type: Number, required: true } })
const health = ref(null)
const error = ref(null)
const states = {
  up: 'Up', starting: 'Starting', stopped: 'Stopped', retrying: 'Retrying',
  reconnecting: 'Reconnecting', disabled: 'Disabled',
  unconfigured: 'Not configured', misconfigured: 'Configuration incomplete',
}
const rows = computed(() => Object.entries(health.value?.ingress || {}))
let poll
let pending = false
async function load() {
  if (pending) return
  pending = true
  try {
    health.value = await fetchHealth()
    error.value = null
  } catch (exc) {
    error.value = exc.message
  } finally {
    pending = false
  }
}
onMounted(() => {
  load()
  poll = setInterval(load, 10000)
})
onUnmounted(() => clearInterval(poll))
</script>

<template>
  <v-expansion-panels class="mb-3" variant="accordion">
    <v-expansion-panel>
      <v-expansion-panel-title>
        <div>
          <div>Assistant message ingress</div>
          <div class="text-caption mt-1">
            {{ error ? 'Status unavailable' : rows.map(([name, state]) => `${name}: ${states[state.state] || 'Unknown'}`).join(' · ') }}
          </div>
        </div>
      </v-expansion-panel-title>
      <v-expansion-panel-text>
        <div v-if="error" role="alert">Could not refresh ingress status: {{ error }}</div>
        <div v-else-if="!health">Loading ingress status…</div>
        <div v-for="[name, state] in rows" :key="name" class="mb-2">
          <strong>{{ name === 'slack' ? 'Slack' : 'Matrix' }}</strong>:
          {{ error ? 'Unknown (last known status below)' : states[state.state] || 'Unknown' }}
          <span v-if="error">{{ states[state.state] || 'Unknown' }}</span>
          <div>Last event: {{ state.last_event_at ? ago(state.last_event_at, props.now) : 'None received' }}</div>
          <div v-if="state.last_error">Last error: {{ state.last_error }}</div>
        </div>
      </v-expansion-panel-text>
    </v-expansion-panel>
  </v-expansion-panels>
</template>
