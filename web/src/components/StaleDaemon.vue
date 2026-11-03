<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import { fetchHealth } from '../api.js'
import { DAEMON_OUTDATED, daemonOutdated } from '../daemon.js'

const outdated = ref(false)
let poll
let pending = false

async function load() {
  if (pending) return
  pending = true
  try {
    outdated.value = daemonOutdated(await fetchHealth())
  } catch {
    // An unreachable daemon is the fleet view's error to report, not a version skew.
  } finally {
    pending = false
  }
}

onMounted(() => {
  load()
  poll = setInterval(load, 30000)
})
onUnmounted(() => clearInterval(poll))
</script>

<template>
  <v-alert v-if="outdated" type="warning" class="stale-daemon mb-3" role="alert">
    {{ DAEMON_OUTDATED }}
  </v-alert>
</template>
